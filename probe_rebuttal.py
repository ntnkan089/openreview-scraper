"""Probe rebuttal/response notes on an ICLR 2026 forum.

Goal: understand how author rebuttals are represented:
  - what invitation pattern they use
  - what signature (Authors?)
  - what `replyto` points at (review note? comment?)
  - what content field holds the text
  - how reviewer text is quoted (blockquote `>`? quotes?)
"""
import json
import sys
from openreview.api import OpenReviewClient

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

client = OpenReviewClient(baseurl="https://api2.openreview.net")

FORUM_ID = sys.argv[1] if len(sys.argv) > 1 else "0wSlFpMsGb"

notes = client.get_notes(forum=FORUM_ID)
print(f"Forum {FORUM_ID}: {len(notes)} notes\n")

by_id = {n.id: n for n in notes}

for n in notes:
    sig = n.signatures[0] if n.signatures else "?"
    invs = n.invitations or []
    inv = invs[-1] if invs else "?"
    replyto = getattr(n, "replyto", None)
    ckeys = list((n.content or {}).keys())
    print(f"id={n.id}  sig={sig}")
    print(f"  invitation={inv}")
    print(f"  replyto={replyto}", end="")
    if replyto and replyto in by_id:
        rt = by_id[replyto]
        rtsig = rt.signatures[0] if rt.signatures else "?"
        print(f"  -> ({rtsig})")
    else:
        print()
    print(f"  content keys: {ckeys}")
    print()

# Dump the first author rebuttal/response/comment in full
print("\n=== FIRST AUTHOR REPLY (full content) ===")
for n in notes:
    sig = n.signatures[0] if n.signatures else ""
    if sig.endswith("/Authors") and getattr(n, "replyto", None):
        print(f"id={n.id}  sig={sig}")
        print(f"replyto={n.replyto}")
        invs = n.invitations or []
        print(f"invitation={invs[-1] if invs else '?'}")
        for k, v in (n.content or {}).items():
            val = v.get("value", v) if isinstance(v, dict) else v
            print(f"\n--- {k} ---")
            print(str(val)[:2500])
        break
