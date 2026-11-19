# ICLR 2027: run a new public Active Submissions snapshot

Open a terminal in the openreview_scraper project folder.
Python 3.11 or newer is recommended.

## One-command collection and export (Git Bash on Windows)

After the one-time environment setup, run:

```bash
./.venv/Scripts/python.exe active_snapshot.py --venue ICLR.cc/2027/Conference --login --compatible
```

This collects ALL public Active Submissions and their returned public reviews
and replies, validates the saved data, and automatically produces matching CSV
and Parquet paper tables with the historical 2025/2026 columns plus new 2027
fields. No snapshot folder needs to be entered. The console prints both paths:

```text
out/snapshots/ICLR_2027_Conference_<UTC timestamp>/compatible/ICLR_2027_Conference_Active_Submissions_papers.csv
out/snapshots/ICLR_2027_Conference_<UTC timestamp>/compatible/ICLR_2027_Conference_Active_Submissions_papers.parquet
```

A small paper sample and column-mapping validation report are also created.
Run the same command again for fresh data after the rebuttal period; each run
uses a new timestamped folder. The sign-in prompt is still interactive.

For a new Git Bash environment, first run:

```bash
python -m venv .venv
./.venv/Scripts/python.exe -m pip install -r requirements.txt
```

## One-time setup (Windows PowerShell)

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## Full collection (Windows PowerShell; run from the project folder)

```powershell
.\.venv\Scripts\python.exe active_snapshot.py --venue ICLR.cc/2027/Conference --login --compatible
```

Enter your OpenReview email and password in the local terminal. Credentials
are not saved. Sign-in was needed for our collection because anonymous API
access returned a challenge; you can omit --login if anonymous access works.

On macOS/Linux, setup and run with:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python active_snapshot.py --venue ICLR.cc/2027/Conference --login --compatible
```

Use the same full-collection command after the rebuttal period. Each new run
creates a separate timestamped output folder and fetches current values.
Do not add --limit for a full collection. Do not use --resume for a refresh:
resume reuses the old run's completed pages instead of fetching them again.

## Outputs

The console prints the exact snapshot folder, typically:
out/snapshots/ICLR_2027_Conference_<UTC timestamp>/

It contains papers.csv, reviews.csv, public_replies.jsonl, paper_ids.json,
manifest.json, venue.json and raw pages/*.json. All publicly returned review
content fields are retained, including new fields, numeric ratings, confidence,
written sections, anonymous reviewer signatures and timestamps. Public author
responses are retained when returned by OpenReview. Public attachments are
represented by links; PDF downloads and historical revisions are not fetched.

## Optional manual conversion of an existing raw snapshot

The one-command --compatible option already performs these steps. Use these
commands only to convert an older snapshot that was collected without it.

In PowerShell, set $snapshot to the EXACT folder printed by your own run.
Replace the example below; do not use the placeholder literally:

```powershell
$snapshot = "out/snapshots/ICLR_2027_Conference_<UTC timestamp>"
.\.venv\Scripts\python.exe verify_snapshot.py "$snapshot"
.\.venv\Scripts\python.exe snapshot_to_parquet.py "$snapshot"
.\.venv\Scripts\python.exe export_legacy_format.py "$snapshot" --out "$snapshot/compatible"
```

snapshot_to_parquet.py creates both papers.parquet and reviews.parquet.
export_legacy_format.py makes the compatible 2026/2025 paper layout while
retaining all additional 2027 fields, plus a four-paper sample. Both CSV and
Parquet are generated. Existing exports are protected against overwriting;
choose a new output folder if you need to repeat the format conversion.

Rebuttal N / General Response are populated for public author-signed responses
with comment, rebuttal or response content, matched through replyto links.
Other public reply content is retained in Public Replies JSON even when it
cannot be assigned to those historical columns. Empty response columns do not
prove that no private author response exists. Authors can later be joined by
paper_id (the stable OpenReview forum ID).

## Completeness and interruptions

manifest.json records the capture interval, counts, errors and coverage status.
Only status=complete and coverage_verified=true confirm that saved paper IDs
also match the independent final enumeration of the active tab. Review values
are captured page by page, rather than at one simultaneous instant.

If interrupted, run the following using that SAME incomplete folder:

```powershell
.\.venv\Scripts\python.exe active_snapshot.py --venue ICLR.cc/2027/Conference --resume "$snapshot" --login --compatible
```

If the final membership check is rate-limited after exports are saved, the
current code reports exported_verification_pending. Saved exports can still
be used. With --compatible, the matching CSV and Parquet have already been
written and validated before this final API check. A resume validates and
reuses existing compatible exports, then retries the membership check.
Repeat any non-default --page-size or --limit used for the original run.

Resume reads and validates completed raw pages before requesting the first
missing page. The console reports `Reused ... saved papers` for this work;
it does not download those papers again or rewrite an earlier progress count
for every cached page. The raw pages remain the source of saved progress even
if the manifest update was interrupted. Windows checkpoint writes use unique
temporary files and retry temporary access-denied errors. Ctrl+C retains saved
pages and prints a resume hint.

## Offline code check

```powershell
.\.venv\Scripts\python.exe -m unittest test_active_snapshot test_legacy_format -v
```
