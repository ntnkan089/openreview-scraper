"""Figure out how to enumerate all submissions for ICLR 2026."""
from openreview.api import OpenReviewClient

client = OpenReviewClient(baseurl="https://api2.openreview.net")
VENUE = "ICLR.cc/2026/Conference"

# Try the recommended approach: filter by content.venueid
venues = client.get_group(VENUE).content
print("Group content keys:", list(venues.keys()) if venues else None)
if venues:
    for k, v in venues.items():
        if "venue" in k.lower() or "decision" in k.lower() or "submission" in k.lower():
            val = v.get("value") if isinstance(v, dict) else v
            print(f"  {k}: {str(val)[:200]}")

# Check the submission invitation
try:
    inv = client.get_invitation(f"{VENUE}/-/Submission")
    print("\nSubmission invitation found")
except Exception as e:
    print(f"\nSubmission invitation error: {e}")

# How many notes total under the venue?
print("\nFetching first page of submissions via content.venueid...")
notes = client.get_notes(content={"venueid": VENUE}, limit=5)
print(f"Got {len(notes)} (first 5)")
for n in notes[:3]:
    title = (n.content.get("title") or {}).get("value", "?")
    venue = (n.content.get("venue") or {}).get("value", "?")
    print(f"  forum={n.forum}  venue={venue!r}  title={title[:60]!r}")

# Try to count by venue label
print("\nSampling decisions...")
for label in ["ICLR 2026 Oral", "ICLR 2026 Poster", "ICLR 2026 Spotlight",
              "ICLR 2026 Submitted", "Submitted to ICLR 2026"]:
    try:
        sample = client.get_notes(content={"venue": label}, limit=1)
        print(f"  venue={label!r}: at least {len(sample)} match" + (f" (e.g. {sample[0].forum})" if sample else ""))
    except Exception as e:
        print(f"  venue={label!r}: err {e}")
