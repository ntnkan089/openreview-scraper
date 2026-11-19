"""Capture the public Active Submissions tab without decision buckets.

Each invocation creates a new UTC snapshot; --resume continues an interrupted one.
Raw API pages are retained permanently, including all publicly returned replies.
"""
from __future__ import annotations

import argparse
import copy
import os
import csv
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

BASE = "https://api2.openreview.net"


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def value(content, key, default=""):
    result = (content or {}).get(key, default)
    return result.get("value", default) if isinstance(result, dict) else result


def atomic_json(path, data):
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


class AccessBlocked(RuntimeError):
    pass


class API:
    def __init__(self, delay=0.5, token=None):
        self.session = requests.Session()
        self.delay = delay
        if token:
            self.session.headers["Authorization"] = "Bearer " + token.removeprefix("Bearer ")

    def get(self, endpoint, params):
        for attempt in range(6):
            time.sleep(self.delay)
            try:
                response = self.session.get(BASE + endpoint, params=params, timeout=(15, 120))
            except (requests.Timeout, requests.ConnectionError):
                if attempt == 5:
                    raise
                time.sleep(min(2 ** attempt, 30))
                continue
            try:
                data = response.json()
            except ValueError:
                response.raise_for_status()
                raise RuntimeError("API returned non-JSON content; snapshot remains incomplete")
            if response.status_code in (401, 403):
                challenge = data.get("details", {}).get("challengeUrl", "")
                raise AccessBlocked(f"OpenReview access blocked ({response.status_code}): "
                                    f"{data.get('message', data)} {challenge}")
            if response.status_code == 429 or response.status_code >= 500:
                if attempt < 5:
                    retry = response.headers.get("Retry-After", "")
                    time.sleep(min(float(retry) if retry.isdigit() else 2 ** (attempt + 1), 60))
                    continue
            response.raise_for_status()
            if "error" in data or data.get("name", "").endswith("Error"):
                raise RuntimeError(str(data))
            return data
        raise RuntimeError("Retry limit reached")


def public_note(note):
    """Exclude private notes and field-level private values when using sign-in."""
    if "everyone" not in note.get("readers", []):
        return None
    result = copy.deepcopy(note)
    result.pop("details", None)
    result["content"] = {
        key: val for key, val in note.get("content", {}).items()
        if not isinstance(val, dict) or "readers" not in val or "everyone" in val["readers"]
    }
    # Sign-in can expose the true author of anonymous notes to privileged users.
    result.pop("tauthor", None)
    return result


def public_response(data):
    result = copy.deepcopy(data)
    for i, submission in enumerate(data.get("notes", [])):
        safe = public_note(submission)
        if safe is None:
            raise RuntimeError("Active tab returned a non-public submission; refusing to export it")
        details = submission.get("details", {})
        safe["details"] = {}
        if "replies" in details:
            safe["details"]["replies"] = [clean for note in details["replies"] if (clean := public_note(note)) is not None]
        result["notes"][i] = safe
    return result


def scan(api, query, page_size=100, details=None):
    offset = 0
    while True:
        params = {**query, "limit": page_size, "offset": offset, "sort": "number:asc"}
        if details:
            params["details"] = details
        data = api.get("/notes", params)
        if not isinstance(data.get("notes"), list) or not isinstance(data.get("count"), int):
            raise RuntimeError("API response missing notes/count")
        yield params, data
        batch = data["notes"]
        if not batch:
            if offset < data["count"]:
                raise RuntimeError("Empty page before reported total; refusing incomplete export")
            break
        offset += len(batch)
        if offset >= data["count"]:
            break


def is_review(note, review_name="Official_Review"):
    return any(inv.endswith("/-/" + review_name) for inv in note.get("invitations", []))


def score(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, str):
        match = re.fullmatch(r"\s*(-?\d+(?:\.\d+)?)\s*(?::[\s\S]*)?", v)
        if match:
            number = float(match.group(1))
            return int(number) if number.is_integer() else number
    return None


def cell(v):
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    return "" if v is None else v


def write_csv(path, rows, required):
    keys = list(dict.fromkeys(required + [key for row in rows for key in row]))
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=keys)
        writer.writeheader()
        writer.writerows({key: cell(v) for key, v in row.items()} for row in rows)


def review_row(submission, review, captured_at):
    content = {key: value(review.get("content"), key) for key in review.get("content", {})}
    signatures = review.get("signatures", [])
    row = {
        "paper_id": submission["id"], "submission_number": submission.get("number"),
        "review_id": review["id"], "reviewer": "; ".join(signatures),
        "reviewer_alias": "; ".join(sig.rsplit("/", 1)[-1].replace("_", " ") for sig in signatures),
        "review_link": "https://openreview.net/forum?id=" + submission["id"] + "&noteId=" + review["id"],
        "captured_at_utc": captured_at,
        **{key: review.get(key) for key in ("cdate", "mdate", "tcdate", "tmdate", "replyto", "readers", "invitations")},
        "full_review": "\n\n".join(key.replace("_", " ").title() + ":\n" + str(cell(v)) for key, v in content.items()),
        "content_json": json.dumps(content, ensure_ascii=False),
    }
    for key, v in content.items():
        row["content." + key] = v
        numeric = score(v)
        if numeric is not None:
            row["score." + key] = numeric
    return row


def iter_papers(folder, manifest):
    """Read one saved page at a time so a full conference fits in memory."""
    for page in sorted((folder / "pages").glob("*.json")):
        envelope = json.loads(page.read_text(encoding="utf-8"))
        for submission in envelope["response"]["notes"]:
            pid = submission["id"]
            details = submission.get("details", {})
            if "replies" not in details:
                raise RuntimeError("Replies missing for " + pid + "; refusing silent review loss")
            notes = details["replies"]
            if not isinstance(notes, list):
                raise RuntimeError("Unexpected replies format for " + pid)
            rs = [n for n in notes if is_review(n, manifest["review_name"])]
            content = submission.get("content", {})
            paper = {
                "paper_id": pid, "submission_number": submission.get("number"),
                "link": "https://openreview.net/forum?id=" + pid,
                "title": value(content, "title"), "abstract": value(content, "abstract"),
                "authors": value(content, "authors", []), "authorids": value(content, "authorids", []),
                "venueid": value(content, "venueid"), "review_count": len(rs),
                "captured_at_utc": envelope["captured_at_utc"],
            }
            for key in content:
                paper["content." + key] = value(content, key)
            rows = []
            for i, note in enumerate(sorted(rs, key=lambda n: (n.get("signatures", []), n["id"])), 1):
                r = review_row(submission, note, envelope["captured_at_utc"])
                rows.append(r)
                for key, v in r.items():
                    if key not in ("paper_id", "submission_number"):
                        paper[f"Review {i} {key}"] = v
            yield paper, rows, notes


def export_snapshot(folder, manifest):
    # First pass discovers all columns, checks uniqueness, and retains only IDs
    # and small coverage metadata. Do not hold a conference of review text twice.
    papers, review_ids = {}, set()
    paper_keys = dict.fromkeys(["paper_id", "submission_number", "link", "title", "review_count"])
    review_keys = dict.fromkeys(["paper_id", "submission_number", "review_id", "reviewer", "full_review"])
    for paper, rows, _ in iter_papers(folder, manifest):
        pid = paper["paper_id"]
        if pid in papers:
            raise RuntimeError("Duplicate submission across pages: " + pid)
        papers[pid] = {key: paper[key] for key in ("paper_id", "submission_number", "link", "review_count")}
        paper_keys.update(dict.fromkeys(paper))
        for row in rows:
            if row["review_id"] in review_ids:
                raise RuntimeError("Duplicate review ID: " + row["review_id"])
            review_ids.add(row["review_id"])
            review_keys.update(dict.fromkeys(row))
    with (folder / "papers.csv").open("w", newline="", encoding="utf-8-sig") as pf, \
         (folder / "reviews.csv").open("w", newline="", encoding="utf-8-sig") as rf, \
         (folder / "public_replies.jsonl").open("w", encoding="utf-8") as nf:
        pw = csv.DictWriter(pf, fieldnames=list(paper_keys))
        rw = csv.DictWriter(rf, fieldnames=list(review_keys))
        pw.writeheader()
        rw.writeheader()
        note_ids = set()
        for paper, rows, notes in iter_papers(folder, manifest):
            pw.writerow({key: cell(v) for key, v in paper.items()})
            for row in rows:
                rw.writerow({key: cell(v) for key, v in row.items()})
            for note in notes:
                if note["id"] in note_ids:
                    continue
                note_ids.add(note["id"])
                record = {"paper_id": paper["paper_id"], "captured_at_utc": paper["captured_at_utc"], "note": note}
                nf.write(json.dumps(record, ensure_ascii=False) + "\n")
    atomic_json(folder / "paper_ids.json", [{key: p[key] for key in ("paper_id", "submission_number", "link")} for p in papers.values()])
    return papers, review_ids


def export_compatible(folder):
    """Validate saved data and produce the historical paper layout locally."""
    import pyarrow.parquet as pq
    from export_legacy_format import export, make_headers, make_mapping, validate_files
    from snapshot_to_parquet import convert
    from verify_snapshot import verify

    verify(folder)
    convert(folder)
    output = folder / "compatible"
    basename = "ICLR_2027_Conference_Active_Submissions_papers"
    csv_path = output / (basename + ".csv")
    parquet_path = output / (basename + ".parquet")
    report_path = output / (basename + "_validation.json")
    if csv_path.exists() or parquet_path.exists():
        # A resumed run can already have finished formatting before the final
        # API membership check failed. Validate and reuse those exact files.
        report = json.loads(report_path.read_text(encoding="utf-8"))
        source = folder / "papers.parquet"
        mapping = make_mapping(pq.ParquetFile(source).schema_arrow.names)
        headers, _ = make_headers(mapping)
        if pq.ParquetFile(parquet_path).schema_arrow.names != headers:
            raise ValueError("Existing compatible Parquet columns differ")
        count = validate_files(source, parquet_path, csv_path, mapping, headers)
        if count != report["papers"] or not report["all_source_columns_preserved"]:
            raise ValueError("Existing compatible export validation differs")
        print("Validated existing compatible exports", flush=True)
    else:
        report = export(folder, output)
    print("Compatible CSV: " + str(csv_path.resolve()), flush=True)
    print("Compatible Parquet: " + str(parquet_path.resolve()), flush=True)
    return {"papers": report["papers"], "reviews": report["reviews"],
            "columns": report["columns"],
            "csv": str(csv_path.relative_to(folder)),
            "parquet": str(parquet_path.relative_to(folder)),
            "validation": str(report_path.relative_to(folder))}


def capture(api, venue, out, page_size=100, limit=None, resume=None, compatible=False):
    if resume:
        folder = Path(resume)
        manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
        if manifest["venue"] != venue or manifest.get("status") == "complete":
            raise ValueError("Resume requires an incomplete snapshot of the same venue")
        if manifest["page_size"] != page_size or manifest["limit"] != limit:
            raise ValueError("Use the original page size and limit when resuming")
    else:
        slug = venue.replace(".cc/", "_").replace("/", "_")
        folder = Path(out) / (slug + "_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ"))
        folder.mkdir(parents=True, exist_ok=False)
        manifest = {"venue": venue, "started_at_utc": utcnow(), "status": "running",
                    "page_size": page_size, "limit": limit,
                    "scope": "Public API data, Active Submissions; latest values at each page fetch, not historical initial versions"}
    (folder / "pages").mkdir(exist_ok=True)
    atomic_json(folder / "manifest.json", manifest)
    print("Snapshot: " + str(folder.resolve()), flush=True)
    try:
        if not (folder / "venue.json").exists():
            group = api.get("/groups", {"id": venue})
            atomic_json(folder / "venue.json", group)
        else:
            group = json.loads((folder / "venue.json").read_text(encoding="utf-8"))
        content = group["groups"][0]["content"]
        active_id = value(content, "submission_venue_id")
        if not active_id:
            raise ValueError("Venue does not expose submission_venue_id")
        manifest.update({"active_venueid": active_id, "query": {"content.venueid": active_id},
                         "review_name": value(content, "review_name", "Official_Review")})
        atomic_json(folder / "manifest.json", manifest)
        offset = 0
        # Keep completed raw pages; retry only the unfinished page on resume.
        while True:
            page_path = folder / "pages" / f"{offset:09d}.json"
            size = min(page_size, limit - offset) if limit else page_size
            if size <= 0:
                break
            params = {**manifest["query"], "limit": size, "offset": offset, "sort": "number:asc", "details": "replies"}
            if page_path.exists():
                envelope = json.loads(page_path.read_text(encoding="utf-8"))
            else:
                data = api.get("/notes", params)
                if not isinstance(data.get("notes"), list) or not isinstance(data.get("count"), int):
                    raise RuntimeError("API response missing notes/count")
                envelope = {"captured_at_utc": utcnow(), "query": params, "response": public_response(data)}
                atomic_json(page_path, envelope)
            data = envelope["response"]
            notes = data["notes"]
            if offset == 0:
                manifest["api_count_at_start"] = data["count"]
            offset += len(notes)
            manifest.update({"captured_submission_rows": offset, "last_page_at_utc": envelope["captured_at_utc"]})
            atomic_json(folder / "manifest.json", manifest)
            print(f"Captured {offset}/{data['count']} papers with public replies", flush=True)
            if offset >= data["count"] or (limit and offset >= limit):
                break
            if not notes:
                raise RuntimeError("Empty page before reported total")
        manifest["phase"] = "exporting"
        atomic_json(folder / "manifest.json", manifest)
        papers, reviews = export_snapshot(folder, manifest)
        manifest.update({"phase": "exporting_compatible" if compatible else "verifying_membership",
                         "papers": len(papers), "reviews": len(reviews),
                         "raw_capture_count_verified": len(papers) == manifest["api_count_at_start"]})
        atomic_json(folder / "manifest.json", manifest)
        if compatible:
            manifest["compatible_exports"] = export_compatible(folder)
            manifest["phase"] = "verifying_membership"
            atomic_json(folder / "manifest.json", manifest)
        # Independently re-enumerate current tab membership, without large replies.
        final_ids, final_counts = set(), []
        for params, data in scan(api, manifest["query"], max(page_size, 1000)):
            final_counts.append(data["count"])
            for n in data["notes"]:
                if n["id"] in final_ids:
                    raise RuntimeError("Duplicate ID during membership verification")
                final_ids.add(n["id"])
        atomic_json(folder / "membership_at_end.json", {"captured_at_utc": utcnow(), "paper_ids": sorted(final_ids), "counts": final_counts})
        captured_ids = set(papers)
        full = limit is None
        stable = bool(final_counts) and all(c == len(final_ids) for c in final_counts) and len(final_ids) == manifest["api_count_at_start"]
        complete = full and stable and captured_ids == final_ids and bool(papers)
        manifest.update({"status": "complete" if complete else ("sample" if limit else "membership_changed"),
                         "finished_at_utc": utcnow(), "papers": len(papers), "reviews": len(reviews),
                         "papers_without_public_reviews": sum(p["review_count"] == 0 for p in papers.values()),
                         "api_count_at_end": len(final_ids), "missing_ids_at_end": sorted(final_ids - captured_ids),
                         "no_longer_active_ids": sorted(captured_ids - final_ids),
                         "coverage_verified": complete})
        atomic_json(folder / "manifest.json", manifest)
        print(json.dumps(manifest, indent=2), flush=True)
        return folder, manifest
    except BaseException as exc:
        manifest.update({"status": ("exported_verification_pending" if manifest.get("phase") == "verifying_membership"
                                    else "blocked" if isinstance(exc, AccessBlocked) else "interrupted"),
                         "last_error": str(exc), "last_error_at_utc": utcnow(), "coverage_verified": False})
        atomic_json(folder / "manifest.json", manifest)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--venue", default="ICLR.cc/2027/Conference")
    parser.add_argument("--out", default="out/snapshots")
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument("--limit", type=int, help="Smoke test only; omit to collect ALL papers")
    parser.add_argument("--delay", type=float, default=0.5)
    parser.add_argument("--resume", help="Existing incomplete snapshot directory")
    parser.add_argument("--login", action="store_true", help="Prompt for local OpenReview sign-in (credentials are never saved)")
    parser.add_argument("--compatible", action="store_true",
                        help="Also validate and export ICLR 2027 CSV and Parquet in the 2025/2026 paper layout")
    args = parser.parse_args()
    if not 1 <= args.page_size <= 1000 or (args.limit is not None and args.limit < 1) or args.delay < 0:
        parser.error("page-size must be 1..1000, limit positive, delay nonnegative")
    if args.compatible and args.venue != "ICLR.cc/2027/Conference":
        parser.error("--compatible currently supports ICLR.cc/2027/Conference only")
    try:
        if args.compatible:
            # Fail before downloading if the optional export dependency is absent.
            import pyarrow.parquet
        token = os.environ.get("OPENREVIEW_TOKEN")
        if args.login:
            from getpass import getpass
            from openreview.api import OpenReviewClient
            username = input("OpenReview email: ").strip()
            password = getpass("OpenReview password (not saved): ")
            client = OpenReviewClient(baseurl=BASE, username=username, password=password)
            token = client.token
            del password, client
        _, manifest = capture(API(args.delay, token=token), args.venue, args.out,
                              args.page_size, args.limit, args.resume, compatible=args.compatible)
    except Exception as exc:
        print(str(exc), file=sys.stderr, flush=True)
        return 1
    return 0 if manifest["status"] in ("complete", "sample") else 2


if __name__ == "__main__":
    sys.exit(main())
