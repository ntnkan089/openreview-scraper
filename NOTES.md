# OpenReview Scraper — Prototype Notes

Prototype status as of 2026-05-25. Built against the spec in
`../Conference Scraping Instruction.md`.

## What's working

- **Paper-level CSV** (`scraper.py`) — one row per paper, columns match Shang's
  spec: link, title, authors (with profile URLs), keywords, abstract,
  primary_area, submission_number, type, Meta Review / Meta Reviewer,
  then `Review N` + `Reviewer N` + per-rating columns for each peer review.
- **Author-level CSV** (`author_scraper.py`) — one row per unique author
  profile: Link, Name, Personal Links (Google Scholar, LinkedIn, DBLP, etc.),
  Expertise, then `History N Title/Location/Start/End` for each career entry.
- **Adaptive across venues** — buckets and rating fields are discovered at
  runtime from the venue group's metadata, not hardcoded.
  - ICLR 2026: discovers 3 buckets (Oral / Poster / Reject) and 5 rating
    fields (soundness, presentation, contribution, rating, confidence).
  - ICML 2025: discovers 4 buckets (Oral / Spotlight Poster / Poster / Reject)
    and 1 rating field (overall_recommendation).
  - Rating fields are auto-discovered, so ICLR's five scores vs. ICML's single
    `overall_recommendation` need no manual change. If discovery ever misfires,
    `--rating-fields a b c` overrides it. The header comment block in
    `scraper.py` documents the only two spots that ever need editing for a new
    venue (rating-field names and the review/rebuttal text-field lists).
- Reviewer signatures (`Reviewer aTGr`, `Area Chair QrQ5`) and stable note
  IDs are captured.
- **Author rebuttals** (added 2026-05-29) — each `Rebuttal N` column holds the
  author response to `Review N`/`Reviewer N`, matched by walking the note
  `replyto` chain back to that reviewer's review. Multiple replies to the same
  reviewer are combined. Author comments aimed at all reviewers (forum-level)
  go in a single `General Response` column.
- **Reviewer quotes are stripped from rebuttals.** Authors quote the reviewer
  with markdown blockquotes (`> ...`) before answering; we drop every
  blockquote line so no reviewer comment text appears in our data
  (`strip_quoted_text`). If a future venue quotes differently, that's the one
  place to adjust.
- **Checkpointing / resume** (added 2026-06-03) — for full-venue scrapes
  (thousands of papers) the scraper flushes progress every 100 papers to
  `out/{slug}_papers_partial.jsonl` + `out/{slug}_checkpoint.json`. Re-running
  the same command auto-resumes; Ctrl-C / crashes / Excel locks survive.
  `--checkpoint-every N` changes cadence; `--restart` discards the checkpoint.
  Files are deleted after a clean CSV write.

## Sample runs

```
.venv/Scripts/python.exe scraper.py        --venue ICLR.cc/2026/Conference --limit 3
.venv/Scripts/python.exe author_scraper.py --venue ICLR.cc/2026/Conference
.venv/Scripts/python.exe scraper.py        --venue ICML.cc/2025/Conference --limit 1
```

Outputs land in `out/`.

## Open questions for Shang

1. **Wide vs long format for reviews.** Papers vary in reviewer count
   (3–5 typical). Wide format (`Review 1..N`) gets ragged — each row has
   trailing empties. Would a companion long-format CSV
   (`paper_id, review_idx, reviewer, text, ratings...`) be preferred for
   downstream analysis? Easy to add as a second output.

2. **Rejected papers may be limited.** ICLR 2026 exposes a `Submitted to
   ICLR 2026` bucket which contains both pending and rejected. Worth
   confirming the exact filtering Shang wants — strictly final-decision
   rejects vs. all unaccepted (which may include withdrawn / desk-rejected).

3. **ICML 2025 meta reviews aren't surfacing publicly** for the samples I
   pulled — no `Area_Chair` signatures on review forums. Need to confirm
   whether ICML 2025 makes meta-reviews public at all, or whether they
   require auth. Same risk applies to other venues.

4. **Decision bucket nuance.** ICLR's `decision_heading_map` says
   `Submitted to ICLR 2026 -> Reject`, but in practice that bucket may
   include other statuses. Worth a sanity pass before claiming complete
   reject coverage.

5. **Rate limits.** Unauthenticated calls cap at ~20 req/minute. For a
   full ICLR 2026 (~10k+ submissions, each with 3–5 reviews + meta-review
   + author profiles), we'll need an authenticated client and probably
   batching via `get_all_notes`. The prototype's `time.sleep(0.2)` only
   prevents the most obvious throttling.

## Known limitations / TODOs

- Author scraper hits 429s on long runs — needs better backoff or a logged-in
  client.
- Some profiles return "Profile Not Found" (e.g., 2 of 57 in the ICLR sample).
  Currently logged and skipped; could store NULL rows if desired.
- `flag_for_ethics_review` and `details_of_ethics_concerns` are currently
  bundled into the review text instead of as separate columns — easy to
  split out if useful.
- No de-duplication of personal links across name variants (one author can
  have multiple `~name1` IDs from re-registrations; OpenReview's profile
  merging is incomplete).
- Submission and meta-review rating field naming convention assumed
  English/lowercase. Should hold for all OpenReview venues but worth a check
  before adding NeurIPS / COLM / etc.

## File map

- `scraper.py` — paper-level fetcher + flattener
- `author_scraper.py` — author-level fetcher + flattener
- `probe.py`, `probe_venue.py`, `probe_author.py`, `probe_rebuttal.py` —
  one-off API explorers (not needed at runtime; kept for documentation of how
  the schema was reverse-engineered, incl. the rebuttal/quote format)
- `out/` — generated CSVs and JSON manifests
- `.venv/` — local Python env with `openreview-py`, `pandas`, `tqdm`
