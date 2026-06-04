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


def run(venue_id: str, out_dir: Path) -> None:
    client = OpenReviewClient(baseurl="https://api2.openreview.net")

    ids_path = out_dir / f"{slug(venue_id)}_author_ids.json"
    if not ids_path.exists():
        print(f"Missing {ids_path}. Run scraper.py first.")
        sys.exit(1)
    author_ids = json.loads(ids_path.read_text(encoding="utf-8"))
    print(f"Fetching {len(author_ids)} author profiles...")

    profiles = []
    for i, aid in enumerate(author_ids, 1):
        try:
            p = client.get_profile(aid)
            profiles.append(p)
            name = preferred_name(p.content or {})
            print(f"  [{i}/{len(author_ids)}] {aid}  {name}")
        except Exception as e:
            print(f"  [{i}/{len(author_ids)}] {aid}  SKIP ({e})")
        time.sleep(0.15)

    if not profiles:
        print("No profiles fetched.")
        return

    max_history = max(len((p.content or {}).get("history") or []) for p in profiles)
    max_history = max(max_history, 1)
    rows = [author_row(p, max_history) for p in profiles]

    out_path = out_dir / f"{slug(venue_id)}_authors.csv"
    written = _write_rows(rows, out_path)
    print(f"Wrote {len(rows)} authors -> {written}  (max history entries: {max_history})")


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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--venue", default="ICLR.cc/2026/Conference")
    ap.add_argument("--out", default="out")
    args = ap.parse_args()
    run(args.venue, Path(args.out))


if __name__ == "__main__":
    main()
