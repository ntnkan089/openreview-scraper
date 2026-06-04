"""Probe OpenReview API v2 to understand ICLR 2026 data shape.

Goal: fetch the example paper (forum id 0wSlFpMsGb, submission 25369)
and inspect what fields are available for submission + reviews + meta-review.
"""
import json
from openreview.api import OpenReviewClient

client = OpenReviewClient(baseurl="https://api2.openreview.net")

FORUM_ID = "0wSlFpMsGb"

notes = client.get_notes(forum=FORUM_ID, details="replyCount,writable")
print(f"Total notes on forum: {len(notes)}")
print()

for n in notes:
    invitations = n.invitations or []
    sig = n.signatures[0] if n.signatures else "?"
    inv = invitations[-1] if invitations else "?"
    content_keys = list((n.content or {}).keys())
    print(f"id={n.id}  number={n.number}  sig={sig}")
    print(f"  invitation={inv}")
    print(f"  content keys: {content_keys}")
    print()

# Dump the submission note + first official review + meta review for column mapping
def is_official_review(n):
    return any("Official_Review" in inv for inv in (n.invitations or []))

def is_meta_review(n):
    return any("Meta_Review" in inv for inv in (n.invitations or []))

def is_submission(n):
    return any("Submission" in inv and "Review" not in inv and "Comment" not in inv
               for inv in (n.invitations or []))

print("\n=== SUBMISSION ===")
for n in notes:
    if is_submission(n):
        print(json.dumps({k: (v if not isinstance(v, dict) else v.get("value", v))
                          for k, v in (n.content or {}).items()}, indent=2, default=str)[:2000])
        break

print("\n=== FIRST OFFICIAL REVIEW ===")
for n in notes:
    if is_official_review(n):
        print(f"signatures: {n.signatures}")
        print(json.dumps({k: (v if not isinstance(v, dict) else v.get("value", v))
                          for k, v in (n.content or {}).items()}, indent=2, default=str)[:2000])
        break

print("\n=== META REVIEW ===")
for n in notes:
    if is_meta_review(n):
        print(f"signatures: {n.signatures}")
        print(json.dumps({k: (v if not isinstance(v, dict) else v.get("value", v))
                          for k, v in (n.content or {}).items()}, indent=2, default=str)[:2000])
        break
