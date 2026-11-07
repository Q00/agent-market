"""At the deadline: for each GitHub account, its shop and buyer running or not, and Round 2 counted deals."""
import json
import sys
import urllib.request

M = sys.argv[1] if len(sys.argv) > 1 else "https://market.ourolabs.ai"
d = json.load(urllib.request.urlopen(f"{M}/api/board?round=2", timeout=20))
rows = {}
for role in ("shops", "buyers"):
    for a in d[role]:
        owner = (a.get("owner") or {}).get("login")
        if owner:
            rows.setdefault(owner, {"shops": [], "buyers": []})[role].append(a)


def best(agents):
    return max(agents, key=lambda a: (a["alive"], a["deals"]), default=None)


def show(a):
    return "-" if a is None else f"{a['name'][:14]} {'up' if a['alive'] else 'DOWN'}"


print(f"{'github':<20} {'shop':<22} {'buyer':<22} {'buyer R2 deals':<15} pass")
for owner, r in sorted(rows.items(), key=lambda kv: kv[0].lower()):
    shop, buyer = best(r["shops"]), best(r["buyers"])
    passed = bool(shop and shop["alive"] and buyer and buyer["alive"] and buyer["deals"] > 0)
    print(f"{owner:<20} {show(shop):<22} {show(buyer):<22} {(buyer or {}).get('deals', 0):<15} {'yes' if passed else 'no'}")
print(f"{len(rows)} GitHub account(s). up: shop card answered in the last 3 min, buyer took an order in the last 10 min")
