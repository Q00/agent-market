"""Wash-trading and collusion signals in one round of the market (read-only).

    python anomaly.py /var/lib/w07/market.db --round 3 [--min-deals 20] [--github]

Per shop with at least --min-deals counted deals:
  top      the counterparty GitHub account (or agent name, for agents without one) with the most deals, and its share
  excl     how much of that counterparty's buying went to this shop's account (a generous buyer that buys
           everywhere stays low; a feeder or a colluding partner is high)
  rev      that counterparty's share of the shop's revenue
  @budget  share of the top counterparty's deals priced at the order budget or 1 below (market median for comparison)
  stars    mean stars from the top counterparty vs from everyone else
  self     deals between a shop and a buyer of the same account (Round 2 and before; Round 3 refuses them at settle)
Then reciprocal pairs (A buys from B and B buys from A) and the GitHub ids of flagged counterparties (a higher id is a
newer account; --github looks up the creation date).
"""
import argparse
import collections
import json
import os
import sqlite3
import statistics
import urllib.request

ap = argparse.ArgumentParser()
ap.add_argument("db")
ap.add_argument("--round", type=int, required=True)
ap.add_argument("--min-deals", type=int, default=20)
ap.add_argument("--share", type=float, default=0.3, help="flag when one counterparty has at least this share")
ap.add_argument("--github", action="store_true", help="look up creation dates of flagged counterparty accounts")
a = ap.parse_args()

db = sqlite3.connect(f"file:{os.path.abspath(a.db)}?mode=ro", uri=True, timeout=60)
try:
    db.execute("select 1 from parties limit 1")
except sqlite3.OperationalError:                 # a WAL copy without its -shm file: read it as a frozen file
    db = sqlite3.connect(f"file:{os.path.abspath(a.db)}?mode=ro&immutable=1", uri=True)
db.row_factory = sqlite3.Row
parties = db.execute("select name, role, url, owner from parties").fetchall()
shop_of_url = {p["url"]: p for p in parties if p["role"] == "shop"}
by_name = {p["name"]: p for p in parties if p["role"] == "shop"}
if db.execute("select 1 from sqlite_master where name='shop_urls'").fetchone():   # older URLs of restarted tunnels
    for u, n in db.execute("select url, name from shop_urls"):
        if u not in shop_of_url and n in by_name:
            shop_of_url[u] = by_name[n]
buyer_of = {p["name"]: p for p in parties if p["role"] == "buyer"}
who = lambda p, name: (p["owner"] if p and p["owner"] else f"~{name}")      # ~name: an agent without a GitHub account

deals = []                                       # (shop name, shop owner, buyer name, buyer owner, price, budget)
if a.round >= 3:
    q = ("select s.shop_url, s.buyer, s.price, o.budget from settlements s join orders o on o.id=s.order_id"
         " where s.round=?")
    for r in db.execute(q, (a.round,)):
        sp, bp = shop_of_url.get(r["shop_url"]), buyer_of.get(r["buyer"])
        if sp:
            deals.append((sp["name"], who(sp, sp["name"]), r["buyer"], who(bp, r["buyer"]), r["price"], r["budget"]))
else:
    q = ("select r.shop_url, r.buyer, r.receipt, o.budget from results r join orders o on o.id=r.order_id"
         " where r.round=? and r.receipt is not null" + (" and r.verified=1" if a.round >= 2 else ""))
    for r in db.execute(q, (a.round,)):
        sp, bp = shop_of_url.get(r["shop_url"]), buyer_of.get(r["buyer"])
        price = (json.loads(r["receipt"]) or {}).get("price")
        if sp and isinstance(price, (int, float)):
            deals.append((sp["name"], who(sp, sp["name"]), r["buyer"], who(bp, r["buyer"]), price, r["budget"]))

stars = collections.defaultdict(list)            # (shop name, buyer owner) -> stars
for r in db.execute("select author, shop_url, stars from reviews where round=? and stars is not null", (a.round,)):
    sp = shop_of_url.get(r["shop_url"])
    if sp:
        stars[(sp["name"], who(buyer_of.get(r["author"]), r["author"]))].append(r["stars"])

by_shop = collections.defaultdict(list)
for d in deals:
    by_shop[d[0]].append(d)
at_budget = lambda ds: sum(1 for d in ds if d[5] and d[4] >= d[5] - 1) / len(ds)
base = statistics.median(at_budget(ds) for ds in by_shop.values() if len(ds) >= a.min_deals) if by_shop else 0

bought = collections.Counter(d[3] for d in deals)                       # deals per buyer account
pair = collections.Counter((d[3], d[1]) for d in deals)                 # (buyer account, shop account) -> deals
rows = []
for shop, ds in by_shop.items():
    if len(ds) < a.min_deals:
        continue
    owner = ds[0][1]
    top, n = collections.Counter(d[3] for d in ds).most_common(1)[0]
    excl = pair[(top, owner)] / bought[top]                             # how much of top's buying goes to this account
    rev = sum(d[4] for d in ds)
    top_rev = sum(d[4] for d in ds if d[3] == top) / rev if rev else 0
    top_atb = at_budget([d for d in ds if d[3] == top])
    s_top = stars.get((shop, top), [])
    s_rest = [x for (s, o), xs in stars.items() if s == shop and o != top for x in xs]
    self_n = sum(1 for d in ds if d[1] == d[3] and not d[1].startswith("~")) if a.round < 3 else None   # Round 3 refuses these
    flags = []
    if self_n and self_n / len(ds) >= 0.05:                             # a few strays happen; a feeder does not stray
        flags.append(f"same owner {self_n / len(ds):.0%}")
    if top != owner and not top.startswith("~") and n / len(ds) >= a.share and excl >= 0.5:
        flags.append("exclusive pair")                                  # most of the shop, and most of the buyer
        if top_atb >= max(0.5, 3 * base):
            flags.append("pair pays full budget")
        if s_top and s_rest and statistics.mean(s_top) - statistics.mean(s_rest) >= 1:
            flags.append("pair star gap")
    rows.append(dict(shop=shop, owner=owner, deals=len(ds), top=top, share=n / len(ds), excl=excl, rev=top_rev,
                     atb=top_atb, s_top=statistics.mean(s_top) if s_top else None,
                     s_rest=statistics.mean(s_rest) if s_rest else None, self=self_n, flags=flags))

rows.sort(key=lambda r: (-len(r["flags"]), -r["share"]))
print(f"Round {a.round}: {len(deals)} counted deals, {len(by_shop)} shops; market median @budget {base:.0%}\n")
print(f"{'shop':22} {'owner':14} {'deals':>6} {'top counterparty':16} {'share':>5} {'excl':>5} {'rev':>4} {'@budget':>7}"
      f" {'stars top/rest':>13} {'self':>6}  flags")
fmt = lambda x: "-" if x is None else f"{x:.2f}"
for r in rows:
    if not r["flags"]:
        continue
    print(f"{r['shop'][:22]:22} {r['owner'][:14]:14} {r['deals']:>6} {r['top'][:16]:16} {r['share']:>5.0%} {r['excl']:>5.0%}"
          f" {r['rev']:>4.0%} {r['atb']:>7.0%} {fmt(r['s_top']):>6}/{fmt(r['s_rest']):<6}"
          f" {'-' if r['self'] is None else r['self']:>6}  {', '.join(r['flags'])}")
print(f"\n{sum(1 for r in rows if not r['flags'])} shops with no flag")

# reciprocal pairs between accounts: each side's buyer bought at least 10% of its deals from the other's shops
buys = collections.defaultdict(collections.Counter)
for d in deals:
    if d[1] != d[3]:
        buys[d[3]][d[1]] += 1
pairs = []
for x, c in buys.items():
    tx = sum(c.values())
    for y, n in c.items():
        ty = sum(buys.get(y, {}).values())
        if x < y and ty and n / tx >= 0.1 and buys[y][x] / ty >= 0.1 and not x.startswith("~") and not y.startswith("~"):
            pairs.append((x, y, n, n / tx, buys[y][x], buys[y][x] / ty))
print("\nreciprocal pairs (buyer account -> shop account, both ways >= 10% of the buyer's deals):")
for x, y, n1, f1, n2, f2 in sorted(pairs, key=lambda p: -(p[3] + p[5])):
    print(f"  {x} -> {y}: {n1} ({f1:.0%})   {y} -> {x}: {n2} ({f2:.0%})")
if not pairs:
    print("  none")

flagged = {r["top"] for r in rows if r["flags"] and not r["top"].startswith("~")}
if flagged:
    print("\nGitHub ids of flagged counterparties (higher is newer):")
    ids = {u["login"]: u["gh_id"] for u in db.execute("select login, gh_id from users")}
    for login in sorted(flagged, key=lambda l: -(ids.get(l) or 0)):
        made = ""
        if a.github and ids.get(login):
            try:
                with urllib.request.urlopen(f"https://api.github.com/user/{ids[login]}", timeout=10) as f:
                    made = json.load(f).get("created_at", "")
            except Exception as e:
                made = f"lookup failed: {type(e).__name__}"
        print(f"  {login:20} id {ids.get(login)}  {made}")
