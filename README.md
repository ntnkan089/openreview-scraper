# openreview-scraper

One-command ICLR 2027 collection and matching historical-format CSV/Parquet
export (Git Bash on Windows, after installing requirements):

```bash
./.venv/Scripts/python.exe active_snapshot.py --venue ICLR.cc/2027/Conference --login --compatible
```

Both files are written under the new timestamped snapshot's `compatible/`
folder, and their exact paths are printed. The command validates the saved
review content, retains all additional 2027 fields, and requires no manual
snapshot-folder selection. Repeat it to capture fresh data in a new folder.

For a fresh ICLR 2027 collection and subsequent CSV and Parquet exports with
historical columns, see [RUN_ICLR_2027.md](RUN_ICLR_2027.md). Each collection creates
a new timestamped folder. The historical-format exporter can select its own
four-paper sample when no manually prepared preview exists.

## Historical-format ICLR 2027 exports

To produce the same paper-per-row layout used by the ICLR 2026 and ICML 2025
files, using a saved 2027 snapshot without any network requests:

```powershell
.\.venv\Scripts\python.exe export_legacy_format.py out\snapshots\ICLR_2027_Conference_20261110T002041_545015Z
```

The outputs sit alongside the earlier conference files in `out/`:

- `ICLR_2027_Conference_Active_Submissions_papers.csv`
- `ICLR_2027_Conference_Active_Submissions_papers.parquet`
- `ICLR_2027_Conference_Active_Submissions_papers_sample.csv` (four real papers)
- `ICLR_2027_Conference_Active_Submissions_papers_validation.json`

The leading metadata columns and repeated `Review N`, `Reviewer N`, `Rebuttal N`,
and score columns retain the historical names and ordering. Up to seven reviewer
blocks are retained because the 2027 data includes papers with seven reviews.
The ICML `Overall Recommendation` columns are included for schema compatibility.
Unavailable fields remain blank. `type` is `Active Submission` for this snapshot.

Additional columns retain Informativeness, each written-review section, review
IDs, complete reviewer signatures, original timestamps, original content JSON,
numeric helper fields, and every other source column. Non-review public replies
are retained in `Public Replies JSON`, and matched author responses and reviewer
comments also populate their corresponding historical or additional columns.
The validation file records the full original-to-compatible column mapping.

CSV uses the same plain UTF-8 encoding and standard quoting as the older exports.
Parquet uses the same lossless, string-valued columns and Zstd compression.
The converter verifies every retained source column against the source Parquet
and compares every CSV cell with the new Parquet before publishing the files.
Original snapshots and the 2026/2025 data are preserved.

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


## Active Submissions / ICLR 2027 review snapshots

Use the separate collector while reviews are in progress. The older scraper.py
expects decision buckets and its text-field allowlist misses ICLR 2027 fields.
The new collector uses the exact venueid filter configured by the public Active
Submissions tab and the documented details=replies batch API.

From this project directory in PowerShell:

~~~powershell
# ALL active submissions; a new timestamped snapshot each time
.\.venv\Scripts\python.exe active_snapshot.py --venue ICLR.cc/2027/Conference

# ALL active submissions plus validated 2025/2026-layout CSV and Parquet
.\.venv\Scripts\python.exe active_snapshot.py --venue ICLR.cc/2027/Conference --login --compatible

# If anonymous access returns ChallengeRequiredError, sign in locally.
# The official openreview-py client prompts for MFA when required.
.\.venv\Scripts\python.exe active_snapshot.py --venue ICLR.cc/2027/Conference --login

# Smoke test only (never reported as full coverage)
.\.venv\Scripts\python.exe active_snapshot.py --limit 2 --login

# Resume the SAME incomplete run, preserving already captured raw pages.
# Repeat the original --limit/--page-size if non-default.
.\.venv\Scripts\python.exe active_snapshot.py --resume out/snapshots/EXACT_RUN_FOLDER --login

# Offline validation
.\.venv\Scripts\python.exe -m unittest test_active_snapshot -v
~~~

Browser challenge clearance is stored in the browser session and may not clear
Python API requests. --login asks for the password with hidden terminal input;
it does not save credentials or tokens. An existing OPENREVIEW_TOKEN environment
variable is also supported; never paste a token or password into chat. Sign-in
is optional; all output is restricted to public notes and public content fields.
The collector does not attempt to identify anonymous reviewers.

Each run creates out/snapshots/ICLR_2027_Conference_<UTC timestamp>/:

- papers.csv: one row per paper, all submission content and wide review blocks.
- reviews.csv: one row per official review, paper_id, submission_number,
  review_id, full anonymous reviewer signature and alias, all review content,
  posting/modification timestamps, and the review URL. content.<field> retains
  original values; score.<field> extracts numeric ratings without discarding
  their descriptive labels. Unknown fields are retained automatically.
- paper_ids.json: stable forum IDs, submission numbers and URLs for later joins.
- public_replies.jsonl: all returned public replies, including reviewer comments,
  meta-reviews, decisions and author replies when present.
- pages/*.json: retained public API payloads plus per-page UTC capture times;
  private notes, private fields and privileged tauthor metadata are excluded.
- venue.json: source venue configuration, including the tab filter.
- manifest.json: capture interval, errors, counts and coverage status.
- membership_at_end.json: independently enumerated final active paper IDs.

Only status=complete with coverage_verified=true means the full captured paper
ID set matches a second independent enumeration of Active Submissions, and its
count matches the API total. Papers without public reviews remain in the output.
A limited run is marked sample. Membership changes, duplicates, missing reply
payloads, access challenges or incomplete pagination cannot silently pass as a
complete collection. Raw pages survive interruption; resume retries the
unfinished page. A new invocation without --resume always creates a new folder.
For a fresh comparison of review changes, start a new run rather than resuming.

These are the latest values visible when each page is fetched, not a simultaneous
conference-wide snapshot or a reconstruction of earlier initial reviews.
Historical revisions and PDF attachments are outside this collector's scope;
public attachment links and all available review content are retained.

Authors can be joined later using paper_id (the OpenReview forum ID), even if
status or title changes. Do not infer authors from anonymous data. When author
fields become public, re-fetch those exact IDs, retain the ID-to-author mapping,
then feed the public ~profile IDs to author_scraper.py using its author manifest.
This is a separate future enrichment step; current review snapshots stay intact.

API reference: https://docs.openreview.net/how-to-guides/data-retrieval-and-modification/how-to-get-all-notes-for-submissions-reviews-rebuttals-etc

## Verify a completed snapshot

Run `.\.venv\Scripts\python.exe verify_snapshot.py out/snapshots/EXACT_RUN_FOLDER` after the collector finishes. This independently compares every review content field in the CSV with the saved API pages, checks paper/review ID uniqueness and coverage, and writes `validation.json` into the snapshot folder.

For compressed, lossless Parquet copies of completed CSVs, run `.\.venv\Scripts\python.exe snapshot_to_parquet.py out/snapshots/EXACT_RUN_FOLDER`. It streams batches through PyArrow, checks row counts, and preserves every column as text.

If the final live membership check is rate-limited after exports finish, the collector records `exported_verification_pending` and retains all exports and raw pages. Use `--resume` with that folder after access is available again. Final verification now requests 1,000 IDs per page to reduce API calls.
