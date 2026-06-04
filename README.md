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
# 1. papers CSV + author-id manifest (out/{venue}_papers.csv, _author_ids.json)
python scraper.py --venue ICLR.cc/2026/Conference --limit 7

# 2. authors CSV (consumes the manifest from step 1)
python author_scraper.py --venue ICLR.cc/2026/Conference

# Optional: human-readable preview of both CSVs into PREVIEW.md
python preview.py
```

`--limit N` is **per decision bucket**, so on ICLR (3 buckets) `--limit 7`
gives ~21 papers. On ICML 2025 (4 buckets) `--limit 5` gives ~20.

Try ICML with the same scraper, no code changes:
```bash
python scraper.py        --venue ICML.cc/2025/Conference --limit 5
python author_scraper.py --venue ICML.cc/2025/Conference
```

## Long runs / crash recovery

Scraping a full conference (10k+ papers) takes hours. The scraper checkpoints
to disk every 100 papers (configurable via `--checkpoint-every N`). If it
crashes, you `Ctrl+C` it, or your CSV is locked by Excel, **re-run the same
command** and it picks up from the last checkpoint. Use `--restart` to
discard the checkpoint and start fresh.

## Output columns

**`{venue}_papers.csv`** — one row per paper:

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
