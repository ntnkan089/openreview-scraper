"""Inspect one OpenReview profile to understand the shape."""
import json
from openreview.api import OpenReviewClient

client = OpenReviewClient(baseurl="https://api2.openreview.net")
profile = client.get_profile("~Pierre-Carl_Langlais1")

print("Top-level attrs:", [a for a in dir(profile) if not a.startswith("_")])
print()
print("id:", profile.id)
print()
print("content keys:", list((profile.content or {}).keys()))
print()
for k, v in (profile.content or {}).items():
    snippet = json.dumps(v, default=str)[:300]
    print(f"--- {k} ---")
    print(snippet)
    print()
