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
    env = dict(os.environ, MARKET_ADMIN_TOKEN=ADMIN, MARKET_SUPPLY_EVERY="0", MARKET_PERSONA_DAILY="10000", MARKET_PERSONA_ON_CHAIN="0", MARKET_COIN="0",
               MAROO_RPC_URL="http://127.0.0.1:9")            # no chain: a customer's deal must not need one
    for k in ("MARKET_PUBLIC", "GITHUB_CLIENT_ID", "GITHUB_CLIENT_SECRET", "MARKET_BASE_URL"):
        env.pop(k, None)
    (tmp / "scorer.py").write_text("def score(db, rnd, shops, customers):\n"
                                   "    open(__file__ + '.ran', 'w').write(str(sorted(shops[0])))\n"
                                   "    return {'shops': {s['name']: 1 for s in shops}, 'customers': {c['name']: c['deals'] for c in customers if c['deals']}}\n")
    env["MARKET_SCORER"] = str(tmp / "scorer.py")
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
        cat_name = next(x for x in cat if x["sku"] == sku)["name"]

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
        check("from Round 4 the board gives ranks and names and nothing else", board.get("ranks_only") is True
              and all(set(x) <= {"name", "owner", "house", "vooy", "joined", "alive", "card", "student", "rank"} for x in board["shops"])
              and all(set(x) <= {"name", "owner", "house", "vooy", "joined", "alive"} for x in board["buyers"]) and board["players"] == [],
              [sorted(x) for x in board["shops"]][:1])
        check("the village's customers are ranked by name", board["customers"] == [{"name": "손님-0001", "alive": True, "rank": 1}], board.get("customers"))
        check("an operator's ranking function makes the ranks", (tmp / "scorer.py.ran").exists()
              and "profit" in (tmp / "scorer.py.ran").read_text() and next(x["rank"] for x in board["shops"] if x["name"] == "s1") == 1)
        _, ah = join(c, "agent-b", "buyer")
        o = c.post(f"{BASE}/api/orders", headers=ah).json()
        check("an agent's own order is still paid on chain", "chain" in o, list(o)[:12])
        ags = {a["name"]: a for a in c.get(f"{BASE}/api/agents").json()["agents"]}
        check("the agents list shows customers and agents with their roles", ags.get("손님-0001", {}).get("roles") == ["buyer"]
              and ags.get("s1", {}).get("roles") == ["shop"], list(ags))

        # --- forum: the two sides apart, and a household that has bought once has a vote that counts
        ps = c.post(f"{BASE}/api/forum", json={"text": "s1: 쌀 들어왔습니다"}, headers=sh).json()["post"]
        pb = c.post(f"{BASE}/api/forum", json={"text": "s1에서 샀는데 괜찮았어요"}, headers=bh).json()["post"]
        sides = {r: [x["id"] for x in c.get(f"{BASE}/api/forum", params={"role": r}).json()] for r in ("buyer", "shop")}
        check("the forum gives one side's posts when asked", sides == {"buyer": [pb], "shop": [ps]}
              and c.get(f"{BASE}/api/forum", params={"role": "x"}).status_code == 400
              and len(c.get(f"{BASE}/api/forum").json()) >= 2, sides)
        r = c.post(f"{BASE}/api/forum/vote", json={"post": ps, "value": -1}, headers=bh).json()
        r2 = c.post(f"{BASE}/api/forum/vote", json={"post": ps, "value": 1}, headers=ah).json()
        seen = next(x for x in c.get(f"{BASE}/api/forum", params={"role": "shop"}).json() if x["id"] == ps)
        check("a customer's vote counts after its first purchase and costs nothing; another buyer still needs the deals",
              r.get("counted") is True and r.get("fee") == 0 and r2.get("counted") is False
              and (seen["likes"], seen["dislikes"]) == (0, 1), (r, r2, seen["likes"], seen["dislikes"]))

        # --- a visit's purchase: into memory (events and facts) and into the household's life, with no model
        sys.path.insert(0, str(HERE / "house"))
        import customer
        import persona_mind as pm
        import persona_world as pw
        town = tmp / "town"
        town.mkdir()

        def home(uuid, name, token=None, key=None, needs=None, rest=0):
            d = town / uuid
            d.mkdir()
            (d / "persona.json").write_text(json.dumps({"uuid": uuid, "persona": f"{name} 씨는 테스트 손님이다.", "age": 60,
                                                        "sex": "여자", "district": "경기-의정부시", "occupation": "주부"}, ensure_ascii=False), encoding="utf-8")
            now = pw.world_day()
            life = {"home": "의정부 아파트", "monthly_money": 600000, "purse": 100000, "purse_day": now, "born": now, "rest_until": rest,
                    "occasions": [], "needs": needs if needs is not None else [{"what": "쌀", "why": "밥", "every_days": 20, "about_price": 20000, "last_met": now - 50}]}
            (d / "life.json").write_text(json.dumps(life, ensure_ascii=False), encoding="utf-8")
            if token:
                (d / "me.json").write_text(json.dumps({"name": name, "token": token}, ensure_ascii=False), encoding="utf-8")
                (d / "buyer_wallet.key").write_text(key.key_hex() + "\n", encoding="utf-8")
            return d

        d = home("c0ffee00", "손님-0001", bh["authorization"].split()[1], bkey,
                 needs=[{"what": cat_name, "why": "떨어졌다", "every_days": 10, "about_price": 5000, "last_met": pw.world_day() - 30}])
        for _ in range(3):                                       # the supply desk asks a shop to wait between buys
            r = c.post(f"{BASE}/api/supply", json={"sku": sku, "qty": 1}, headers=sh)
            if r.status_code == 200:
                break
            time.sleep(float(r.json().get("wait", 5)) + 0.5)
        assert r.status_code == 200, r.text
        more = r.json()["units"][0]
        v = customer.Visit(d, BASE)
        v.life = pw.Life(d, v.p)
        co = {"id": "cs", "shop": "s1", "shop_url": shop_url, "item": more["sku"], "price": 3000, "task": "t-" + os.urandom(4).hex(),
              "currency": "credits", "iat": int(time.time()), "unit": more["unit"]}
        got = v.settle({"name": "s1", "url": shop_url, "pubkey": skey.address}, co, kit.jwt_sign(skey, co), via=None)
        check("a visit's purchase settles and is recorded as an event with facts pointing at it", isinstance(got, dict)
              and [e["kind"] for e in v.mind.events] == ["bought"] and {f["r"] for f in v.mind.facts} == {"sold you", "price"}, got)
        check("it meets the household's need and comes out of its purse", got.get("this_was_for") == cat_name and v.life.s["purse"] == 97000, got)
        check("the outcome is a reward for what was learned", v.reward() == 1.0)
        v.shown = [v.mind.get(v.mind.add("shop:s1", "routing", "s1은 값을 깎아 준다"))]
        v.remember({"text": "s1 주인은 친절하다", "about": "s1"})
        v.finish({"left": "샀다", "used": [v.shown[0]["id"]]})
        m2 = pm.Mind(d)
        check("after the visit the used rule is stronger and the opinion is kept", m2.get(v.shown[0]["id"])["alpha"] == 2.0
              and any(i["text"] == "s1 주인은 친절하다" and i["scope"] == "shop:s1" for i in m2.items) and m2.events[-1]["kind"] == "left")
        check("with what it came for, the household rests", json.loads((d / "life.json").read_text())["rest_until"] > pw.world_day())
        check("the next visit recalls the raw event", "you bought" in pm.Mind(d).collect(cat_name + " 가격", "graph"))

        # --- talk: a kit tells the market its agent was written to; the feed has no words without the admin token
        t = c.post(f"{BASE}/api/talk", json={"from": "agent-b", "text": "장부 바꿔 볼래요?", "reply": "아니요", "by": "tool"}, headers=sh)
        c.post(f"{BASE}/api/talk", json={"from": "아무개", "text": "x"}, headers=sh)
        feed = c.get(f"{BASE}/api/talks").json()
        full = c.get(f"{BASE}/api/talks", params={"since": 0}, headers={"x-admin-token": ADMIN}).json()
        check("the market keeps who wrote to whom, as the writer called itself", t.status_code == 200
              and [(x["to"], x["from"], x["from_role"], x["chars"], x["reply_chars"], x["by"]) for x in feed]
              == [("s1", "agent-b", "buyer", 10, 3, "tool"), ("s1", "아무개", None, 1, 0, "mind")], feed)
        check("the words are for the operator only", all("text" not in x for x in feed) and full[0]["text"] == "장부 바꿔 볼래요?"
              and c.post(f"{BASE}/api/talk", json={"text": "x"}).status_code == 401)

        # --- neighbours: customers talk to one another through town.jsonl
        d2 = home("beef0002", "이웃-0002", "t2", maroo.Wallet.generate())
        (town / "roster.json").write_text(json.dumps({"cursor": 2, "active": {"c0ffee00": 0, "beef0002": 0}}), encoding="utf-8")
        a1, a2 = customer.Visit(d, BASE), customer.Visit(d2, BASE)
        check("a customer sees who else is in town", [x["name"] for x in a1.neighbours()["around_now"]] == ["이웃-0002"])
        check("and how often each has bought here", "이 시장에서 0번 삼" in a1.neighbours()["around_now"][0]["who"])
        asked = []
        customer.react.complete = lambda m, **k: asked.append(m) or "s1이 싸긴 한데 유통기한은 꼭 물어보세요."
        gone = a1.chat({"with": "s1", "text": "환불해 주세요"})
        check("a shop is not a neighbour, and chat says where a shop is reached", isinstance(gone, str) and "talk" in gone and not asked, gone)
        piles = a1.forum()
        check("a customer reads the households' posts and the shops' apart", [x["text"] for x in piles["from_customers"]] == ["s1에서 샀는데 괜찮았어요"]
              and [x["dislikes"] for x in piles["from_shops"]] == [1] and "What other households wrote" in a1.forum_text(), piles)
        out = a1.chat({"with": "이웃-0002", "text": "쌀은 어디가 싸요?"})
        check("the neighbour answers as someone of this village", pw.place("beef0002")[0] in asked[0][0]["content"]
              and "same village" in asked[0][0]["content"] and pw.place(a1.p.get("uuid"))[0] in asked[0][1]["content"], asked[0][0]["content"][:200])
        a1.tell_everyone({"text": "오늘 s1에서 쌀 싸게 샀어요"})
        check("chat gets the neighbour's answer and both lines become events", out == {"neighbour_says": "s1이 싸긴 한데 유통기한은 꼭 물어보세요."}
              and [(e["kind"], e["shop"]) for e in a1.mind.events[-2:]] == [("said", "neighbour:이웃-0002"), ("heard", "neighbour:이웃-0002")])
        heard = a2.hear_town()
        check("the neighbour hears it at its next visit: what was said to it and to everyone", len(heard) == 2
              and [e["kind"] for e in a2.mind.events] == ["heard", "said", "heard"], heard)
        check("heard once, not again", a2.hear_town() == [])
        check("a customer does not hear its own remark", not any("오늘 s1에서" in x for x in a1.hear_town()))

        # --- who comes to town: the households that want to go, not a timer
        for k in ("c0ffee00", "beef0002"):
            shutil.rmtree(town / k)
        (town / "roster.json").unlink(); (town / "town.jsonl").unlink()
        pool = tmp / "pool.jsonl"
        people = [{"uuid": f"{i:08x}aaaa", "persona": f"박{chr(0xAC00 + i)}수 씨는 테스트 손님이다."} for i in range(1, 7)]
        pool.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in people), encoding="utf-8")
        now = pw.world_day()
        for i, x in enumerate(people):                           # 1-3 have run out of rice; 4 too, but rests; 5-6 need nothing
            due = [{"what": "쌀", "why": "밥", "every_days": 20, "about_price": 20000, "last_met": now - (90 if i < 4 else 1)}]
            home(x["uuid"], "", needs=due, rest=now + 999 if i == 3 else 0)
        sup = subprocess.Popen([sys.executable, str(HERE / "house/customer.py"), "supervise", "--pool", str(pool), "--state", str(town),
                                "--market", BASE, "--active", "2", "--every", "99999", "--first-visit", "99999", "--stay-hours", "1"],
                               env=dict(env, MARKET_ADMIN_TOKEN=ADMIN), stdout=open(tmp / "sup.log", "w"), stderr=subprocess.STDOUT)
        try:
            wanting = {x["uuid"] for x in people[:3]}
            for _ in range(40):
                time.sleep(1)
                try:
                    r = json.loads((town / "roster.json").read_text())
                except (OSError, ValueError):
                    continue
                if len(r["active"]) == 2:
                    break
            first = set(r["active"])
            check("the town fills, up to --active, with households that have a reason to come", len(first) == 2 and first <= wanting, (first, wanting))
            vill = json.loads((town / "village.json").read_text())
            check("the village's state is written for everyone to read", vill["households"] == 6 and vill["with_life"] == 6
                  and len(vill["in_town"]) == 2 and vill["weather"] and vill["in_town"][0]["lives_in"], vill)
            gone = sorted(first)[0]
            pw.send_home(town / gone, 5)                         # as a visit does when the customer has what it came for
            for _ in range(40):
                time.sleep(1)
                r = json.loads((town / "roster.json").read_text())
                if gone not in r["active"] and len(r["active"]) == 2:
                    break
            check("one who is done goes home and the next who wants to come arrives", gone not in r["active"]
                  and set(r["active"]) == wanting - {gone}, (r["active"], gone))
            names = {a["name"] for a in c.get(f"{BASE}/api/agents").json()["agents"]}
            check("only those who came are on the market's list", sum(1 for n in names if n.startswith("박")) == 3, sorted(names))
        finally:
            sup.terminate()
            sup.wait(10)
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
