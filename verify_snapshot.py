"""Independently compare a snapshot's CSVs with its saved public API pages."""
import argparse
import csv
import hashlib
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


def digest(content):
    return hashlib.sha256(json.dumps(content, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def verify(folder):
    folder = Path(folder)
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    if not (folder / "paper_ids.json").exists():
        raise ValueError("Wait until the collector finishes writing the CSV exports")
    papers, reviews, fields = {}, {}, Counter()
    empty_reviews = 0
    for path in sorted((folder / "pages").glob("*.json")):
        page = json.loads(path.read_text(encoding="utf-8"))
        for note in page["response"]["notes"]:
            pid = note["id"]
            if pid in papers:
                raise ValueError("Duplicate raw paper: " + pid)
            papers[pid] = note.get("number")
            count = 0
            for r in note["details"]["replies"]:
                if any(i.endswith("/-/" + manifest["review_name"]) for i in r.get("invitations", [])):
                    if r["id"] in reviews:
                        raise ValueError("Duplicate raw review: " + r["id"])
                    raw_values = {k: v.get("value", "") if isinstance(v, dict) else v for k, v in r["content"].items()}
                    reviews[r["id"]] = (pid, digest(raw_values))
                    fields.update(r["content"].keys())
                    count += 1
            empty_reviews += count == 0
    csv.field_size_limit(2 ** 31 - 1)
    seen_papers, seen_reviews = set(), set()
    with (folder / "papers.csv").open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            pid = row["paper_id"]
            if pid in seen_papers or pid not in papers or str(papers[pid]) != row["submission_number"]:
                raise ValueError("Invalid or duplicate CSV paper: " + pid)
            seen_papers.add(pid)
    with (folder / "reviews.csv").open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            rid = row["review_id"]
            if rid in seen_reviews or rid not in reviews:
                raise ValueError("Invalid or duplicate CSV review: " + rid)
            pid, raw_digest = reviews[rid]
            if pid != row["paper_id"]:
                raise ValueError("Review linked to wrong paper: " + rid)
            content = json.loads(row["content_json"])
            if digest(content) != raw_digest:
                raise ValueError("Review content differs from raw snapshot: " + rid)
            for key, val in content.items():
                expected = json.dumps(val, ensure_ascii=False) if isinstance(val, (dict, list)) else ("" if val is None else str(val))
                if row.get("content." + key) != expected:
                    raise ValueError("Missing or changed review field: " + rid + "/" + key)
            seen_reviews.add(rid)
    if seen_papers != set(papers) or seen_reviews != set(reviews):
        raise ValueError("CSV coverage does not match raw API pages")
    ids = json.loads((folder / "paper_ids.json").read_text(encoding="utf-8"))
    if len(ids) != len(papers) or {p["paper_id"] for p in ids} != set(papers):
        raise ValueError("Paper ID manifest does not match raw snapshot")
    report = {"verified_at_utc": datetime.now(timezone.utc).isoformat(), "status": "passed",
              "scope": "Export integrity against saved API pages; live tab coverage is recorded separately in manifest.json",
              "papers": len(papers), "reviews": len(reviews), "papers_without_public_reviews": empty_reviews,
              "review_field_counts": dict(fields), "all_review_content_matches_raw_pages": True}
    (folder / "validation.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot")
    verify(parser.parse_args().snapshot)
