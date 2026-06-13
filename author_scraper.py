"""Author-level scraper. Reads {venue_slug}_author_ids.json and pulls profiles.

Usage:
    python author_scraper.py --venue ICLR.cc/2026/Conference
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from pathlib import Path

from openreview.api import OpenReviewClient

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass


# Known link fields on a profile.content dict. Each profile may have a subset.
PERSONAL_LINK_FIELDS = [
    "homepage", "gscholar", "dblp", "semanticScholar",
    "orcid", "linkedin", "wikipedia", "aclanthology",
]

LINK_LABELS = {
    "homepage": "Homepage",
    "gscholar": "Google Scholar",
    "dblp": "DBLP",
    "semanticScholar": "Semantic Scholar",
    "orcid": "ORCID",
    "linkedin": "LinkedIn",
    "wikipedia": "Wikipedia",
    "aclanthology": "ACL Anthology",
}


def preferred_name(profile_content: dict) -> str:
    for n in profile_content.get("names") or []:
        if n.get("preferred"):
            return n.get("fullname") or ""
    names = profile_content.get("names") or []
    return (names[0].get("fullname") or "") if names else ""


def personal_links(profile_content: dict) -> str:
    """Render as 'Google Scholar (url); LinkedIn (url); ...'."""
    parts = []
    for f in PERSONAL_LINK_FIELDS:
        url = profile_content.get(f)
        if url:
            parts.append(f"{LINK_LABELS[f]} ({url})")
    return "; ".join(parts)


def expertise_summary(profile_content: dict) -> str:
    """Flat list of unique expertise keywords."""
    seen = []
    for entry in profile_content.get("expertise") or []:
        for kw in entry.get("keywords") or []:
            if kw not in seen:
                seen.append(kw)
    return "; ".join(seen)


def author_row(profile, max_history: int) -> dict:
    c = profile.content or {}
    row = {
        "Link": f"https://openreview.net/profile?id={profile.id}",
        "Name": preferred_name(c),
        "Personal Links": personal_links(c),
        "Expertise": expertise_summary(c),
    }
    history = c.get("history") or []
    for i in range(max_history):
        idx = i + 1
        if i < len(history):
            h = history[i]
            inst = h.get("institution") or {}
            name = inst.get("name") or ""
            domain = inst.get("domain") or ""
            country = inst.get("country") or ""
            location = name
            if domain:
                location = f"{name} ({domain})" if name else domain
            if country:
                location = f"{location} [{country}]" if location else country
            row[f"History {idx} Title"] = h.get("position") or ""
            row[f"History {idx} Location"] = location
            row[f"History {idx} Start"] = h.get("start") if h.get("start") is not None else ""
            end = h.get("end")
            row[f"History {idx} End"] = end if end is not None else "Present"
        else:
            row[f"History {idx} Title"] = ""
            row[f"History {idx} Location"] = ""
            row[f"History {idx} Start"] = ""
            row[f"History {idx} End"] = ""
    return row


def slug(venue_id: str) -> str:
    return venue_id.replace(".cc/", "_").replace("/", "_")


# ---------- checkpoint / resume ----------
# Profile fetches are one network call each and are the slow, rate-limited part
# of the pipeline (hundreds of authors, 429s on long runs). So we checkpoint the
# same way scraper.py does: every CHECKPOINT_EVERY profiles we flush to disk so a
# crash / Ctrl-C / 429 storm can be recovered by re-running the same command.
# Two files per venue:
#   {slug}_authors_checkpoint.json      — state: venue_id, count
#   {slug}_authors_partial.jsonl        — one fetched profile per line, append-only
# Both are removed after a successful final CSV write.


class _Profile:
    """Minimal stand-in for an OpenReview profile so resumed profiles expose the
    same `.id` / `.content` interface that author_row() expects. `query_id` is
    the ~id we asked for (used as the resume/skip key, since OpenReview may
    return a merged canonical id different from the one we queried)."""
    __slots__ = ("query_id", "id", "content")

    def __init__(self, query_id: str, id: str, content: dict):
        self.query_id = query_id
        self.id = id
        self.content = content


def author_checkpoint_paths(out_dir: Path, venue_id: str) -> tuple[Path, Path]:
    base = slug(venue_id)
    return (out_dir / f"{base}_authors_checkpoint.json",
            out_dir / f"{base}_authors_partial.jsonl")


def profile_to_json(p: _Profile) -> dict:
    return {"query_id": p.query_id, "id": p.id, "content": p.content or {}}


def profile_from_json(d: dict) -> _Profile:
    return _Profile(d.get("query_id") or d.get("id"),
                    d.get("id"), d.get("content") or {})


def load_author_checkpoint(state_path: Path, partial_path: Path
                           ) -> tuple[dict | None, list[_Profile]]:
    if not state_path.exists() or not partial_path.exists():
        return None, []
    state = json.loads(state_path.read_text(encoding="utf-8"))
    profiles: list[_Profile] = []
    with partial_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                profiles.append(profile_from_json(json.loads(line)))
    return state, profiles


def flush_author_checkpoint(state_path: Path, partial_path: Path,
                            state: dict, new_profiles: list[_Profile]) -> None:
    """Append new profiles to JSONL (durable), then atomically rewrite state."""
    if new_profiles:
        with partial_path.open("a", encoding="utf-8") as f:
            for p in new_profiles:
                f.write(json.dumps(profile_to_json(p), ensure_ascii=False) + "\n")
    tmp = state_path.with_suffix(state_path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(state_path)


def run(venue_id: str, out_dir: Path,
        checkpoint_every: int = 100, restart: bool = False,
        fmt: str = "csv") -> None:
    client = OpenReviewClient(baseurl="https://api2.openreview.net")
    out_dir.mkdir(parents=True, exist_ok=True)

    ids_path = out_dir / f"{slug(venue_id)}_author_ids.json"
    if not ids_path.exists():
        print(f"Missing {ids_path}. Run scraper.py first.")
        sys.exit(1)
    author_ids = json.loads(ids_path.read_text(encoding="utf-8"))

    state_path, partial_path = author_checkpoint_paths(out_dir, venue_id)

    # ---- resume / restart ----
    profiles: list[_Profile] = []
    if restart:
        if state_path.exists() or partial_path.exists():
            print("--restart: clearing previous author checkpoint files.")
            state_path.unlink(missing_ok=True)
            partial_path.unlink(missing_ok=True)
    else:
        resumed_state, profiles = load_author_checkpoint(state_path, partial_path)
        if resumed_state and resumed_state.get("venue_id") == venue_id:
            print(f"Resuming from checkpoint: {len(profiles)} profiles already done. "
                  f"Pass --restart to discard and start fresh.")
        elif resumed_state:
            print(f"Checkpoint is for a different venue "
                  f"({resumed_state.get('venue_id')!r}); ignoring it.")
            profiles = []

    done: set[str] = {p.query_id for p in profiles}
    print(f"Fetching {len(author_ids)} author profiles "
          f"({len(done)} already done)...")

    pending: list[_Profile] = []

    def do_flush(reason: str = "") -> None:
        if not pending:
            return
        state = {
            "venue_id": venue_id,
            "completed_count": len(profiles),
            "last_flush_ts": int(time.time()),
        }
        flush_author_checkpoint(state_path, partial_path, state, pending)
        tag = f" ({reason})" if reason else ""
        print(f"  [checkpoint{tag}] flushed {len(pending)} new profiles "
              f"(total {len(profiles)} on disk)")
        pending.clear()

    try:
        for i, aid in enumerate(author_ids, 1):
            if aid in done:
                continue
            try:
                p = client.get_profile(aid)
                holder = _Profile(aid, p.id, p.content or {})
                profiles.append(holder)
                pending.append(holder)
                done.add(aid)
                name = preferred_name(holder.content)
                print(f"  [{i}/{len(author_ids)}] {aid}  {name}")
                if len(pending) >= checkpoint_every:
                    do_flush()
            except Exception as e:
                # transient failures (e.g. 429) are NOT recorded as done, so a
                # re-run retries them.
                print(f"  [{i}/{len(author_ids)}] {aid}  SKIP ({e})")
            time.sleep(0.15)
        do_flush("end-of-fetch")
    except KeyboardInterrupt:
        print("\n!! Interrupted. Saving progress before exit.")
        do_flush("interrupt")
        print(f"Saved {len(profiles)} profiles to {partial_path.name}. "
              f"Re-run the same command to resume.")
        sys.exit(130)
    except Exception:
        print("\n!! Error during fetch. Saving progress before re-raising.")
        do_flush("error")
        raise

    if not profiles:
        print("No profiles fetched.")
        return

    max_history = max(len((p.content or {}).get("history") or []) for p in profiles)
    max_history = max(max_history, 1)
    rows = [author_row(p, max_history) for p in profiles]

    base_path = out_dir / f"{slug(venue_id)}_authors"
    written = _write_outputs(rows, base_path, fmt)
    names = ", ".join(p.name for p in written)
    print(f"Wrote {len(rows)} authors -> {names}  (max history entries: {max_history})")

    # Clean run → drop checkpoint files (CSV is now the source of truth).
    state_path.unlink(missing_ok=True)
    partial_path.unlink(missing_ok=True)


def _write_rows(rows: list[dict], out_path: Path) -> Path:
    """Write rows to out_path. If it's locked (e.g. open in Excel) fall back to
    a timestamped sibling file so a long profile fetch is never thrown away."""
    fieldnames = list(rows[0].keys())

    def _dump(path: Path) -> None:
        with path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            w.writerows(rows)

    try:
        _dump(out_path)
        return out_path
    except PermissionError:
        alt = out_path.with_name(
            f"{out_path.stem}_{time.strftime('%Y%m%d_%H%M%S')}{out_path.suffix}")
        print(f"  !! {out_path} is locked (open in Excel?). Writing to {alt.name} instead.")
        _dump(alt)
        return alt


def _write_parquet(rows: list[dict], out_path: Path) -> Path:
    """Write rows to Parquet (zstd), all columns as text (lossless mirror of the
    CSV). Falls back to a timestamped file if the target is locked."""
    import pandas as pd

    def _dump(path: Path) -> None:
        df = pd.DataFrame([{k: ("" if v is None else str(v)) for k, v in r.items()}
                           for r in rows])
        df.to_parquet(path, engine="pyarrow", compression="zstd", index=False)

    try:
        _dump(out_path)
        return out_path
    except PermissionError:
        alt = out_path.with_name(
            f"{out_path.stem}_{time.strftime('%Y%m%d_%H%M%S')}{out_path.suffix}")
        print(f"  !! {out_path} is locked. Writing to {alt.name} instead.")
        _dump(alt)
        return alt


def _write_outputs(rows: list[dict], base_path: Path, fmt: str) -> list[Path]:
    """Write rows as CSV and/or Parquet (fmt: csv|parquet|both). base_path has
    no extension."""
    written = []
    if fmt in ("csv", "both"):
        written.append(_write_rows(rows, base_path.with_suffix(".csv")))
    if fmt in ("parquet", "both"):
        written.append(_write_parquet(rows, base_path.with_suffix(".parquet")))
    return written


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--venue", default="ICLR.cc/2026/Conference")
    ap.add_argument("--out", default="out")
    ap.add_argument("--checkpoint-every", type=int, default=100,
                    help="Flush progress to disk every N profiles. On crash, "
                         "re-run the same command to resume. Default 100.")
    ap.add_argument("--restart", action="store_true",
                    help="Ignore (and delete) any existing author checkpoint "
                         "for this venue and start fresh.")
    ap.add_argument("--format", choices=["csv", "parquet", "both"], default="csv",
                    help="Output file format. 'parquet' is ~half the size and "
                         "lossless; 'both' writes .csv and .parquet. Default csv.")
    args = ap.parse_args()
    run(args.venue, Path(args.out),
        checkpoint_every=args.checkpoint_every, restart=args.restart,
        fmt=args.format)


if __name__ == "__main__":
    main()
