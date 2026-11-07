"""Round 4 end to end: a real market process and real signed transactions, on tests/fake_chain.py.

    .venv/bin/python tests/test_r4_chain.py

The exit code is the number of failed checks.
"""
import base64
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE / "tests"))
RPC_PORT = 8742
os.environ["MAROO_RPC_URL"] = f"http://127.0.0.1:{RPC_PORT}"

import httpx  # noqa: E402

from fake_chain import FakeChain  # noqa: E402
from test_r3_e2e import ADMIN, BASE, CARD_PORT, PORT, RESULTS, Agent, Cards, canon, check, kit, maroo, new_order  # noqa: E402

CW = maroo.CREDIT_WEI
F = "r4"


def mandates(shop, buyer, o, unit, price, tx_hash=None, data=None, sign_with=None):
    """The checkout and the buyer's closed mandates. data=None: the transaction names this checkout."""
    co = {"id": "cs", "shop": shop.name, "shop_url": shop.url, "item": unit["sku"], "price": price,
          "task": "t-" + os.urandom(6).hex(), "currency": "credits", "iat": int(time.time()), "unit": unit["unit"],
          "cert": unit["cert"]}
    jwt = kit.jwt_sign(shop.key, co)
    digest = hashlib.sha256(jwt.encode()).digest()
    h = kit.b64url(digest)
    opens = {k: hashlib.sha256(canon(v["mandate"])).hexdigest() for k, v in o["mandates"].items()}
    cm = {"vct": "mandate.checkout.1", "order": o["order"], "open_mandate": opens["checkout"], "checkout_jwt": jwt,
          "checkout_hash": h}
    pm = {"vct": "mandate.payment.1", "order": o["order"], "open_mandate": opens["payment"], "payee": shop.url,
          "amount": price, "currency": "credits", "checkout_hash": h}
    return jwt, digest, cm, pm


def post_settle(shop, buyer, o, jwt, cm, pm, signer=None):
    signer = signer or buyer.key
    sign = lambda m: {"mandate": m, "sig": base64.b64encode(signer.sign(canon(m))).decode()}
    return shop.post("/api/settle", {"order": o["order"], "checkout_jwt": jwt, "checkout_mandate": sign(cm),
                                     "payment_mandate": sign(pm)})


def old_database_upgrades(tmp):
    """A database from before Round 4 has settlements without the chain columns; the market must still start on it."""
    path = tmp / "old.db"
    db = sqlite3.connect(path)
    db.execute("create table settlements(order_id text primary key, ts real, round int, buyer text, shop_url text,"
               " item text, price int, task text, checkout_hash text)")
    db.commit()
    db.close()
    code = ("import sys; sys.argv = ['server']; sys.path.insert(0, sys.argv[0] or '.'); "
            f"sys.path.insert(0, {str(HERE / 'market')!r}); import server; server.Market({str(path)!r})")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    db = sqlite3.connect(path)
    cols = {x[1] for x in db.execute("pragma table_info(settlements)")}
    indexes = {x[1] for x in db.execute("pragma index_list(settlements)")}
    db.close()
    check(F, "a database from before Round 4 upgrades",
          r.returncode == 0 and {"tx_hash", "fee_tx_hash", "shop_fee", "buyer_fee"} <= cols
          and {"settlements_tx", "settlements_fee_tx"} <= indexes, r.stderr[-160:])


def run(c, chain):
    shop, other = Agent(c, "s1", "shop"), Agent(c, "s2", "shop")
    buyer, thief = Agent(c, "b1", "buyer"), Agent(c, "b2", "buyer")
    chain.fund(buyer.key.address, 10 ** 22)
    chain.fund(thief.key.address, 10 ** 22)
    c.post(f"{BASE}/api/admin/round", headers={"x-admin-token": ADMIN}, json={"round": 4})
    info = c.get(f"{BASE}/api/chain").json()
    check(F, "the market says which chain it pays on", info["chain_id"] == 450815 and info["from_round"] == 4, info["network"])
    check(F, "an address that is not a wallet cannot join",
          c.post(f"{BASE}/api/join", json={"name": "x", "role": "buyer", "pubkey": "abc"}).status_code == 400)

    def fit():
        o = new_order(buyer)
        sku = min(o["acceptable"], key=lambda s: CAT[s]["list_price"])
        return o, sku

    CAT = {x["sku"]: x for x in c.get(f"{BASE}/api/catalog").json()}
    # goods do not expire during the test, so settle always sees the unit in stock
    def goods_order():
        while True:
            o, sku = fit()
            if o["kind"] == "goods" and CAT[sku]["list_price"] <= 300_000:
                return o, sku

    def stocked(sku):
        r = shop.supply(sku, 1)
        assert r.status_code == 200, r.text
        return next(u for u in shop.inv()["units"] if u["sku"] == sku)

    def refused(label, key, r, chain_says=None):
        j = r.json()
        check(F, f"refused: {label}", r.status_code == 400 and j["checks"].get(key) is False
              and (chain_says is None or chain_says in j.get("chain", "")), f"{r.status_code} {j.get('chain')} {r.text[:100]}")

    o, sku = goods_order()
    unit = stocked(sku)
    price = min(o["budget"], CAT[sku]["list_price"])
    fee = maroo.fees(price)
    wei, fee_wei = (price - fee["shop"]) * CW, fee["market"] * CW
    jwt, digest, cm, pm = mandates(shop, buyer, o, unit, price)
    market = info["market_address"]
    check(F, "the order names the fees and the market's wallet",
          o["chain"]["market_address"] == market and o["chain"]["market_fee_bps"] == maroo.MARKET_FEE_BPS, o["chain"])

    def fee_tx():
        return maroo.pay(buyer.key, market, fee_wei, digest)
    refused("no transaction", "paid_on_chain", post_settle(shop, buyer, o, jwt, cm, pm), "no transaction hash")
    pm2 = dict(pm, tx_hash="0x" + "ab" * 32)
    refused("a transaction that does not exist", "paid_on_chain", post_settle(shop, buyer, o, jwt, cm, pm2), "not on chain")
    refused("too little", "paid_on_chain",
            post_settle(shop, buyer, o, jwt, cm, dict(pm, tx_hash=maroo.pay(buyer.key, shop.key.address, wei - CW, digest))), "amount")
    refused("the full price with no shop fee taken", "paid_on_chain",
            post_settle(shop, buyer, o, jwt, cm, dict(pm, tx_hash=maroo.pay(buyer.key, shop.key.address, price * CW, digest))), "amount")
    refused("paid to another wallet", "paid_on_chain",
            post_settle(shop, buyer, o, jwt, cm, dict(pm, tx_hash=maroo.pay(buyer.key, other.key.address, wei, digest))), "payee")
    refused("paid by another wallet", "paid_on_chain",
            post_settle(shop, buyer, o, jwt, cm, dict(pm, tx_hash=maroo.pay(thief.key, shop.key.address, wei, digest))), "payer")
    refused("the transaction names another checkout", "paid_on_chain",
            post_settle(shop, buyer, o, jwt, cm, dict(pm, tx_hash=maroo.pay(buyer.key, shop.key.address, wei, b"x" * 32))), "checkout")
    chain.fail_to.add(shop.key.address)
    try:
        maroo.pay(buyer.key, shop.key.address, wei, digest)
        check(F, "pay() raises when the transfer fails on chain", False)
    except RuntimeError as e:
        check(F, "pay() raises when the transfer fails on chain", "failed" in str(e))
        failed_tx = next(h for h, t in chain.txs.items() if t["status"] == "0x0")
    chain.fail_to.clear()
    refused("a failed transaction", "paid_on_chain", post_settle(shop, buyer, o, jwt, cm, dict(pm, tx_hash=failed_tx)), "failed")
    r = post_settle(shop, buyer, o, jwt, cm, pm, signer=thief.key)
    check(F, "without the buyer's signature the market does not touch the chain or reveal anything",
          r.status_code == 400 and set(r.json()["checks"]) == {"checkout_mandate_signed_by_buyer", "payment_mandate_signed_by_buyer"}, r.text[:120])

    before = chain.balances.get(shop.key.address, 0)
    tx = maroo.pay(buyer.key, shop.key.address, wei, digest)
    check(F, "the shop receives the price less its fees", chain.balances[shop.key.address] - before == wei)
    refused("no market fee", "paid_on_chain", post_settle(shop, buyer, o, jwt, cm, dict(pm, tx_hash=tx)), "market fee: no transaction hash")
    refused("a market fee that is too small", "paid_on_chain",
            post_settle(shop, buyer, o, jwt, cm, dict(pm, tx_hash=tx, fee_tx_hash=maroo.pay(buyer.key, market, fee_wei - CW, digest))), "market fee: the amount")
    refused("a market fee paid to someone else", "paid_on_chain",
            post_settle(shop, buyer, o, jwt, cm, dict(pm, tx_hash=tx, fee_tx_hash=maroo.pay(buyer.key, shop.key.address, fee_wei, digest))), "market fee: the payee")
    market_before = chain.balances.get(market, 0)
    fee_hash = fee_tx()
    check(F, "the market's wallet receives both sides' market fees", chain.balances[market] - market_before == fee_wei)
    cash = shop.inv()["balance"]
    r = post_settle(shop, buyer, o, jwt, cm, dict(pm, tx_hash=tx, fee_tx_hash=fee_hash))
    j = r.json()
    check(F, "a real payment settles", r.status_code == 200 and j["settled"], r.text[:160])
    mr = j.get("mandate_receipt", {})
    check(F, "the receipt carries the transactions and the fees",
          mr.get("tx_hash") == tx and mr.get("fee_tx_hash") == fee_hash and mr.get("shop_fee") == fee["shop"]
          and mr.get("buyer_fee") == fee["buyer"] and mr.get("market_fee") == fee["market"], mr.get("tx_url"))
    check(F, "the shop's balance grows by the price less its fees", shop.inv()["balance"] - cash == price - fee["shop"],
          (price, fee["shop"]))
    led = c.get(f"{BASE}/api/ledger", params={"limit": 200}).json()
    entries = led["entries"] if isinstance(led, dict) and "entries" in led else led
    check(F, "the public ledger carries the transaction", tx in json.dumps(entries), "")

    o2, sku2 = goods_order()
    unit2 = stocked(sku2)
    price2 = min(o2["budget"], CAT[sku2]["list_price"])
    jwt2, digest2, cm2, pm2 = mandates(shop, buyer, o2, unit2, price2)
    refused("a transaction already used", "transaction_not_used_before", post_settle(shop, buyer, o2, jwt2, cm2, dict(pm2, tx_hash=tx)))
    refused("a fee transaction already used", "transaction_not_used_before",
            post_settle(shop, buyer, o2, jwt2, cm2, dict(pm2, tx_hash=maroo.pay(buyer.key, shop.key.address, (price2 - maroo.fees(price2)["shop"]) * CW, digest2), fee_tx_hash=fee_hash)))
    jwt3, digest3, cm3, pm3 = mandates(shop, buyer, o2, unit2, 100)
    refused("a price below the shop's fees", "price_covers_fees", post_settle(shop, buyer, o2, jwt3, cm3, dict(pm3, tx_hash="0x" + "cd" * 32)))


def main():
    tmp = Path(tempfile.mkdtemp(prefix="r4-"))
    old_database_upgrades(tmp)
    chain = FakeChain(RPC_PORT)
    cards = ThreadingHTTPServer(("127.0.0.1", CARD_PORT), Cards)
    threading.Thread(target=cards.serve_forever, daemon=True).start()
    env = dict(os.environ, MARKET_ADMIN_TOKEN=ADMIN, MARKET_ORDER_GAP="0", MARKET_SUPPLY_EVERY="0")
    for k in ("MARKET_PUBLIC", "GITHUB_CLIENT_ID", "GITHUB_CLIENT_SECRET"):
        env.pop(k, None)
    log = open(tmp / "market.log", "w")
    proc = subprocess.Popen([sys.executable, str(HERE / "market/server.py"), "--port", str(PORT), "--db", str(tmp / "m.db")],
                            stdout=log, stderr=subprocess.STDOUT, env=env)
    c = httpx.Client(timeout=30)
    try:
        for _ in range(120):
            try:
                if c.get(f"{BASE}/api/demand").status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.25)
        run(c, chain)
    finally:
        proc.terminate()
        proc.wait(10)
        cards.shutdown()
        chain.stop()
        log.close()
        failed = [r for r in RESULTS if not r[2]]
        print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed; market log {tmp / 'market.log'}")
        for r in failed:
            print(f"  FAILED {r[1]}: {r[3]}")
        if not failed:
            shutil.rmtree(tmp, ignore_errors=True)
    sys.exit(len([r for r in RESULTS if not r[2]]))


if __name__ == "__main__":
    main()
