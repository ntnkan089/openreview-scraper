"""OpenReview conference scraper — prototype.

Pulls papers + reviews + meta-reviews + author rebuttals for a venue and
flattens into CSV rows shaped to Shang's spec.

Usage:
    python scraper.py --venue ICLR.cc/2026/Conference --limit 5
    python scraper.py --venue ICML.cc/2025/Conference --limit 5

Outputs:
    {venue_slug}_papers.csv
    {venue_slug}_authors.csv  (run author_scraper.py separately)

Long runs (--limit in the thousands) automatically checkpoint every 100 papers
to {venue_slug}_papers_partial.jsonl + {venue_slug}_checkpoint.json. If the
script crashes / is killed / Excel locks the CSV, re-run the same command and
it picks up where it left off. Pass --checkpoint-every N to change the cadence
or --restart to ignore the checkpoint.

============================================================================
ADAPTING TO A NEW CONFERENCE / YEAR  (read this first)
============================================================================
Most venue differences are handled AUTOMATICALLY — you normally only change
the --venue argument. The two things that differ between venues are:

1. REVIEW RATING FIELDS.
   ICLR has several scores (soundness, presentation, contribution, rating,
   confidence); ICML 2025 has just one (overall_recommendation). You do NOT
   have to set these by hand: discover_rating_fields() inspects a real review
   note and finds them at runtime. If auto-discovery ever picks wrong, pass
   them explicitly, e.g.:
       python scraper.py --venue X --rating-fields overall_recommendation
   See discover_rating_fields() and DEFAULT_RATING_FIELDS below.

2. REVIEW / REBUTTAL FREE-TEXT FIELD NAMES.
   Which content keys hold narrative text. Edit TEXT_REVIEW_FIELDS (review
   prose) and REBUTTAL_TEXT_FIELDS (rebuttal prose) below if a new venue uses
   a field name not already listed. Anything not listed there and not a
   discovered rating field is ignored.

Everything else (decision buckets, accept tiers, the formally-rejected
venueid) is read from the venue group config in venue_buckets().
============================================================================
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time

# Make stdout UTF-8 on Windows so paper titles with em-dashes etc. don't crash prints.
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from openreview.api import OpenReviewClient


# Review-rating fields are discovered per-venue at runtime (see discover_rating_fields).
# This list is used only as the *first* venue's seed/fallback if discovery turns
# up nothing and we need somewhere to start.
DEFAULT_RATING_FIELDS = [
    "soundness",
    "presentation",
    "contribution",
    "rating",
    "confidence",
]

# Review.content keys treated as free-text narrative (joined into Review N column).
# Anything not in this list and not in the discovered rating fields is ignored.
TEXT_REVIEW_FIELDS = [
    "summary",
    "strengths",
    "weaknesses",
    "questions",
    "details_of_ethics_concerns",
    # ICML 2025 long-form fields:
    "claims_and_evidence",
    "methods_and_evaluation_criteria",
    "theoretical_claims",
    "experimental_designs_or_analyses",
    "relation_to_broader_scientific_literature",
    "essential_references_not_discussed",
    "other_strengths_and_weaknesses",
    "other_comments_or_suggestions",
    "questions_for_authors",
    # Generic fallbacks:
    "review",
    "comments",
]

# Content keys that hold author rebuttal / response prose. Author replies on
# OpenReview are usually "Official_Comment" notes whose body is in `comment`;
# some venues use a dedicated `rebuttal`/`response` field. Add new field names
# here if a future venue stores rebuttal text under a different key.
REBUTTAL_TEXT_FIELDS = [
    "comment",
    "rebuttal",
    "response",
]


@dataclass
class ReviewRow:
    """A single peer review, flattened."""
    reviewer_handle: str           # e.g. "Reviewer aTGr"
    reviewer_signature: str        # e.g. "ICLR.cc/2026/.../Reviewer_aTGr" — stable key
    note_id: str                   # OpenReview's internal note ID (e.g. 33364)
    full_text: str                 # joined summary/strengths/weaknesses/questions
    ratings: dict[str, Any] = field(default_factory=dict)
    rebuttal_text: str = ""        # author response(s) to THIS review (quotes stripped)


@dataclass
class PaperRow:
    """A single paper with all its reviews."""
    forum_id: str                  # OpenReview forum/submission id (used as resume key)
    link: str
    title: str
    authors: str                   # "Name1 (profile_url); Name2 (profile_url); ..."
    author_ids: list[str]          # internal use → author scraper
    keywords: str
    abstract: str
    primary_area: str
    submission_number: int | None
    type: str                      # "Accept (Oral)" / "Accept (Poster)" / "Reject"
    meta_review_text: str
    meta_reviewer_handle: str
    reviews: list[ReviewRow] = field(default_factory=list)
    general_response: str = ""     # author comments aimed at all reviewers, not one review


# ---------- helpers ----------

def content_value(content: dict | None, key: str, default: Any = "") -> Any:
    """OpenReview API v2 wraps every content field as {'value': X}. Unwrap it."""
    if not content:
        return default
    v = content.get(key)
    if v is None:
        return default
    if isinstance(v, dict) and "value" in v:
        return v["value"]
    return v


def handle_from_signature(sig: str) -> str:
    """ICLR.cc/2026/Conference/Submission25369/Reviewer_aTGr → 'Reviewer aTGr'."""
    last = sig.rsplit("/", 1)[-1]
    return last.replace("_", " ")


def is_official_review(note) -> bool:
    """Identify Official_Review notes by invitation, not content keys.

    Different venues use different rating-field names (ICLR='rating',
    ICML 2025='overall_recommendation', etc.), so we can't gate on a specific
    key. Instead match the invitation pattern Conference uses for top-level
    review submissions.
    """
    sig = (note.signatures or [""])[0]
    if "/Reviewer_" not in sig:
        return False
    invs = note.invitations or []
    # accept if any invitation ends in /-/Official_Review (no extra suffix like Rebuttal)
    for inv in invs:
        if inv.endswith("/-/Official_Review"):
            return True
    return False


def is_meta_review(note) -> bool:
    sig = (note.signatures or [""])[0]
    if "/Area_Chair_" not in sig:
        return False
    # meta-review notes have 'summary' content; avoid pulling AC's official comments
    return "summary" in (note.content or {})


def is_decision(note) -> bool:
    sig = (note.signatures or [""])[0]
    return sig.endswith("/Program_Chairs") and "decision" in (note.content or {})


def stitch_review_text(content: dict) -> str:
    """Join the structured review text fields into one verbatim block."""
    parts = []
    for key in TEXT_REVIEW_FIELDS:
        val = content_value(content, key)
        if val:
            label = key.replace("_", " ").title()
            parts.append(f"{label}:\n{val}")
    return "\n\n".join(parts)


def stitch_meta_review_text(content: dict) -> str:
    parts = []
    for key in ("summary", "reviewer_concerns", "reviewer_scores",
                "metareview", "justification_for_why_not_higher_score",
                "justification_for_why_not_lower_score"):
        val = content_value(content, key)
        if val:
            label = key.replace("_", " ").title()
            parts.append(f"{label}:\n{val}")
    return "\n\n".join(parts)


# ---------- rebuttals ----------

def strip_quoted_text(text: str) -> str:
    """Remove the reviewer's words that authors quote inside a rebuttal.

    On OpenReview, authors quote the reviewer with markdown blockquotes —
    lines beginning with '>' (e.g. '> How scalable is the pipeline?') — then
    answer underneath. Shang asked that NO reviewer comment text appear in our
    data, so we drop every blockquote line and keep only the authors' own prose.

    NOTE: if some future venue's authors quote reviewers differently (e.g.
    italics or plain quotation marks instead of '>'), extend the logic here.
    """
    if not text:
        return text
    kept = [ln for ln in text.splitlines() if not ln.lstrip().startswith(">")]
    cleaned = "\n".join(kept)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)   # collapse gaps left by removed quotes
    return cleaned.strip()


def is_author_rebuttal(note, forum_id: str) -> bool:
    """True if `note` is an author-written reply (rebuttal / response / comment).

    Authored by the submission's /Authors group, carries text, and is not the
    submission note itself. Reviewer and Area-Chair replies are excluded here,
    so their text never enters the rebuttal columns.
    """
    sig = (note.signatures or [""])[0]
    if not sig.endswith("/Authors"):
        return False
    if note.id == forum_id:                 # the submission note itself
        return False
    content = note.content or {}
    return any(content_value(content, k) for k in REBUTTAL_TEXT_FIELDS)


def stitch_rebuttal_text(content: dict) -> str:
    """Join an author note's text fields, then strip any quoted reviewer text."""
    parts = []
    for key in REBUTTAL_TEXT_FIELDS:
        val = content_value(content, key)
        if val:
            parts.append(str(val))
    return strip_quoted_text("\n\n".join(parts))


def resolve_target_review(note, by_id: dict, review_ids: set[str],
                          max_hops: int = 12) -> str | None:
    """Walk the replyto chain up from `note`; return the review note id it
    ultimately responds to, or None if it's a general/forum-level comment.

    Handles author -> review (direct) and author -> reviewer-comment -> ... ->
    review (threaded) by following replyto until a review is hit.
    """
    cur = getattr(note, "replyto", None)
    for _ in range(max_hops):
        if not cur:
            return None
        if cur in review_ids:
            return cur
        parent = by_id.get(cur)
        if parent is None:
            return None
        cur = getattr(parent, "replyto", None)
    return None


# ---------- core fetch ----------

def fetch_submissions(client, venue_id: str, limit: int,
                       venue_label: str | None = None,
                       venueid: str | None = None) -> list:
    """Get up to `limit` legit submissions matching either a venue label
    (the per-tier human label, e.g. 'ICLR 2026 Oral') OR a venueid (e.g.
    'ICLR.cc/2026/Conference/Rejected_Submission' for formally-rejected only).

    Exactly one of venue_label / venueid should be given.

    Filters out OpenReview Archive direct-uploads (venueid not starting
    with the conference prefix) — these are third-party mirrors that have
    no reviews.
    """
    assert (venue_label is None) ^ (venueid is None), \
        "give exactly one of venue_label / venueid"

    if venue_label is not None:
        query = {"venue": venue_label}
    else:
        query = {"venueid": venueid}

    out = []
    page = 0
    page_size = 50
    while len(out) < limit:
        batch = client.get_notes(
            content=query,
            limit=page_size,
            offset=page * page_size,
        )
        if not batch:
            break
        for n in batch:
            vid = content_value(n.content, "venueid", "")
            if isinstance(vid, str) and vid.startswith(venue_id):
                out.append(n)
                if len(out) >= limit:
                    break
        page += 1
        if len(batch) < page_size:
            break
    return out[:limit]


def fetch_paper_row(client, submission, decision_label: str,
                    rating_fields: list[str]) -> PaperRow:
    """Build a PaperRow with all reviews + meta review for one paper."""
    content = submission.content or {}
    forum_id = submission.id

    # author list with profile links
    names = content_value(content, "authors", []) or []
    ids = content_value(content, "authorids", []) or []
    pairs = []
    for nm, aid in zip(names, ids):
        if aid and aid.startswith("~"):
            url = f"https://openreview.net/profile?id={aid}"
            pairs.append(f"{nm} ({url})")
        else:
            pairs.append(nm)
    authors_str = "; ".join(pairs)

    # keywords can be list or comma-string
    kws = content_value(content, "keywords", "")
    keywords_str = ", ".join(kws) if isinstance(kws, list) else kws

    paper = PaperRow(
        forum_id=forum_id,
        link=f"https://openreview.net/forum?id={forum_id}",
        title=content_value(content, "title"),
        authors=authors_str,
        author_ids=[i for i in ids if i and i.startswith("~")],
        keywords=keywords_str,
        abstract=content_value(content, "abstract"),
        primary_area=content_value(content, "primary_area"),
        submission_number=submission.number,
        type=decision_label,                 # default; refined below if a Decision note exists
        meta_review_text="",
        meta_reviewer_handle="",
    )

    # pull all reply notes
    notes = client.get_notes(forum=forum_id)

    for n in notes:
        if is_decision(n):
            d = content_value(n.content, "decision")
            if d:
                paper.type = d
        elif is_meta_review(n):
            paper.meta_review_text = stitch_meta_review_text(n.content or {})
            paper.meta_reviewer_handle = handle_from_signature(n.signatures[0])
        elif is_official_review(n):
            sig = n.signatures[0]
            review = ReviewRow(
                reviewer_handle=handle_from_signature(sig),
                reviewer_signature=sig,
                note_id=n.id,
                full_text=stitch_review_text(n.content or {}),
                ratings={k: content_value(n.content, k) for k in rating_fields},
            )
            paper.reviews.append(review)

    # ---- attach author rebuttals to the review each one answers ----
    # Each author reply's replyto chain leads back to a specific reviewer's
    # review (-> that Rebuttal N), or to the forum root (-> General Response).
    by_id = {n.id: n for n in notes}
    review_by_note = {r.note_id: r for r in paper.reviews}
    review_ids = set(review_by_note)

    rebuttals = [n for n in notes if is_author_rebuttal(n, forum_id)]
    rebuttals.sort(key=lambda n: getattr(n, "cdate", 0) or 0)   # posting order

    per_review: dict[str, list[str]] = {}
    general: list[str] = []
    for n in rebuttals:
        text = stitch_rebuttal_text(n.content or {})
        if not text:
            continue
        target = resolve_target_review(n, by_id, review_ids)
        if target is not None:
            per_review.setdefault(target, []).append(text)
        else:
            general.append(text)

    # combine multiple rebuttals to the same review into one cell
    for note_id, texts in per_review.items():
        review_by_note[note_id].rebuttal_text = "\n\n---\n\n".join(texts)
    paper.general_response = "\n\n---\n\n".join(general)

    # stable ordering: by reviewer handle so re-runs produce deterministic columns
    paper.reviews.sort(key=lambda r: r.reviewer_handle)
    return paper


# ---------- flattening ----------

def _pretty(name: str) -> str:
    return name.replace("_", " ").title()


def paper_to_dict(paper: PaperRow, max_reviews: int,
                  rating_fields: list[str]) -> dict:
    row = {
        "link": paper.link,
        "title": paper.title,
        "authors": paper.authors,
        "keywords": paper.keywords,
        "abstract": paper.abstract,
        "primary_area": paper.primary_area,
        "submission_number": paper.submission_number,
        "type": paper.type,
        "Meta Review": paper.meta_review_text,
        "Meta Reviewer": paper.meta_reviewer_handle,
        # author comments addressed to all reviewers (not a single review)
        "General Response": paper.general_response,
    }
    for i in range(max_reviews):
        idx = i + 1
        # Rebuttal N sits in the same block as Review N / Reviewer N — it is the
        # author response to that exact reviewer (kept aligned by signature).
        if i < len(paper.reviews):
            r = paper.reviews[i]
            row[f"Review {idx}"] = r.full_text
            row[f"Reviewer {idx}"] = r.reviewer_handle
            row[f"Rebuttal {idx}"] = r.rebuttal_text
            for k in rating_fields:
                row[f"Review {idx} {_pretty(k)}"] = r.ratings.get(k, "")
        else:
            row[f"Review {idx}"] = ""
            row[f"Reviewer {idx}"] = ""
            row[f"Rebuttal {idx}"] = ""
            for k in rating_fields:
                row[f"Review {idx} {_pretty(k)}"] = ""
    return row


def write_papers_csv(papers: list[PaperRow], rating_fields: list[str],
                     out_path: Path) -> None:
    if not papers:
        print(f"No papers to write to {out_path}")
        return
    max_reviews = max((len(p.reviews) for p in papers), default=0)
    rows = [paper_to_dict(p, max_reviews, rating_fields) for p in papers]
    fieldnames = list(rows[0].keys())
    with out_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)
    print(f"Wrote {len(rows)} papers -> {out_path}  (max reviews per paper: {max_reviews})")


# ---------- venue config ----------

def venue_buckets(client, venue_id: str) -> tuple[dict[str, str], str | None]:
    """Return ({venue_label: decision_label}, rejected_venueid) for the venue.

    decision_heading_map drives the accept-tier buckets. The rejected
    venueid comes from the same group's `rejected_venue_id` field — used
    to fetch *formally rejected* papers only (excluding withdrawn /
    desk-rejected, which carry no reviews).
    """
    grp = client.get_group(venue_id)
    content = grp.content or {}

    raw = content.get("decision_heading_map")
    mapping = {}
    if raw:
        mapping = raw.get("value") if isinstance(raw, dict) else raw

    rejected_vid_raw = content.get("rejected_venue_id")
    rejected_vid = None
    if rejected_vid_raw:
        rejected_vid = (rejected_vid_raw.get("value")
                        if isinstance(rejected_vid_raw, dict) else rejected_vid_raw)

    return mapping, rejected_vid


def discover_rating_fields(client, venue_id: str) -> list[str]:
    """Inspect a sample Official_Review note to discover the rating-like fields
    for this venue (anything in content that isn't a text field). Falls back
    to DEFAULT_RATING_FIELDS if no sample is found.

    Done once per run instead of guessing per-paper.
    """
    # Find one sample submission for this venue
    sample = client.get_notes(content={"venueid": venue_id}, limit=1)
    if not sample:
        return list(DEFAULT_RATING_FIELDS)
    notes = client.get_notes(forum=sample[0].forum)
    review = next((n for n in notes if is_official_review(n)), None)
    if not review or not review.content:
        return list(DEFAULT_RATING_FIELDS)
    fields = []
    for k in review.content.keys():
        if k in TEXT_REVIEW_FIELDS:
            continue
        # skip housekeeping fields
        if k in ("code_of_conduct", "supplementary_material",
                 "flag_for_ethics_review"):
            continue
        fields.append(k)
    return fields or list(DEFAULT_RATING_FIELDS)


# ---------- checkpoint / resume ----------
# Long scrapes (10k+ papers) are too expensive to redo from scratch on a crash.
# Every CHECKPOINT_EVERY papers we flush progress to disk so a kill / network
# blip / Ctrl-C / Excel-lock can be recovered by simply re-running the same
# command. Two files per venue:
#   {slug}_checkpoint.json        — state: venue_id, rating_fields, count
#   {slug}_papers_partial.jsonl   — one paper per line, append-only
# Both are removed after a successful final CSV write.

def checkpoint_paths(out_dir: Path, venue_id: str) -> tuple[Path, Path]:
    base = slug(venue_id)
    return (out_dir / f"{base}_checkpoint.json",
            out_dir / f"{base}_papers_partial.jsonl")


def paper_to_json(p: PaperRow) -> dict:
    return {
        "forum_id": p.forum_id,
        "link": p.link,
        "title": p.title,
        "authors": p.authors,
        "author_ids": p.author_ids,
        "keywords": p.keywords,
        "abstract": p.abstract,
        "primary_area": p.primary_area,
        "submission_number": p.submission_number,
        "type": p.type,
        "meta_review_text": p.meta_review_text,
        "meta_reviewer_handle": p.meta_reviewer_handle,
        "general_response": p.general_response,
        "reviews": [
            {
                "reviewer_handle": r.reviewer_handle,
                "reviewer_signature": r.reviewer_signature,
                "note_id": r.note_id,
                "full_text": r.full_text,
                "ratings": r.ratings,
                "rebuttal_text": r.rebuttal_text,
            }
            for r in p.reviews
        ],
    }


def paper_from_json(d: dict) -> PaperRow:
    reviews = [ReviewRow(**r) for r in d.get("reviews", [])]
    return PaperRow(
        forum_id=d["forum_id"],
        link=d["link"],
        title=d["title"],
        authors=d["authors"],
        author_ids=d.get("author_ids", []),
        keywords=d.get("keywords", ""),
        abstract=d.get("abstract", ""),
        primary_area=d.get("primary_area", ""),
        submission_number=d.get("submission_number"),
        type=d["type"],
        meta_review_text=d.get("meta_review_text", ""),
        meta_reviewer_handle=d.get("meta_reviewer_handle", ""),
        reviews=reviews,
        general_response=d.get("general_response", ""),
    )


def load_checkpoint(state_path: Path, partial_path: Path
                    ) -> tuple[dict | None, list[PaperRow]]:
    """Return (state_dict, list_of_papers) if a checkpoint exists, else (None, [])."""
    if not state_path.exists() or not partial_path.exists():
        return None, []
    state = json.loads(state_path.read_text(encoding="utf-8"))
    papers: list[PaperRow] = []
    with partial_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                papers.append(paper_from_json(json.loads(line)))
    return state, papers


def flush_checkpoint(state_path: Path, partial_path: Path,
                     state: dict, new_papers: list[PaperRow]) -> None:
    """Append new papers to JSONL (durable) then atomically rewrite state."""
    if new_papers:
        with partial_path.open("a", encoding="utf-8") as f:
            for p in new_papers:
                f.write(json.dumps(paper_to_json(p), ensure_ascii=False) + "\n")
    tmp = state_path.with_suffix(state_path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(state_path)


# ---------- entry point ----------

def slug(venue_id: str) -> str:
    return venue_id.replace(".cc/", "_").replace("/", "_")


def run(venue_id: str, limit_per_bucket: int, out_dir: Path,
        rating_fields_override: list[str] | None = None,
        checkpoint_every: int = 100,
        restart: bool = False) -> list[PaperRow]:
    client = OpenReviewClient(baseurl="https://api2.openreview.net")
    out_dir.mkdir(parents=True, exist_ok=True)

    state_path, partial_path = checkpoint_paths(out_dir, venue_id)

    # ---- resume / restart logic ----
    resumed_state: dict | None = None
    all_papers: list[PaperRow] = []
    if restart:
        if state_path.exists() or partial_path.exists():
            print("--restart: clearing previous checkpoint files.")
            state_path.unlink(missing_ok=True)
            partial_path.unlink(missing_ok=True)
    else:
        resumed_state, all_papers = load_checkpoint(state_path, partial_path)
        if resumed_state and resumed_state.get("venue_id") == venue_id:
            print(f"Resuming from checkpoint: {len(all_papers)} papers already done. "
                  f"Pass --restart to discard and start fresh.")
        elif resumed_state:
            print(f"Checkpoint is for a different venue "
                  f"({resumed_state.get('venue_id')!r}); ignoring it.")
            resumed_state, all_papers = None, []

    completed_ids: set[str] = {p.forum_id for p in all_papers}

    buckets, rejected_venueid = venue_buckets(client, venue_id)
    if not buckets:
        print(f"Could not discover decision buckets for {venue_id}. Aborting.")
        sys.exit(1)
    print(f"Discovered buckets for {venue_id}:")
    for venue_field, decision in buckets.items():
        print(f"  venue={venue_field!r} -> decision={decision!r}")
    if rejected_venueid:
        print(f"Rejected venueid (formally rejected only): {rejected_venueid}")

    # Rating fields: explicit override > resumed checkpoint > auto-discover.
    # Reusing the resumed fields keeps the CSV columns consistent between runs.
    if rating_fields_override:
        rating_fields = rating_fields_override
        print(f"Using manual review rating fields: {rating_fields}")
    elif resumed_state and resumed_state.get("rating_fields"):
        rating_fields = resumed_state["rating_fields"]
        print(f"(resumed) review rating fields: {rating_fields}")
    else:
        rating_fields = discover_rating_fields(client, venue_id)
        print(f"Discovered review rating fields: {rating_fields}")

    pending_flush: list[PaperRow] = []

    def do_flush(reason: str = "") -> None:
        if not pending_flush:
            return
        state = {
            "venue_id": venue_id,
            "rating_fields": rating_fields,
            "completed_count": len(all_papers),
            "last_flush_ts": int(time.time()),
        }
        flush_checkpoint(state_path, partial_path, state, pending_flush)
        tag = f" ({reason})" if reason else ""
        print(f"  [checkpoint{tag}] flushed {len(pending_flush)} new papers "
              f"(total {len(all_papers)} on disk)")
        pending_flush.clear()

    try:
        for venue_field, decision in buckets.items():
            # Per Shang: reject bucket = formally rejected + has reviews.
            # Use venueid (Rejected_Submission) instead of the broader
            # 'Submitted to ...' label, which mixes in withdrawn/desk-rejected.
            is_reject = decision.lower().startswith("reject")
            if is_reject and rejected_venueid:
                print(f"\n--- Fetching up to {limit_per_bucket} from bucket "
                      f"{decision!r} via venueid={rejected_venueid} ---")
                subs = fetch_submissions(client, venue_id, limit_per_bucket,
                                         venueid=rejected_venueid)
            else:
                print(f"\n--- Fetching up to {limit_per_bucket} from bucket {venue_field!r} ---")
                subs = fetch_submissions(client, venue_id, limit_per_bucket,
                                         venue_label=venue_field)
            print(f"  got {len(subs)} submissions")
            for i, sub in enumerate(subs, 1):
                if sub.forum in completed_ids:
                    print(f"  [{i}/{len(subs)}] {sub.forum}  (already done, skipping)")
                    continue
                title = content_value(sub.content, "title", "?")[:70]
                print(f"  [{i}/{len(subs)}] {sub.forum}  {title!r}")
                paper = fetch_paper_row(client, sub, decision, rating_fields)
                # drop reject rows with no reviews so the CSV doesn't carry empty rows
                if is_reject and not paper.reviews:
                    print("    (skip: no reviews)")
                    continue
                all_papers.append(paper)
                pending_flush.append(paper)
                completed_ids.add(sub.forum)
                if len(pending_flush) >= checkpoint_every:
                    do_flush()
                time.sleep(0.2)   # be polite
        do_flush("end-of-fetch")
    except KeyboardInterrupt:
        print("\n!! Interrupted. Saving progress before exit.")
        do_flush("interrupt")
        print(f"Saved {len(all_papers)} papers to {partial_path.name}. "
              f"Re-run the same command to resume.")
        sys.exit(130)
    except Exception:
        print("\n!! Error during fetch. Saving progress before re-raising.")
        do_flush("error")
        raise

    csv_path = out_dir / f"{slug(venue_id)}_papers.csv"
    write_papers_csv(all_papers, rating_fields, csv_path)

    # dump author-id list for the author scraper to consume
    author_ids = sorted({aid for p in all_papers for aid in p.author_ids})
    ids_path = out_dir / f"{slug(venue_id)}_author_ids.json"
    ids_path.write_text(json.dumps(author_ids, indent=2), encoding="utf-8")
    print(f"Wrote {len(author_ids)} unique author ids -> {ids_path}")

    # Final CSV is the source of truth — drop the checkpoint files now that
    # the run completed cleanly. (Kept on disk only on crash/interrupt.)
    state_path.unlink(missing_ok=True)
    partial_path.unlink(missing_ok=True)

    return all_papers


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--venue", default="ICLR.cc/2026/Conference",
                    help="OpenReview venue group id")
    ap.add_argument("--limit", type=int, default=3,
                    help="Papers per decision bucket (Oral/Poster/Reject)")
    ap.add_argument("--out", default="out", help="Output directory")
    ap.add_argument("--rating-fields", nargs="*", default=None,
                    help="Manually set review rating columns instead of "
                         "auto-discovering (e.g. --rating-fields "
                         "overall_recommendation). Rarely needed.")
    ap.add_argument("--checkpoint-every", type=int, default=100,
                    help="Flush progress to disk every N papers. On crash, "
                         "re-run the same command to resume. Default 100.")
    ap.add_argument("--restart", action="store_true",
                    help="Ignore (and delete) any existing checkpoint for this "
                         "venue and start fresh.")
    args = ap.parse_args()
    run(args.venue, args.limit, Path(args.out), args.rating_fields,
        checkpoint_every=args.checkpoint_every, restart=args.restart)


if __name__ == "__main__":
    main()
