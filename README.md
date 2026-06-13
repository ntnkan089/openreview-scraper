# openreview-scraper

Pulls papers + reviews + meta-reviews + author rebuttals from an
[OpenReview](https://openreview.net) venue (e.g. ICLR, ICML) and flattens
them into two CSVs: one paper-per-row and one author-per-row.

Cross-venue out of the box — the rating fields (ICLR's
`soundness/presentation/contribution/rating/confidence`, ICML's
`overall_recommendation`, etc.) and decision buckets (Oral / Poster /
Spotlight / Reject) are discovered at runtime, not hard-coded.

## Install

Needs Python 3.10+.

```bash
git clone https://github.com/ntnkan089/openreview-scraper.git
cd openreview-scraper
python -m venv .venv
# Windows PowerShell:
.\.venv\Scripts\Activate.ps1
# macOS / Linux:
source .venv/bin/activate
pip install -r requirements.txt
```

## Run

```bash
# 1. one papers file PER decision bucket + author-id manifest
#    (out/{venue}_{bucket}_papers.csv, out/{venue}_author_ids.json)
python scraper.py --venue ICLR.cc/2026/Conference --limit 7

# 2. authors CSV (consumes the manifest from step 1)
python author_scraper.py --venue ICLR.cc/2026/Conference

# Optional: human-readable preview of the CSVs into PREVIEW.md
python preview.py
```

`--limit N` is **per decision bucket**, so on ICLR (3 buckets) `--limit 7`
gives ~21 papers. On ICML 2025 (4 buckets) `--limit 5` gives ~20.

**Output is split one file per bucket** (e.g.
`ICLR_2026_Conference_Accept_Oral_papers.csv`,
`..._Accept_Poster_papers.csv`, `..._Reject_papers.csv`) so no single file
gets unwieldy on a full conference.

**File format.** Both scrapers take `--format csv|parquet|both` (default
`csv`). Parquet is ~half the size, lossless (columns stored as text), and
immune to Excel's 32k-char-per-cell / embedded-newline issues — but it's
binary, so read it with pandas / DuckDB / `preview.py`, not a text editor.
```bash
python scraper.py        --venue ICLR.cc/2026/Conference --limit 7 --format both
python author_scraper.py --venue ICLR.cc/2026/Conference --format parquet
```

Try ICML with the same scraper, no code changes:
```bash
python scraper.py        --venue ICML.cc/2025/Conference --limit 5
python author_scraper.py --venue ICML.cc/2025/Conference
```

## Long runs / crash recovery

Scraping a full conference (10k+ papers) takes hours. **Both** scrapers
checkpoint to disk every 100 items (configurable via `--checkpoint-every N`):
`scraper.py` flushes papers, and `author_scraper.py` flushes profiles (the
author stage is the slow, rate-limited one — OpenReview throttles
unauthenticated profile lookups to ~20/min). If a run crashes, you `Ctrl+C`
it, or your CSV is locked by Excel, **re-run the same command** and it picks
up from the last checkpoint. Use `--restart` to discard the checkpoint and
start fresh.

For long runs, prefer `--format parquet` (or `both`) — it roughly halves the
on-disk size of the output and avoids the per-cell limits that break large
review/rebuttal text in Excel.

Full-conference run, copy-paste (every paper in every bucket, checkpointed,
Parquet output). On Windows PowerShell use `.\.venv\Scripts\python.exe`; in
git bash / macOS / Linux use `.venv/Scripts/python.exe` (or just `python`
inside an activated venv):
```bash
python scraper.py        --venue ICLR.cc/2026/Conference --limit 100000 --format parquet --checkpoint-every 100
python author_scraper.py --venue ICLR.cc/2026/Conference --format parquet --checkpoint-every 100
```
If a run dies, re-run the **same** line to resume from the last checkpoint.
`--checkpoint-every 100` flushes progress every 100 items (the default — lower
it, e.g. `--checkpoint-every 25`, to checkpoint more often on a flaky network).

## Output columns

**`{venue}_{bucket}_papers.csv`** (one file per decision bucket) — one row per paper:

`link, title, authors, keywords, abstract, primary_area,
submission_number, type, Meta Review, Meta Reviewer, General Response,
Review N, Reviewer N, Rebuttal N, Review N <rating fields...>` (repeated
per reviewer).

- `Reviewer N` is the OpenReview-stable signature (e.g. `Reviewer aTGr`).
- `Rebuttal N` is the **author response** to that exact reviewer, matched
  by walking the OpenReview `replyto` chain. Multiple replies to the same
  reviewer are joined with `---`. Reviewer text quoted inside rebuttals
  (markdown blockquotes `> ...`) is stripped so no reviewer prose leaks
  into the rebuttal columns.
- `General Response` holds author comments aimed at all reviewers (not
  a specific review).
- The rating columns vary by venue and are auto-discovered. Pass
  `--rating-fields a b c` to override if discovery picks wrong.

**`{venue}_authors.csv`** — one row per unique author profile:

`Link, Name, Personal Links, Expertise, History N Title/Location/Start/End`.

## Adapting to a new venue / year

In most cases just change `--venue`. The two spots that can need a manual
edit for an unusual venue are documented at the top of `scraper.py`:

1. `REBUTTAL_TEXT_FIELDS` — content keys where rebuttal prose lives.
2. `TEXT_REVIEW_FIELDS` — content keys where review prose lives.

Sample outputs from ICLR 2026 + ICML 2025 are in `out/` for reference.

## Files

| file | what it does |
| --- | --- |
| `scraper.py` | paper-level fetch + flatten |
| `author_scraper.py` | author profile fetch (run after `scraper.py`) |
| `preview.py` | renders both CSVs into `PREVIEW.md` for eyeballing |
| `verify_ratings.py` | dump a single forum's review fields side-by-side with the OpenReview URL |
| `probe*.py` | one-off API explorers, kept as reference for reverse-engineering the schema |
| `NOTES.md` | design notes + open questions |
