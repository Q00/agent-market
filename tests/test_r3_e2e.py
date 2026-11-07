"""Round 3 end to end: a real market process, driven over HTTP, every economy feature checked.

    .venv/bin/python tests/test_r3_e2e.py

Starts market/server.py on 127.0.0.1:8740 with a temporary database (one short-lived food category so expiry shows
up in seconds, MARKET_BREAK_WINDOW=3, MARKET_ECON_EVERY=1) and a card server on 8741 for the shops. Prints one line
per check and a summary; the exit code is the number of failed checks.
Shortcuts through the database (the test owns it): owners' claim codes, a defect flag on a unit before it is sold,
fee_at a day back for the storage fee, and low balances before bankruptcy filings.
"""
import base64
import hashlib
import json
import math
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE / "publish/scripts"))
import market_kit as kit  # noqa: E402
import maroo  # noqa: E402

PORT, CARD_PORT = 8740, 8741
BASE = f"http://127.0.0.1:{PORT}"
CARDS = f"http://127.0.0.1:{CARD_PORT}"
ADMIN = "e2e-admin"
START = 5_000_000
SHORT_SHELF = 0.004                  # hours (14.4 s) for one food category, so units expire during the test
FOOD_GRADES = [("A", (0.8, 1.0)), ("B", (0.45, 0.8)), ("C", (0.15, 0.45))]     # market/server.py
COST_BAND = {"A": (0.55, 0.75), "B": (0.40, 0.60), "C": (0.25, 0.45)}

RESULTS = []


def check(feature, name, ok, evidence=""):
    RESULTS.append((feature, name, bool(ok), str(evidence)[:160]))
    print(f"{'PASS' if ok else 'FAIL'}  [{feature}] {name}  {evidence}"[:240], flush=True)
    return ok


def canon(d):
    return json.dumps(d, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def pub(key):
    return key.address


def verify(address, sig_b64, body):
    return maroo.verify(address, sig_b64, canon(body))


class Cards(BaseHTTPRequestHandler):
    def do_GET(self):
        if not self.path.endswith("/.well-known/agent-card.json"):
            self.send_response(404)
            self.end_headers()
            return
        name = self.path.split("/")[1]
        body = json.dumps({"name": name, "description": "e2e shop", "version": "1",
                           "supportedInterfaces": [{"url": f"{CARDS}/{name}", "protocolBinding": "JSONRPC"}]}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class Agent:
    """A shop or buyer joined straight over HTTP."""

    def __init__(self, c, name, role, owner=None):
        self.c, self.name, self.role = c, name, role
        self.key = maroo.Wallet.generate()
        self.url = f"{CARDS}/{name}" if role == "shop" else None
        body = {"name": name, "role": role, "pubkey": pub(self.key), "url": self.url}
        if owner:
            body["owner"] = owner
        r = c.post(f"{BASE}/api/join", json=body)
        assert r.status_code == 200, (name, r.status_code, r.text)
        self.token = r.json()["token"]

    def get(self, path, **kw):
        return self.c.get(f"{BASE}{path}", headers={"authorization": f"Bearer {self.token}"}, **kw)

    def post(self, path, body=None):
        return self.c.post(f"{BASE}{path}", headers={"authorization": f"Bearer {self.token}"}, json=body or {})

    def inv(self):
        return self.get("/api/inventory").json()

    def supply(self, sku, qty, wait=True):
        while True:
            r = self.post("/api/supply", {"sku": sku, "qty": qty})
            if r.status_code == 429 and wait:
                time.sleep(float(r.json()["wait"]) + 0.2)
                continue
            return r


def new_order(buyer):
    """A fresh order: a null result closes the open one first."""
    r = buyer.post("/api/orders")
    o = r.json()
    if "order" in o:
        buyer.post("/api/results", {"order": o["order"], "shop": "", "receipt": None})
    while True:
        r = buyer.post("/api/orders")
        if r.status_code == 429:
            time.sleep(float(r.json()["wait"]) + 0.05)
            continue
        return r.json()


def order_where(buyer, want, tries=80):
    for _ in range(tries):
        o = new_order(buyer)
        if want(o):
            return o
    raise AssertionError("no fitting order")


def settle(shop, buyer, o, unit, sku=None, price=None, cert="unit", claim=None, warranty=None, payee=None):
    """The shop's checkout JWT and the buyer's closed mandates, sent the way the kit's ACP layer sends them."""
    sku = sku or unit["sku"]
    price = o["budget"] if price is None else price
    co = {"id": "cs_e2e", "shop": shop.name, "shop_url": shop.url, "item": sku, "price": price,
          "task": "t-" + os.urandom(6).hex(), "currency": "credits", "iat": int(time.time()), "unit": unit["unit"]}
    if cert == "unit":
        co["cert"] = unit["cert"]
    elif cert is not None:
        co["cert"] = cert
    if claim is not None:
        co["claim"] = claim
    if warranty is not None:
        co["warranty_hours"] = warranty
    jwt = kit.jwt_sign(shop.key, co)
    h = kit.b64url(hashlib.sha256(jwt.encode()).digest())
    opens = {k: hashlib.sha256(canon(v["mandate"])).hexdigest() for k, v in o["mandates"].items()}
    cm = {"vct": "mandate.checkout.1", "order": o["order"], "open_mandate": opens["checkout"],
          "checkout_jwt": jwt, "checkout_hash": h}
    pm = {"vct": "mandate.payment.1", "order": o["order"], "open_mandate": opens["payment"],
          "payee": payee or shop.url, "amount": price, "currency": "credits", "checkout_hash": h}
    sign = lambda m: {"mandate": m, "sig": base64.b64encode(buyer.key.sign(canon(m))).decode()}
    r = shop.post("/api/settle", {"order": o["order"], "checkout_jwt": jwt, "checkout_mandate": sign(cm),
                                  "payment_mandate": sign(pm)})
    return r, co


def cheapest(skus, catalog):
    return min(skus, key=lambda s: catalog[s]["list_price"])


def wait_for(fn, seconds=30, every=0.5):
    end = time.time() + seconds
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(every)
    return fn()


def percentile(vals):
    n = len(vals)
    return [(sum(w < v for w in vals) + 0.5 * (sum(w == v for w in vals) - 1)) / (n - 1) if n > 1 else 1.0 for v in vals]


def wilson(l, d):
    n = l + d
    z, p = 1.96, l / n
    return round((p + z * z / (2 * n) - z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n)) / (1 + z * z / n), 3)


def main():
    tmp = Path(tempfile.mkdtemp(prefix="r3e2e-"))
    db_path = tmp / "market.db"
    rules = json.loads((HERE / "market/categories.json").read_text(encoding="utf-8"))
    catalog_rows = {}
    import csv
    with open(HERE / "market/catalog.csv", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            catalog_rows[r["sku"]] = r
    # the cheapest food category becomes the short-lived one
    food_rules = [r for r in rules if r.get("kind") == "food"]
    def min_price(prefix):
        return min((int(x["list_price"]) for x in catalog_rows.values() if x["category"] == prefix), default=10 ** 9)
    short = min(food_rules, key=lambda r: min_price(r["prefix"]))
    short["shelf_hours"] = SHORT_SHELF
    (tmp / "categories.json").write_text(json.dumps(rules, ensure_ascii=False), encoding="utf-8")

    cards = ThreadingHTTPServer(("127.0.0.1", CARD_PORT), Cards)
    threading.Thread(target=cards.serve_forever, daemon=True).start()
    (tmp / "students.txt").write_text("Alice\nd1\nd2\n")
    env = dict(os.environ, MARKET_ADMIN_TOKEN=ADMIN, MARKET_ORDER_GAP="0", MARKET_BREAK_WINDOW="3",
               MARKET_ECON_EVERY="1", MARKET_SUPPLY_EVERY="0", MARKET_SURGE_FREE="500", MARKET_VOTE_MIN_DEALS="5", MARKET_DISLIKE_FEE_FROM="0", MARKET_STUDENTS=str(tmp / "students.txt"), MARKET_START_CAPITAL=str(START), PYTHONUNBUFFERED="1")
    for k in ("MARKET_PUBLIC", "GITHUB_CLIENT_ID", "GITHUB_CLIENT_SECRET"):
        env.pop(k, None)
    log = open(tmp / "market.log", "w")
    proc = subprocess.Popen([str(HERE / ".venv/bin/python"), str(HERE / "market/server.py"), "--port", str(PORT),
                             "--db", str(db_path), "--categories", str(tmp / "categories.json")],
                            stdout=log, stderr=subprocess.STDOUT, env=env)
    c = httpx.Client(timeout=30)
    t0 = time.time()
    try:
        assert wait_for(lambda: _up(c), 30), "market did not start"
        db = sqlite3.connect(db_path, timeout=30)
        db.row_factory = sqlite3.Row
        for login in ("alice", "bob"):
            db.execute("insert into users(login, gh_id, avatar, html, claim, created) values(?,?,?,?,?,?)",
                       (login, 1, "", "", f"claim-{login}", time.time()))
        db.commit()
        run(c, db, short)
    finally:
        proc.terminate()
        proc.wait(10)
        cards.shutdown()
        log.close()
        failed = [r for r in RESULTS if not r[2]]
        print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed in {time.time() - t0:.0f} s; "
              f"market log {tmp / 'market.log'}")
        for r in failed:
            print(f"  FAILED [{r[0]}] {r[1]}: {r[3]}")
        if not failed:
            shutil.rmtree(tmp, ignore_errors=True)
    sys.exit(len([r for r in RESULTS if not r[2]]))


def _up(c):
    try:
        return c.get(f"{BASE}/api/demand").status_code == 200
    except httpx.HTTPError:
        return False


def run(c, db, short):
    admin = {"x-admin-token": ADMIN}
    r = c.post(f"{BASE}/api/admin/round", headers=admin, json={"round": 3})
    assert r.status_code == 200, r.text
    mpub = c.get(f"{BASE}/api/market-key").json()["pubkey"]

    # ---------- 1. what is public
    F = "1 public data"
    cat_list = c.get(f"{BASE}/api/catalog").json()
    catalog = {x["sku"]: x for x in cat_list}
    check(F, "catalog lists every SKU",
          len(cat_list) == (HERE / "market/catalog.csv").read_text(encoding="utf-8").count("\n") - 1, len(cat_list))
    check(F, "catalog rows carry no rank", not any("rank" in x for x in cat_list), sorted(cat_list[0]))
    demand = c.get(f"{BASE}/api/demand").json()
    check(F, "demand carries no weights", not any("weight" in d for d in demand) and len(demand) == len({x["category"] for x in cat_list}),
          f"{len(demand)} categories, keys {sorted(demand[0])}")
    by_cat = {}
    for s, x in catalog.items():
        by_cat.setdefault(x["category"], []).append(s)
    goods_cats = [d["category"] for d in demand if d["kind"] == "goods"]
    food_cats = [d["category"] for d in demand if d["kind"] == "food" and (d["shelf_hours"] or 0) >= 720]

    # ---------- agents
    shops = {n: Agent(c, n, "shop") for n in [f"g{i}" for i in range(8)] + [f"f{i}" for i in range(8)]
             + ["d1", "d2", "d3", "d4", "d5", "poor", "exp", "bk"]}
    s_alice = Agent(c, "s-alice", "shop", owner="claim-alice")
    s_bob = Agent(c, "s-bob", "shop", owner="claim-bob")
    b1 = Agent(c, "b1", "buyer", owner="claim-alice")
    b2 = Agent(c, "b2", "buyer")
    taster = Agent(c, "taster", "buyer")

    o = new_order(b2)
    led = c.get(f"{BASE}/api/ledger", params={"after": -50}).json()
    pub_orders = [e for e in led if e["kind"] == "order" and e["data"]["order"] == o["order"]]
    keys = sorted(pub_orders[0]["data"]) if pub_orders else None
    check(F, "ledger order entry hides budget and item",
          keys == ["budget_commitment", "buyer", "expires", "order", "round"], keys)
    check(F, "the buyer's own order shows budget, acceptable SKUs and mandates",
          all(k in o for k in ("budget", "acceptable", "mandates", "market_pubkey", "category")), sorted(o))

    # ---------- 2. supply
    F = "2 supply"
    cheap_goods = cheapest([s for cat in goods_cats for s in by_cat[cat]], catalog)
    cheap_food = cheapest([s for cat in food_cats for s in by_cat[cat]], catalog)
    lots = {}
    def buy(name, sku):
        lots[name] = shops[name].supply(sku, 20)
    ts = [threading.Thread(target=buy, args=(n, cheap_goods if n.startswith("g") else cheap_food)) for n in shops
          if n[0] in "gf" and n[1:].isdigit()]
    [t.start() for t in ts]
    [t.join() for t in ts]
    check(F, "16 lots of 20 accepted", all(r.status_code == 200 for r in lots.values()),
          {n: r.status_code for n, r in lots.items() if r.status_code != 200})
    g0 = lots["g0"].json()
    check(F, "the wallet pays the sum of unit costs",
          g0["total"] == sum(u["cost"] for u in g0["units"]) and g0["balance"] == START - g0["total"]
          and shops["g0"].inv()["balance"] == START - g0["total"], f"total {g0['total']}, balance {g0['balance']}")
    goods_units = [u for n, r in lots.items() if n[0] == "g" for u in r.json()["units"]]
    food_units = [u for n, r in lots.items() if n[0] == "f" for u in r.json()["units"]]
    share = {g: sum(u["grade"] == g for u in goods_units) / len(goods_units) for g in "ABC"}
    check(F, "goods grades near A 70 / B 20 / C 10 (160 units, +-0.12)",
          abs(share["A"] - .7) < .12 and abs(share["B"] - .2) < .12 and abs(share["C"] - .1) < .12,
          {k: round(v, 2) for k, v in share.items()})
    gp = catalog[cheap_goods]["list_price"]
    bad = [u for u in goods_units if not COST_BAND[u["grade"]][0] * gp - 1 <= u["cost"] <= COST_BAND[u["grade"]][1] * gp]
    check(F, "goods cost inside its grade's band", not bad, f"list {gp}, {len(bad)} outside")
    shelf = catalog[cheap_food]["shelf_hours"]
    def drawn(u):
        left = (u["cert"]["cert"]["expires_at"] - u["cert"]["cert"]["supplied_at"]) / (shelf * 3600)
        return next((g for g, (lo, hi) in FOOD_GRADES if lo - 0.001 <= left <= hi + 0.001), "?")
    fshare = {g: sum(drawn(u) == g for u in food_units) / len(food_units) for g in "ABC"}
    check(F, "food drawn by shelf life left near A 25 / B 50 / C 25 (+-0.12)",
          abs(fshare["A"] - .25) < .12 and abs(fshare["B"] - .5) < .12 and abs(fshare["C"] - .25) < .12,
          {k: round(v, 2) for k, v in fshare.items()})
    fp = catalog[cheap_food]["list_price"]
    def near(u):                                                  # at a band edge either grade may have been drawn
        left = (u["cert"]["cert"]["expires_at"] - u["cert"]["cert"]["supplied_at"]) / (shelf * 3600)
        return [g for g, (lo, hi) in FOOD_GRADES if lo - 0.001 <= left <= hi + 0.001]
    bad = [u for u in food_units
           if not any(COST_BAND[g][0] * fp - 1 <= u["cost"] <= COST_BAND[g][1] * fp for g in near(u))]
    check(F, "food cost inside the band of its drawn grade", not bad, f"list {fp}, {len(bad)} outside")
    shown = {g: sum(u["grade"] == g for u in food_units) / len(food_units) for g in "ABC"}
    mismatch = sum(u["grade"] != drawn(u) for u in food_units)
    RESULTS.append(("2 supply", "INFO food grade shown vs drawn", True,
                    f"shown {dict((k, round(v, 2)) for k, v in shown.items())}, {mismatch}/{len(food_units)} differ"))
    print(f"INFO  [2 supply] food grade shown {shown}, {mismatch}/{len(food_units)} units show a grade other than drawn")
    r = shops["g1"].post("/api/supply", {"sku": cheap_goods, "qty": 21})
    check(F, "qty 21 refused", r.status_code == 400, r.status_code)
    r = shops["g1"].post("/api/supply", {"sku": cheap_goods, "qty": 1})
    check(F, "a second call within 10 s waits", r.status_code == 429 and "wait" in r.json(), r.text[:80])
    priciest = max(catalog, key=lambda s: catalog[s]["list_price"])
    before = shops["poor"].inv()["balance"]
    r = shops["poor"].supply(priciest, 20)
    check(F, "a lot above the balance is refused, nothing paid",
          r.status_code == 402 and shops["poor"].inv()["balance"] == before == START, r.status_code)

    # ---------- 3. certificates
    F = "3 certificates"
    u = goods_units[0]
    cert = u["cert"]
    check(F, "signed by the market key", verify(mpub, cert["sig"], cert["cert"]))
    check(F, "bound to this unit, SKU and shop",
          cert["cert"]["unit"] == u["unit"] and cert["cert"]["sku"] == u["sku"] and cert["cert"]["shop"] in ("g0", "g1", "g2", "g3", "g4", "g5", "g6", "g7"))
    forged = {"cert": dict(cert["cert"], grade="A" if cert["cert"]["grade"] != "A" else "B"), "sig": cert["sig"]}
    check(F, "an edited certificate no longer verifies", not verify(mpub, forged["sig"], forged["cert"]))
    o_fake = {"market_pubkey": mpub, "acceptable": [u["sku"]], "kind": "goods"}
    other = goods_units[1]
    check(F, "kit check_accept refuses another unit's certificate",
          kit.check_accept(o_fake, {"unit": u["unit"], "item": u["sku"], "cert": other["cert"]}, None) is not None)
    check(F, "kit check_accept takes the right certificate",
          kit.check_accept(o_fake, {"unit": u["unit"], "item": u["sku"], "cert": cert}, None) is None)

    # ---------- 4. settle
    F = "4 settle"
    d1 = shops["d1"]
    stray_sku = cheap_goods
    d1.supply(stray_sku, 1)                                       # a unit no order of this test is likely to accept
    o1 = order_where(b1, lambda o: stray_sku not in o["acceptable"]
                     and catalog[cheapest(o["acceptable"], catalog)]["list_price"] <= 300_000)
    x = cheapest(o1["acceptable"], catalog)
    d1.supply(x, 2)
    inv = d1.inv()["units"]
    ux = [u for u in inv if u["sku"] == x]
    uy = next(u for u in inv if u["sku"] == stray_sku)
    price = min(o1["budget"], catalog[x]["list_price"])

    def fails(label, key, **kw):
        r, _ = settle(kw.pop("shop", d1), kw.pop("buyer", b1), o1, kw.pop("unit", ux[0]), price=kw.pop("price", price), **kw)
        j = r.json()
        check(F, f"refused: {label}", r.status_code == 400 and j.get("settled") is False and j["checks"].get(key) is False,
              f"{r.status_code} {[k for k, v in (j.get('checks') or {}).items() if not v]}")

    fails("unit not in this shop's stock", "unit_in_stock", unit=goods_units[5], cert=None)
    fails("item outside the order's acceptable SKUs", "item_acceptable", unit=uy)
    fails("another unit's certificate", "cert_matches_unit", cert=ux[1]["cert"])
    fails("claim grade Z", "claim_and_warranty_well_formed", claim="Z")
    fails("warranty 100 h (max 72)", "claim_and_warranty_well_formed", warranty=100)
    fails("payment over the budget", "amount_in_range", price=o1["budget"] + 10)
    r, _ = settle(d1, shops["d2"], o1, ux[0], price=o1["budget"] + 10)      # signed by someone other than the order's buyer
    check(F, "without the buyer's signatures a refusal lists only the signature checks",
          r.status_code == 400 and not {"amount_in_range", "bound_to_open_mandates"} & set(r.json()["checks"]), r.text[:160])
    s_alice.supply(x, 1)
    ua = next(u for u in s_alice.inv()["units"] if u["sku"] == x)
    r, _ = settle(s_alice, b1, o1, ua, price=price)
    j = r.json()
    check(F, "refused: shop and buyer of one owner", r.status_code == 400 and j["checks"]["not_same_owner"] is False,
          [k for k, v in j["checks"].items() if not v])
    seen = [x["name"] for x in c.get(f"{BASE}/api/shops").json()["shops"]]
    mine = [x["name"] for x in b1.get("/api/shops").json()["shops"]]
    check(F, "a buyer's shop list leaves out shops of its own account", "s-alice" in seen and "s-alice" not in mine
          and len(mine) == len(seen) - 1, (seen, mine))
    bal = d1.inv()["balance"]
    true_grade = ux[0]["grade"]
    r, co = settle(d1, b1, o1, ux[0], price=price, claim=true_grade)
    j = r.json()
    mr = j.get("mandate_receipt") or {}
    check(F, "a clean deal settles", r.status_code == 200 and j["settled"] is True, r.text[:160])
    check(F, "money moves to the shop", d1.inv()["balance"] == bal + price, f"{bal} + {price}")
    check(F, "the unit leaves the shelf", ux[0]["unit"] not in {u["unit"] for u in d1.inv()["units"]})
    srow = db.execute("select * from settlements where order_id=?", (o1["order"],)).fetchone()
    check(F, "settlement row: unit, cost, grade, honest claim",
          srow and srow["unit"] == ux[0]["unit"] and srow["cost"] == ux[0]["cost"] and srow["claim_honest"] == 1,
          dict(srow) if srow else None)
    receipt = {k: v for k, v in mr.items() if k != "sig"}
    check(F, "mandate receipt signed by the market, honest claim", verify(mpub, mr.get("sig"), receipt)
          and mr.get("claim_honest") is True and mr.get("unit") == ux[0]["unit"], sorted(mr))
    led = c.get(f"{BASE}/api/ledger", params={"after": -5}).json()
    st = next(e for e in led if e["kind"] == "settle" and e["data"]["order"] == o1["order"])
    check(F, "the ledger's settle entry has no budget", "budget" not in st["data"], sorted(st["data"]))
    check(F, "the buyer's receipt carries value, the ledger does not",
          mr.get("value") == srow["value"] and "value" not in st["data"], (mr.get("value"), sorted(st["data"])))
    r, _ = settle(d1, b1, o1, ux[1], price=price)
    check(F, "the same order cannot settle twice", r.status_code == 400 and r.json()["checks"]["not_settled_before"] is False)
    rec = {"shop": d1.url, "item": x, "price": price, "task": co["task"]}
    rec["sig_seller"] = base64.b64encode(d1.key.sign(canon(rec))).decode()
    rr = b1.post("/api/results", {"order": o1["order"], "shop": d1.url, "receipt": rec, "stars": 5, "review": "ok",
                                  "sig_buyer": base64.b64encode(b1.key.sign(canon(dict({k: rec[k] for k in ("shop", "item", "price", "task")}, order=o1["order"])))).decode()})
    check(F, "the buyer's result for the settled deal verifies", rr.status_code == 200 and rr.json()["verified"] is True,
          rr.text[:200])

    # dishonest claim: settles, flagged
    d2 = shops["d2"]
    o2 = order_where(b1, lambda o: catalog[cheapest(o["acceptable"], catalog)]["list_price"] <= 300_000)
    x2 = cheapest(o2["acceptable"], catalog)
    d2.supply(x2, 1)
    u2 = d2.inv()["units"][0]
    lie = next(g for g in "ABC" if g != u2["grade"])
    r, _ = settle(d2, b1, o2, u2, price=min(o2["budget"], catalog[x2]["list_price"]), claim=lie)
    j = r.json()
    srow = db.execute("select claim, claim_honest, grade from settlements where order_id=?", (o2["order"],)).fetchone()
    check(F, "a false grade claim settles but is flagged",
          r.status_code == 200 and j["mandate_receipt"]["claim_honest"] is False and srow["claim_honest"] == 0,
          f"claimed {lie}, shipped {u2['grade']}; {r.status_code} {x2} {catalog[x2]['category']} {j.get('checks') if r.status_code != 200 else j['mandate_receipt']}")

    # ---------- 5. breakage, warranty, expiry, broken news
    F = "5 breakage/expiry"
    kb = kit.Market(BASE)
    kit.CATALOG.update(catalog)
    kb.join("buyer", "b1", b1.key)                               # the kit's own client, signed rejoin
    md_b = Path(tempfile.mkdtemp(prefix="kitb-")) / ".market"
    md_b.mkdir()
    news_state = {}
    kit.broken_news(kb, md_b, news_state)                        # starts from now
    start_seq = news_state["ledger_seq"]

    def broken_deal(shop, warranty):
        o = order_where(b1, lambda o: o.get("kind") == "goods"
                        and catalog[cheapest(o["acceptable"], catalog)]["list_price"] <= 300_000)
        sku = cheapest(o["acceptable"], catalog)
        shop.supply(sku, 1)
        u = shop.inv()["units"][0]
        db.execute("update units set defect=1 where id=?", (u["unit"],))
        db.commit()
        p = min(o["budget"], catalog[sku]["list_price"])
        r, _ = settle(shop, b1, o, u, price=p, warranty=warranty)
        assert r.status_code == 200, r.text
        return o, p, sku

    ow, pw, skw = broken_deal(shops["d3"], 1)
    on, pn, skn = broken_deal(shops["d4"], None)
    bal3_after_sale = shops["d3"].inv()["balance"]
    def both_broke():
        rows = db.execute("select * from settlements where order_id in (?,?) and broke=1", (ow["order"], on["order"])).fetchall()
        return rows if len(rows) == 2 else None
    rows = wait_for(both_broke, 20)
    rows = {r["order_id"]: r for r in rows or []}
    check(F, "defective goods break within the window", len(rows) == 2, len(rows))
    if len(rows) == 2:
        check(F, "under warranty: refunded, value 0", rows[ow["order"]]["refunded"] == 1 and rows[ow["order"]]["value"] == 0)
        check(F, "refund leaves the shop's balance", shops["d3"].inv()["balance"] == bal3_after_sale - pw,
              f"{bal3_after_sale} - {pw} -> {shops['d3'].inv()['balance']}")
        check(F, "no warranty: no refund", rows[on["order"]]["refunded"] == 0 and rows[on["order"]]["broke"] == 1)
    led = c.get(f"{BASE}/api/ledger", params={"after": -40}).json()
    br = [e["data"] for e in led if e["kind"] == "broken"]
    check(F, "ledger 'broken' entries carry refund amounts",
          sorted(b["refunded"] for b in br if b["order"] in (ow["order"], on["order"])) == sorted([0, pw]), br)
    kit.broken_news(kb, md_b, news_state)
    lines = [json.loads(x) for x in (md_b / "broken.jsonl").read_text(encoding="utf-8").splitlines()] \
        if (md_b / "broken.jsonl").exists() else []
    mine = [e for e in c.get(f"{BASE}/api/ledger", params={"after": start_seq}).json()
            if e["kind"] == "broken" and e["data"]["buyer"] == "b1"]   # earlier deals may break on their own, too
    check(F, "kit broken.jsonl: one line per broken purchase, name and broke_at only",
          len(lines) == len(mine) >= 2 and all(sorted(x) == ["broke_at", "name"] for x in lines)
          and {catalog[skw]["name"], catalog[skn]["name"]} <= {x["name"] for x in lines}, lines)

    ex = shops["exp"]
    short_sku = cheapest(by_cat[short["prefix"]], catalog)
    ex.supply(short_sku, 3)
    ids = {u["unit"] for u in ex.inv()["units"]}
    gone = wait_for(lambda: not ex.inv()["units"], 25)
    st = {r["status"] for r in db.execute(f"select status from units where id in ({','.join('?' * len(ids))})", list(ids))}
    check(F, "food past its shelf life is written off", gone and st == {"expired"}, st)
    led = c.get(f"{BASE}/api/ledger", params={"after": -60}).json()
    exp = [e["data"] for e in led if e["kind"] == "expire" and e["data"]["shop"] == "exp"]
    check(F, "ledger 'expire' entry with the lost cost", exp and sum(len(e["units"]) for e in exp) == 3, exp)

    # ---------- 6. storage fee
    F = "6 storage"
    g2 = shops["g2"]
    held = sum(u["cost"] for u in g2.inv()["units"] if u["kind"] == "goods")
    before = g2.inv()["balance"]
    day_back = time.time() - 86400
    db.execute("update wallets set fee_at=? where shop='g2'", (day_back,))
    db.commit()
    entry = wait_for(lambda: next((e for e in c.get(f"{BASE}/api/ledger", params={"after": -30}).json()
                                   if e["kind"] == "storage" and e["data"]["shop"] == "g2"), None), 15)
    fee = entry["data"]["fee"] if entry else None
    check(F, "1% a day of the goods' cost", entry and entry["data"]["goods_cost"] == held
          and abs(fee - held * 0.01) <= held * 0.01 * 0.002 + 1, f"held {held}, fee {fee}")
    check(F, "the fee leaves the balance", fee is not None and g2.inv()["balance"] == before - fee)
    f0 = shops["f0"]
    db.execute("update wallets set fee_at=? where shop='f0'", (day_back,))
    db.commit()
    time.sleep(3)
    check(F, "food in stock pays nothing", f0.inv()["balance"] == START - lots["f0"].json()["total"])

    # ---------- 7. bankruptcy
    F = "7 bankruptcy"
    bk = shops["bk"]
    r = bk.post("/api/bankruptcy")
    check(F, "refused above the line", r.status_code == 409, r.text[:100])
    bk.supply(cheap_goods, 5)
    db.execute("update wallets set balance=100000 where shop='bk'")
    db.commit()
    r = bk.post("/api/bankruptcy")
    j = r.json()
    i1 = bk.inv()
    seized = db.execute("select count(*) from units where shop='bk' and status='seized'").fetchone()[0]
    check(F, "first filing: stock seized, balance reset, money counted",
          r.status_code == 200 and j["seized_units"] == 5 == seized and i1["balance"] == START and not i1["units"]
          and i1["injected"] == START - 100000 and i1["bankruptcies_left"] == 1, j)
    db.execute("update wallets set balance=200000 where shop='bk'")
    db.commit()
    r = bk.post("/api/bankruptcy")
    i2 = bk.inv()
    check(F, "second filing adds to injected", r.status_code == 200 and i2["injected"] == 2 * START - 300000
          and i2["bankruptcies_left"] == 0, i2["injected"])
    db.execute("update wallets set balance=100000 where shop='bk'")
    db.commit()
    r = bk.post("/api/bankruptcy")
    check(F, "third filing refused", r.status_code == 409, r.text[:100])

    # ---------- 4b. one more clean deal so b1 reaches 5 settled deals (a voter whose votes count)
    d5 = shops["d5"]
    o5 = order_where(b1, lambda o: catalog[cheapest(o["acceptable"], catalog)]["list_price"] <= 300_000)
    x5 = cheapest(o5["acceptable"], catalog)
    d5.supply(x5, 1)
    r, _ = settle(d5, b1, o5, d5.inv()["units"][0], price=min(o5["budget"], catalog[x5]["list_price"]))
    n_b1 = db.execute("select count(*) from settlements where buyer='b1'").fetchone()[0]
    check("4 settle", "b1 has 5 settled deals", r.status_code == 200 and n_b1 == 5, n_b1)

    # ---------- 8. forum
    F = "8 forum"
    seq_settle = next(e["seq"] for e in c.get(f"{BASE}/api/ledger", params={"after": 0}).json() if e["kind"] == "settle")
    r = s_bob.post("/api/forum", {"text": "huge ref", "refs": [2 ** 70]})
    check(F, "a ref too large for the database is refused", r.status_code == 400, r.status_code)
    r = s_bob.post("/api/forum", {"text": "s-bob: 정직하게 팝니다", "refs": [seq_settle, 9_999_999]})
    p_bob = r.json().get("post")
    check(F, "post accepted", r.status_code == 200 and p_bob, r.text[:100])
    r = s_bob.post("/api/forum", {"text": "again"})
    check(F, "second post within 5 min waits", r.status_code == 429 and r.json()["wait"] > 290, r.text[:80])
    r = b2.post("/api/forum", {"text": "x" * 2001})
    check(F, "2,001 characters refused", r.status_code == 400, r.status_code)
    r = b2.post("/api/forum", {"text": "y" * 2000})
    check(F, "2,000 characters accepted", r.status_code == 200, r.status_code)
    p_alice = s_alice.post("/api/forum", {"text": "s-alice: 우리 가게 최고"}).json()["post"]
    r = s_bob.post("/api/forum/vote", {"post": p_bob, "value": 1})
    check(F, "no votes on one's own post", r.status_code == 400)
    r1 = b1.post("/api/forum/vote", {"post": p_bob, "value": 1})
    r2 = taster.post("/api/forum/vote", {"post": p_bob, "value": -1})
    r3 = b1.post("/api/forum/vote", {"post": p_alice, "value": 1})
    check(F, "an eligible voter counts", r1.json().get("counted") is True, r1.text)
    check(F, "a voter with < 5 settled deals does not count", r2.json().get("counted") is False, r2.text)
    check(F, "a vote on a post of the same owner does not count", r3.json().get("counted") is False, r3.text)
    posts = {p["id"]: p for p in c.get(f"{BASE}/api/forum").json()}
    pb = posts[p_bob]
    check(F, "tally counts only eligible votes; score = Wilson",
          (pb["likes"], pb["dislikes"]) == (1, 0) and pb["score"] == wilson(1, 0)
          and (posts[p_alice]["likes"], posts[p_alice]["dislikes"]) == (0, 0) and posts[p_alice]["score"] is None,
          (pb["likes"], pb["dislikes"], pb["score"]))
    refs = pb["refs"]
    check(F, "refs resolve against the ledger", refs[0]["found"] and refs[0]["kind"] == "settle"
          and refs[1] == {"seq": 9_999_999, "found": False}, refs)
    check(F, "the market posted the round change",
          any(p["role"] == "market" and "Round 3" in p["text"] for p in posts.values()))
    sys.path.insert(0, str(HERE / "market"))
    import server as srv
    ps = srv.post_score
    check(F, "Wilson orders by evidence", ps(0, 0) is None and ps(1, 0) < ps(10, 0) and ps(10, 10) < ps(10, 0)
          and ps(3, 1) < ps(30, 10), (ps(1, 0), ps(10, 0), ps(10, 10), ps(3, 1), ps(30, 10)))
    r = c.post(f"{BASE}/api/admin/forum-hide", headers={"x-admin-token": ADMIN}, json={"post": p_alice})
    check(F, "admin hide removes the post", r.status_code == 200
          and p_alice not in {p["id"] for p in c.get(f"{BASE}/api/forum").json()})
    r = c.post(f"{BASE}/api/admin/forum-hide", json={"post": p_bob})
    check(F, "hide needs the admin token", r.status_code == 403)

    # ---------- 11. kit v4
    F = "11 kit"
    kk = kit.Market(BASE)
    kkey = maroo.Wallet.generate()
    kk.join("buyer", "kit-buyer", kkey)
    md = Path(tempfile.mkdtemp(prefix="kito-")) / ".market"
    md.mkdir()
    (md / "outbox.jsonl").write_text(
        json.dumps({"post": "kit에서 올린 글, 김치 구합니다", "as": "buyer"}, ensure_ascii=False) + "\n"
        + json.dumps({"vote": p_bob, "value": 1}) + "\nnot json\n", encoding="utf-8")
    kit.outbox(kk, md)
    sent = [json.loads(x) for x in (md / "outbox.sent.jsonl").read_text(encoding="utf-8").splitlines()]
    check(F, "outbox: post and vote delivered, bad line reported",
          [s.get("status") for s in sent[:2]] == [200, 200] and sent[2].get("error") == "not JSON"
          and not (md / "outbox.jsonl").exists(), [s.get("status", s.get("error")) for s in sent])
    check(F, "the kit's post is on the forum",
          any(p["author"] == "kit-buyer" for p in c.get(f"{BASE}/api/forum").json()))
    (md / "outbox.jsonl").write_text(json.dumps({"post": "second"}) + "\n", encoding="utf-8")
    kit.outbox(kk, md)
    check(F, "a post inside the 5 min gap stays in the outbox",
          (md / "outbox.jsonl").exists() and "second" in (md / "outbox.jsonl").read_text())

    ks = kit.Market(BASE)
    skey = maroo.Wallet.generate()
    ks.join("shop", "kit-shop", skey, url=f"{CARDS}/kit-shop")
    sd = Path(tempfile.mkdtemp(prefix="kits-"))
    kit.SYNC_EVERY, kit.RESTOCK_EVERY = 1, 0
    kit.CATALOG.clear()                                          # a fresh kit process fetches and writes it
    threading.Thread(target=kit.sync_loop, args=(None, {}, sd, ks, {}), daemon=True).start()
    smd = sd / ".market"
    ok = wait_for(lambda: all((smd / f).exists() for f in ("inventory.json", "catalog.json", "demand.json", "forum.json")), 20)
    check(F, "sync writes inventory, catalog, demand and forum files", ok,
          sorted(p.name for p in smd.iterdir()) if smd.exists() else None)
    stocked = wait_for(lambda: json.loads((smd / "inventory.json").read_text(encoding="utf-8"))["units"]
                       if (smd / "inventory.json").exists() else None, 30)
    check(F, "default restock stocks an empty shop", bool(stocked), len(stocked or []))
    bad = []
    for bal in (50_000, 1_000_000, 5_000_000):
        for _ in range(20):
            for b in kit.default_restock({"units": [], "balance": bal}, demand):
                if not (1 <= b["qty"] <= 2 and b["qty"] * catalog[b["sku"]]["list_price"] * 0.75 <= bal * 0.1 + 1e-6):
                    bad.append((bal, b))
    check(F, "default restock spends at most 10% of the balance per SKU, at most 2 units", not bad, bad[:3])
    check(F, "default restock stays put with 20+ units",
          kit.default_restock({"units": [{}] * 20, "balance": 10 ** 7}, demand) == [])

    kb2 = kit.Market(BASE)
    k3 = maroo.Wallet.generate()
    kb2.join("shop", "kit-broke", k3, url=f"{CARDS}/kit-broke")
    kb2.call("GET", "/api/inventory", "shop")
    db.execute("update wallets set balance=100000 where shop='kit-broke'")
    db.commit()
    inv3 = kb2.call("GET", "/api/inventory", "shop").json()
    bd = Path(tempfile.mkdtemp(prefix="kitk-"))
    (bd / ".market").mkdir()
    kit.restock({}, bd, kb2, bd / ".market", inv3, demand)
    inv3b = kb2.call("GET", "/api/inventory", "shop").json()
    check(F, "a stuck shop with nothing to sell files for bankruptcy by itself",
          inv3b["bankruptcies"] == 1 and inv3b["injected"] == START - 100000, inv3b["bankruptcies"])

    # ---------- 9. rankings
    F = "9 rankings"
    def board_ready():
        j = c.get(f"{BASE}/api/board", params={"round": 3}).json()
        s = {x["name"]: x for x in j["shops"]}
        return j if s.get("d5", {}).get("deals") == 1 and s.get("bk", {}).get("bankruptcies") == 2 else None
    bj = wait_for(board_ready, 30, 1)
    check(F, "board shows the round's settlements", bool(bj))
    if bj:
        ranked = [s for s in bj["shops"] if s["deals"] or s["profit"]]
        pa, pr, pst = (percentile([s[k] for s in ranked]) for k in ("book", "revenue", "stars_rank"))
        bad = [(s["name"], s["all"], round(0.5 * a + 0.3 * b + 0.2 * cc, 3)) for s, a, b, cc in zip(ranked, pa, pr, pst)
               if abs(s["all"] - round(0.5 * a + 0.3 * b + 0.2 * cc, 3)) > 0.0011]
        check(F, "shop ALL = 0.5 book + 0.3 revenue + 0.2 stars (percentiles)", ranked and not bad, bad[:3])
        bks = next(s for s in bj["shops"] if s["name"] == "bk")
        w = db.execute("select * from wallets where shop='bk'").fetchone()
        check(F, "profit = balance - start - injected", bks["profit"] == w["balance"] - w["start"] - w["injected"],
              f"{bks['profit']} = {w['balance']} - {w['start']} - {w['injected']}")
        d3 = next(s for s in bj["shops"] if s["name"] == "d3")
        check(F, "revenue leaves out refunded deals", d3["revenue"] == 0 and d3["refunds"] == 1, (d3["revenue"], d3["refunds"]))
        rb = [b for b in bj["buyers"] if b["deals"]]
        qa, qd, qp = (percentile([b[k] or 0 for b in rb]) for k in ("saved", "deals", "posts"))
        qf = [b["forum"] or 0 for b in rb]
        bad = [(b["name"], b["all"]) for b, a, f_, d, p_ in zip(rb, qa, qf, qd, qp)
               if abs(b["all"] - round(0.5 * a + 0.3 * f_ + 0.1 * d + 0.1 * p_, 3)) > 0.0011]
        check(F, "buyer ALL = 0.5 saved + 0.3 forum rating + 0.1 deals + 0.1 posts (forum as the rating itself)", rb and not bad, bad[:3])
        b1row = next(b for b in bj["buyers"] if b["name"] == "b1")
        exp_surplus = sum((s["value"] or 0) - s["price"] + (s["price"] if s["refunded"] else 0)
                          for s in db.execute("select * from settlements where buyer='b1'"))
        check(F, "buyer surplus = value - price + refunds", b1row["surplus"] == exp_surplus, (b1row["surplus"], exp_surplus))
        alls = {}
        for kind in ("shops", "buyers"):
            for x in bj[kind]:
                if x.get("all") is not None:
                    key = (x["owner"] or {}).get("login") or x["name"]
                    alls[key] = round(alls.get(key, 0) + x["all"], 3)
        got = {r["player"]: r["total"] for r in bj.get("players", [])}
        check(F, "players: one row per account, total = shop ALL + buyer ALL, best first",
              got == alls and [r["total"] for r in bj["players"]] == sorted(got.values(), reverse=True),
              (sorted(got.items())[:4], sorted(alls.items())[:4]))

    # ---------- 10. hidden taste
    F = "10 taste"
    for _ in range(200):
        new_order(taster)
    taste = json.loads(db.execute("select categories from tastes where buyer='taster'").fetchone()[0])
    cats = [r[0] for r in db.execute("select category from orders where buyer='taster' and category is not null")]
    sh = sum(x in taste for x in cats) / len(cats)
    check(F, "4 hidden favourite categories", len(taste) == 4 and len(set(taste)) == 4, taste)
    check(F, "about 70% or more of orders from the favourites", 0.6 <= sh <= 0.95, f"{sh:.2f} of {len(cats)}")
    pub_t = [e for e in c.get(f"{BASE}/api/ledger", params={"after": -50}).json() if e["kind"] == "order"]
    check(F, "favourites never reach the ledger", all("category" not in e["data"] and "item" not in e["data"] for e in pub_t))

    # ---------- results above 1,000,000
    F = "4 settle"
    ob = new_order(b2)
    big = {"shop": d1.url, "item": "x", "price": 1_500_000, "task": "t-big"}
    r = b2.post("/api/results", {"order": ob["order"], "shop": d1.url, "receipt": big})
    check(F, "a result with a price above 1,000,000 is taken (51 SKUs can cost more)", r.status_code == 200, r.text[:120])

    # ---------- surge: a SKU the whole market has been buying costs more
    F = "13 surge"
    d5 = shops["d5"]
    fresh = sorted((s for s, i in catalog.items() if i["kind"] == "goods" and i["list_price"] < 20000),
                   key=lambda s: catalog[s]["list_price"])
    hot, calm = fresh[-1], fresh[-2]
    now = time.time()
    db.executemany("insert into units(id, shop, sku, kind, cost, supplied_at, status) values(?,?,?,?,?,?,?)",
                   [(f"u-hot-{i}", "nobody", hot, "goods", 1, now, "sold") for i in range(800)])   # free 500 + 300
    db.commit()
    r = d5.supply(hot, 1)
    lp = catalog[hot]["list_price"]
    check(F, "300 units over the free limit: the cost is x1.5", r.status_code == 200 and r.json()["surge"] == 1.5
          and 0.25 * lp * 1.5 - 1 <= r.json()["units"][0]["cost"] <= 0.75 * lp * 1.5, r.text[:120])
    r = d5.supply(calm, 1)
    check(F, "a SKU nobody buys keeps the plain cost", r.status_code == 200 and r.json()["surge"] == 1.0, r.text[:120])
    time.sleep(10.5)                                              # surge_list is cached for 10 s
    rows = c.get(f"{BASE}/api/supply-prices").json()["surge"]
    check(F, "the public list and the inventory name the hot SKU, not the calm one",
          [x["sku"] for x in rows] == [hot] and [x["sku"] for x in d5.inv()["surge"]] == [hot], rows[:2])

    # ---------- players: an account's idle agent must not erase its ranked one
    F = "14 players"
    Agent(c, "s-alice-idle", "shop", owner="claim-alice")
    deadline = time.time() + 40
    while time.time() < deadline:
        bj2 = c.get(f"{BASE}/api/board").json()
        if any(s["name"] == "s-alice-idle" for s in bj2["shops"]):
            break
        time.sleep(1)
    row = next((p for p in bj2["players"] if p["player"] == "alice"), None)
    check(F, "an idle agent joined last leaves the account's row on its ranked shop",
          row is not None and row["shop"] and row["shop"]["name"] == "s-alice", row)
    listed = [p for p in bj2["players"] if p.get("student")]
    check(F, "only accounts on the student list get a class rank, matched without case",
          {p["player"] for p in listed} <= {"alice", "d1", "d2"} and any(p["player"] == "alice" for p in listed)
          and all("student_pct" not in p for p in bj2["players"] if not p.get("student")), [p["player"] for p in listed])
    want = dict(zip([p["player"] for p in listed], percentile([p["student_total"] for p in listed])))
    check(F, "class percentile is taken among the students alone",
          all(abs(p["student_pct"] - want[p["player"]]) < 0.0011 for p in listed), want)
    r = c.post(f"{BASE}/api/admin/notice", json={"text": "hello"})
    check(F, "a notice needs the admin token", r.status_code == 403, r.status_code)
    r = c.post(f"{BASE}/api/admin/notice", headers={"x-admin-token": ADMIN}, json={"text": "Patch notice"})
    last_post = c.get(f"{BASE}/api/forum", params={"limit": 1}).json()[-1]
    check(F, "the notice appears on the forum as the market", r.status_code == 200
          and last_post["author"] == "market" and last_post["text"] == "Patch notice", last_post)
    bk2 = next(s for s in bj2["shops"] if s["name"] == "d5")
    check(F, "book = cash + the cost of unsold stock", bk2["book"] == bk2["profit"] + bk2["stock_cost"] and bk2["stock_units"] >= 1,
          (bk2["book"], bk2["profit"], bk2["stock_cost"]))

    # ---------- one account, one stance on an author; a dislike burst across authors costs money
    F = "15 forum voices"
    def buyer_post(author, owner="o-x"):
        cur = db.execute("insert into forum(ts, round, author, role, owner, thread, text, refs) values(?,3,?,'buyer',?,null,'x','[]')",
                         (time.time(), author, owner))
        db.commit()
        return cur.lastrowid
    px = [buyer_post(f"x{i}") for i in range(1, 7)]
    extra = buyer_post("x1")
    cash = lambda: db.execute("select sum(w.balance) from wallets w join parties s on s.name=w.shop where s.owner='alice'").fetchone()[0]
    start_cash = cash()
    rs = [b1.post("/api/forum/vote", {"post": p, "value": -1}).json() for p in px[:3]]
    check(F, "the first three dislikes in the window are free", all(x["fee"] == 0 for x in rs) and cash() == start_cash
          and rs[-1]["next_dislike_fee"] == 20000, rs)
    r = b1.post("/api/forum/vote", {"post": extra, "value": -1}).json()
    check(F, "another dislike on an author already disliked costs nothing", r["fee"] == 0 and r["next_dislike_fee"] == 20000, r)
    r = b1.post("/api/forum/vote", {"post": px[3], "value": -1}).json()
    check(F, "the fourth author costs 20000 from the account's shop", r["fee"] == 20000 and cash() == start_cash - 20000
          and r["next_dislike_fee"] == 40000, (r, cash() - start_cash))
    r = b1.post("/api/forum/vote", {"post": px[4], "value": 1}).json()
    check(F, "likes are free", r["fee"] == 0, r)
    kept = {x[0]: x[1] for x in db.execute("select shop, balance from wallets where shop in (select name from parties where owner='alice')")}
    db.execute("update wallets set balance=0 where shop in (select name from parties where owner='alice')")
    db.commit()
    r = b1.post("/api/forum/vote", {"post": px[5], "value": -1})
    check(F, "without the cash the dislike is refused and not recorded", r.status_code == 402 and r.json()["fee"] == 40000
          and db.execute("select count(*) from votes where post=?", (px[5],)).fetchone()[0] == 0, r.text)
    for shop, bal in kept.items():
        db.execute("update wallets set balance=? where shop=?", (bal, shop))
    db.commit()
    sib = Agent(c, "s-alice-sib", "shop", owner="claim-alice")
    px2 = buyer_post("x7")
    px3 = buyer_post("x7")
    sib.post("/api/forum/vote", {"post": px2, "value": 1})
    r = b1.post("/api/forum/vote", {"post": px3, "value": -1}).json()
    check(F, "a like from a sibling without enough deals does not hide a dislike from the fee", r["fee"] == 40000, r)
    px4 = buyer_post("x8")
    n_rows = db.execute("select count(*) from dislike_fees").fetchone()[0]
    r = taster.post("/api/forum/vote", {"post": px4, "value": -1}).json()
    check(F, "a voter short of the deals is on the fee window too, since its vote counts later",
          r["counted"] is False and db.execute("select count(*) from dislike_fees").fetchone()[0] == n_rows + 1, r)
    px5 = buyer_post("x9")
    db.execute("update forum set hidden=1 where id=?", (px5,))
    db.commit()
    r = b1.post("/api/forum/vote", {"post": px5, "value": -1}).json()
    check(F, "a dislike on a hidden post costs nothing", r["fee"] == 0 and db.execute("select count(*) from dislike_fees").fetchone()[0] == n_rows + 1, r)
    # ---------- more than one vote on an author, priced by the square
    pow_t = buyer_post("y1"); pow_t2 = buyer_post("y1"); pow_t3 = buyer_post("y2"); pow_t4 = buyer_post("y3"); pow_t5 = buyer_post("y4")
    db.execute("delete from dislike_fees"); db.commit()
    bal = cash()
    def book_of():
        return db.execute("select max(w.balance - w.start - w.injected + coalesce((select sum(u.cost) from units u where u.shop=w.shop and u.status='stock'), 0))"
                          " as b from wallets w join parties s on s.name=w.shop and s.role='shop' where s.owner='alice'").fetchone()[0]
    unit = max(20000, int(0.03 * book_of()))
    r = b1.post("/api/forum/vote", {"post": pow_t, "value": -4}).json()
    check(F, "-4 buys 4 votes for unit x 3^2, and opening the stance stays free", r["fee"] == unit * 9 and r["power_fee"] == unit * 9
          and r["votes_on_author"] == 4 and cash() == bal - unit * 9, (r, unit, cash() - bal))
    bal = cash(); unit = max(20000, int(0.03 * book_of()))
    r = b1.post("/api/forum/vote", {"post": pow_t2, "value": -6}).json()
    check(F, "going from 4 to 6 pays only the difference", r["power_fee"] == unit * (25 - 9) and r["votes_on_author"] == 6 and cash() == bal - unit * 16, (r, unit))
    bal = cash()
    r = b1.post("/api/forum/vote", {"post": pow_t, "value": -2}).json()
    check(F, "asking for fewer votes than held costs nothing and refunds nothing", r["fee"] == 0 and r["votes_on_author"] == 6 and cash() == bal, r)
    vr = db.execute("select value from votes where post=? and voter='buyer:b1'", (pow_t,)).fetchone()
    check(F, "the post itself still records one dislike", vr["value"] == -1, dict(vr))
    db.execute("update wallets set balance=balance+100000000 where shop=(select name from parties where role='shop' and owner='alice' limit 1)"); db.commit()
    big = book_of()
    check(F, "the unit is 3% of the account's largest Book", srv.power_unit(db, {"owner": "alice", "role": "buyer", "name": "b1"}) == int(0.03 * big), big)
    db.execute("update wallets set balance=balance-100000000 where shop=(select name from parties where role='shop' and owner='alice' limit 1)"); db.commit()
    r = b1.post("/api/forum/vote", {"post": pow_t3, "value": -(10 ** 4)})
    check(F, "more than the limit is refused", r.status_code == 400, r.status_code)
    r = taster.post("/api/forum/vote", {"post": pow_t3, "value": -3})
    check(F, "a voter short of the deals cannot buy votes", r.status_code == 403 and db.execute("select count(*) from dislike_power where author='y2'").fetchone()[0] == 0, r.status_code)
    r = b1.post("/api/forum/vote", {"post": pow_t3, "value": -(10 ** 3)})
    check(F, "without the cash the votes are refused and nothing changes", r.status_code == 402
          and db.execute("select count(*) from votes where post=? and voter='buyer:b1'", (pow_t3,)).fetchone()[0] == 0, r.status_code)
    r = b1.post("/api/forum/vote", {"post": pow_t4, "value": 1.5})
    check(F, "a fractional value is refused", r.status_code == 400, r.status_code)
    r = b1.post("/api/forum/vote", {"post": pow_t4, "value": True})
    check(F, "a boolean value is refused", r.status_code == 400, r.status_code)
    db.execute("update forum set hidden=1 where id=?", (pow_t5,)); db.commit()
    r = b1.post("/api/forum/vote", {"post": pow_t5, "value": -3})
    check(F, "votes on a hidden post are refused", r.status_code == 400, r.status_code)
    mem = sqlite3.connect(":memory:")
    mem.row_factory = sqlite3.Row
    mem.executescript("""create table votes(post int, voter text, owner text, value int, ts real);
        create table forum(id integer primary key, round int, author text, role text, owner text, hidden int default 0);
        create table settlements(buyer text, shop_url text);
        create table reviews(round int, author text, shop_url text, stars int);
        create table parties(name text, role text, owner text);
        create table dislike_power(round int, account text, author text, power int, paid int, ts real);""")
    for i in range(1, 301):
        mem.execute("insert into forum values(?,3,'t','buyer','o-t',0)", (i,))
        mem.execute("insert into votes values(?, 'buyer:bomb', 'o-bomb', -1, 0)", (i,))
    for i in range(1, 9):
        mem.execute("insert into votes values(?, 'buyer:fan', 'o-fan', 1, 0)", (i,))
        mem.execute("insert into votes values(?, 'buyer:plain', null, 1, 0)", (i,))
        mem.execute("insert into votes values(?, 'buyer:mine', 'o-t', 1, 0)", (i,))
    for voter in ("bomb", "fan", "plain", "mine"):
        mem.executemany("insert into settlements values(?, 'u')", [(voter,)] * 50)
    mem.execute("insert into votes values(1, 'buyer:new', 'o-new', -1, 0)")
    got = srv.forum_ratings(mem, 3).get(("buyer", "t"))
    check(F, "300 dislikes are one voice; 8 likes weigh 4; no owner weighs 1; same owner and new voters are out",
          got == srv.post_score(4 + 1, 1), (got, srv.post_score(5, 1)))
    mem.execute("insert into dislike_power values(3, 'o-bomb', 't', 7, 0, 0)")
    got = srv.forum_ratings(mem, 3).get(("buyer", "t"))
    check(F, "votes paid for count on the rating", got == srv.post_score(5, 7), (got, srv.post_score(5, 7)))
    mem.execute("delete from votes where voter='buyer:bomb'")
    mem.execute("insert into votes values(1, 'buyer:bomb', 'o-bomb', 1, 0)")
    got = srv.forum_ratings(mem, 3).get(("buyer", "t"))
    check(F, "votes paid for sleep while the account likes that author", got == srv.post_score(6, 0), (got, srv.post_score(6, 0)))
    mem.execute("delete from dislike_power")
    mem.executemany("insert into reviews values(3, ?, 'u', ?)", [("bomb", 1)] * 100 + [("fan", 5), ("fan", 3)])
    mem.execute("insert into parties values('bomb', 'buyer', 'o-bomb')")
    check(F, "a shop's stars take one mean per reviewing account", sorted(srv.star_voices(mem, 3, {})["u"]) == [1.0, 4.0],
          srv.star_voices(mem, 3, {}))
    sofa = {"value": 1000000, "price": 700000, "refunded": 0, "buyer_fee": 0}
    snack = {"value": 1000, "price": 700, "refunded": 0, "buyer_fee": 0}
    broke = {"value": 0, "price": 700, "refunded": 0, "buyer_fee": 0}
    back = {"value": 0, "price": 700, "refunded": 1, "buyer_fee": 0}
    check(F, "a buyer's fee counts against what it saved",
          srv.saved_of([dict(sofa, buyer_fee=100000)] * 100) == 0.1, srv.saved_of([dict(sofa, buyer_fee=100000)] * 100))
    check(F, "a sofa and a snack at 30% off score the same", srv.saved_of([sofa] * 100) == srv.saved_of([snack] * 100) == 0.15,
          srv.saved_of([sofa] * 100))
    check(F, "unserved goods cost the whole deal, a refund returns it to zero",
          srv.saved_of([broke] * 100) == -0.5 and srv.saved_of([back] * 100) == 0.0, (srv.saved_of([broke] * 100), srv.saved_of([back] * 100)))

    # ---------- a tunnel restart: the shop joins again from a new URL
    F = "12 new URL"
    d5 = shops["d5"]
    old_url, new_url, ts = d5.url, f"{CARDS}/d5-tunnel2", int(time.time())
    body = {"name": "d5", "role": "shop", "pubkey": pub(d5.key), "url": new_url, "ts": ts}
    body["sig"] = base64.b64encode(d5.key.sign(canon({"name": "d5", "role": "shop", "url": new_url, "ts": ts}))).decode()
    r = c.post(f"{BASE}/api/join", json=body)
    check(F, "the shop joins again from a new URL", r.status_code == 200, r.text[:120])
    moved = db.execute("select count(*) from settlements where shop_url=?", (new_url,)).fetchone()[0]
    left = db.execute("select count(*) from settlements where shop_url=?", (old_url,)).fetchone()[0]
    check(F, "this round's settlements move to the new URL", moved == 1 and left == 0, (moved, left))
    urls = {u for (u,) in db.execute("select url from shop_urls where name='d5'")}
    check(F, "shop_urls keeps both URLs", urls == {old_url, new_url}, urls)
    def still_there():
        j = c.get(f"{BASE}/api/board", params={"round": 3}).json()
        s = {x["name"]: x for x in j["shops"]}
        return s.get("d5") if s.get("d5", {}).get("deals") == 1 else None
    check(F, "the board still counts the shop's deal", bool(wait_for(still_there, 20, 1)))


if __name__ == "__main__":
    main()
