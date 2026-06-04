"""Verification aid: for a given forum, print the venue's auto-discovered
rating fields and each reviewer's score values, plus the live OpenReview URL
so you can open the same paper in a browser and check the numbers match.

Usage:
    python verify_ratings.py ICLR.cc/2026/Conference 0wSlFpMsGb
    python verify_ratings.py ICML.cc/2025/Conference mEV0nvHcK3
"""
import sys
from openreview.api import OpenReviewClient
import scraper

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

venue_id = sys.argv[1]
forum_id = sys.argv[2]

client = OpenReviewClient(baseurl="https://api2.openreview.net")

discovered = scraper.discover_rating_fields(client, venue_id)
print(f"Venue: {venue_id}")
print(f"AUTO-DISCOVERED rating fields ({len(discovered)}): {discovered}")
print(f"Open this paper in a browser to compare:")
print(f"  https://openreview.net/forum?id={forum_id}")
print("=" * 70)

notes = client.get_notes(forum=forum_id)
for n in notes:
    if not scraper.is_official_review(n):
        continue
    handle = scraper.handle_from_signature(n.signatures[0])
    print(f"\n{handle}   (review note id={n.id})")
    # show EVERY field on the review so you can see what was kept vs ignored
    for k in (n.content or {}):
        val = scraper.content_value(n.content, k)
        if k in discovered:
            tag = "  <-- RATING (kept as a column)"
        elif k in scraper.TEXT_REVIEW_FIELDS:
            tag = "  (text -> folded into 'Review N' column)"
        else:
            tag = "  (ignored: housekeeping)"
        shown = str(val)
        if len(shown) > 60:
            shown = shown[:60] + "..."
        print(f"    {k:42} = {shown}{tag}")
