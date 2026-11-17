"""Persona customers on the market, without a model: their own orders, paid in credits, inside a daily allowance.

    .venv/bin/python tests/test_customers.py

A real market process in Round 4 with MARKET_PERSONA_DAILY=10000. A shop stocks a unit and signs a checkout; a buyer
labelled "persona" opens its own order for that SKU and price and settles with no chain transaction. The exit code is
the number of failed checks.
"""
import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE / "publish/scripts"))
import maroo  # noqa: E402
import market_kit as kit  # noqa: E402

PORT = 8780
BASE = f"http://127.0.0.1:{PORT}"
ADMIN = "cust-admin"
RESULTS = []


def check(name, ok, evidence=""):
    RESULTS.append((name, bool(ok)))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {str(evidence)[:240]}", flush=True)


def canon(d):
    return json.dumps(d, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def join(c, name, role, url=None):
    key = maroo.Wallet.generate()
    r = c.post(f"{BASE}/api/join", json={"name": name, "role": role, "pubkey": key.address, "url": url},
               headers={"x-admin-token": ADMIN})
    assert r.status_code == 200, r.text
    return key, {"authorization": f"Bearer {r.json()['token']}"}


def main():
    tmp = Path(tempfile.mkdtemp(prefix="cust-"))
    env = dict(os.environ, MARKET_ADMIN_TOKEN=ADMIN, MARKET_SUPPLY_EVERY="0", MARKET_PERSONA_DAILY="10000",
               MAROO_RPC_URL="http://127.0.0.1:9")            # no chain: a customer's deal must not need one
    for k in ("MARKET_PUBLIC", "GITHUB_CLIENT_ID", "GITHUB_CLIENT_SECRET", "MARKET_BASE_URL"):
        env.pop(k, None)
    proc = subprocess.Popen([sys.executable, str(HERE / "market/server.py"), "--port", str(PORT), "--db", str(tmp / "m.db")],
                            stdout=open(tmp / "market.log", "w"), stderr=subprocess.STDOUT, env=env)
    c = httpx.Client(timeout=30)
    try:
        for _ in range(120):
            try:
                if c.get(f"{BASE}/api/demand").status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.25)
        c.post(f"{BASE}/api/admin/round", json={"round": 4}, headers={"x-admin-token": ADMIN})
        shop_url = "http://127.0.0.1:9/shop"                     # never fetched in this test
        # a shop joins with a card URL the market cannot fetch: skip the card check by joining over the admin path
        skey = maroo.Wallet.generate()
        db = tmp / "m.db"
        r = c.post(f"{BASE}/api/join", json={"name": "s1", "role": "shop", "pubkey": skey.address, "url": shop_url},
                   headers={"x-admin-token": ADMIN})
        if r.status_code != 200:                                 # the card fetch fails: insert the shop directly
            import sqlite3
            con = sqlite3.connect(db)
            con.execute("insert into parties(token, name, role, url, pubkey, joined, alive) values(?,?,?,?,?,?,?)",
                        ("shop-token", "s1", "shop", shop_url, skey.address, time.time(), time.time()))
            con.execute("insert into shop_urls values(?,?)", (shop_url, "s1"))
            con.commit()
            con.close()
            sh = {"authorization": "Bearer shop-token"}
        else:
            sh = {"authorization": f"Bearer {r.json()['token']}"}
        cat = c.get(f"{BASE}/api/catalog").json()
        sku = next(x for x in cat if x["kind"] == "goods" and x["list_price"] < 9000)["sku"] if any("kind" in x for x in cat) else cat[0]["sku"]
        unit = c.post(f"{BASE}/api/supply", json={"sku": sku, "qty": 2}, headers=sh).json()["units"]

        bkey, bh = join(c, "손님-0001", "buyer")
        r = c.post(f"{BASE}/api/orders", json={"sku": sku, "budget": 5000}, headers=bh)
        check("a buyer that is not a customer gets a market order, not its own", "credits_left" not in r.text, r.text[:120])
        c.post(f"{BASE}/api/admin/label", json={"name": "손님-0001", "label": "persona"}, headers={"x-admin-token": ADMIN})
        check("a customer sees its daily credits", c.get(f"{BASE}/api/customer", headers=bh).json().get("credits_left") == 10000)

        def buy(u, price):
            co = {"id": "cs", "shop": "s1", "shop_url": shop_url, "item": u["sku"], "price": price,
                  "task": "t-" + os.urandom(4).hex(), "currency": "credits", "iat": int(time.time()), "unit": u["unit"]}
            jwt = kit.jwt_sign(skey, co)
            o = c.post(f"{BASE}/api/orders", json={"sku": u["sku"], "budget": price}, headers=bh)
            if o.status_code != 200:
                return o
            o = o.json()
            h = kit.b64url(hashlib.sha256(jwt.encode()).digest())
            opens = {k: hashlib.sha256(canon(v["mandate"])).hexdigest() for k, v in o["mandates"].items()}
            cm = {"vct": "mandate.checkout.1", "order": o["order"], "open_mandate": opens["checkout"], "checkout_jwt": jwt, "checkout_hash": h}
            pm = {"vct": "mandate.payment.1", "order": o["order"], "open_mandate": opens["payment"], "payee": shop_url,
                  "amount": price, "currency": "credits", "checkout_hash": h}
            sign = lambda m: {"mandate": m, "sig": base64.b64encode(bkey.sign(canon(m))).decode()}
            return c.post(f"{BASE}/api/settle", json={"order": o["order"], "checkout_jwt": jwt, "checkout_mandate": sign(cm),
                                                      "payment_mandate": sign(pm)})

        before = c.get(f"{BASE}/api/inventory", headers=sh).json()["balance"]
        r = buy(unit[0], 6000)
        check("a customer's deal settles in credits with no chain transaction", r.status_code == 200 and r.json().get("settled"),
              r.text[:200])
        after = c.get(f"{BASE}/api/inventory", headers=sh).json()["balance"]
        check("the shop is paid the whole price in credits", after - before == 6000, (before, after))
        check("the allowance goes down by what was spent", c.get(f"{BASE}/api/customer", headers=bh).json()["credits_left"] == 4000)
        r = buy(unit[1], 6000)
        check("a customer cannot spend past its daily credits", r.status_code == 402, r.text[:160])
        board = c.get(f"{BASE}/api/board").json()
        check("customers are not on the buyer leaderboard", all(b["name"] != "손님-0001" for b in board.get("buyers", [])))
        _, ah = join(c, "agent-b", "buyer")
        o = c.post(f"{BASE}/api/orders", headers=ah).json()
        check("an agent's own order is still paid on chain", "chain" in o, list(o)[:12])
        ags = {a["name"]: a for a in c.get(f"{BASE}/api/agents").json()["agents"]}
        check("the agents list shows customers and agents with their roles", ags.get("손님-0001", {}).get("roles") == ["buyer"]
              and ags.get("s1", {}).get("roles") == ["shop"], list(ags))
    finally:
        proc.terminate()
        proc.wait(10)
        failed = [r for r in RESULTS if not r[1]]
        print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed; logs in {tmp}")
        if not failed:
            shutil.rmtree(tmp, ignore_errors=True)
    sys.exit(len([r for r in RESULTS if not r[1]]))


if __name__ == "__main__":
    main()
