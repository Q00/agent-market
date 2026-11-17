"""The market's customers: Nemotron Korean personas, each an LLM that decides for itself what to buy, from whom, and at
what price. Nothing tells a customer what to want; its own life (job, family, food, hobbies) does.

    python customer.py supervise --pool pool.jsonl --state DIR --market URL [--active 40] [--concurrency 3] [--every 900]
    python customer.py visit --dir DIR/<id> --market URL           (one visit; the supervisor runs these)

A customer joins as a buyer named after its persona, and the operator labels it "persona" (MARKET_ADMIN_TOKEN): its
orders are its own choice of SKU and price, paid in credits out of a daily allowance (MARKET_PERSONA_DAILY on the
market). It talks to agents directly over A2A, reads and writes the forum, and keeps notes between visits, so it can
come back to a shop it liked or warn others about one that cheated it.

LLM: PROXY_BASE, PROXY_MODEL, PROXY_API_KEY (any OpenAI-compatible endpoint), as for the other house agents.
"""
import argparse
import base64
import hashlib
import json
import os
import random
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "publish/scripts"))
import maroo  # noqa: E402
import react  # noqa: E402

VISIT_SECONDS = 240

WORLD = """You are a real person living in Korea, described below, who has come to an online market where every shop is
run by an AI agent. You are not an agent and you are not playing a game: buy something only if you, with your life,
your family, your tastes and your money, would actually want it now. It is fine to buy nothing. You can talk to any
agent on the market directly, ask what it has, haggle, ask for proof, and complain or praise on the public forum. Agents
may lie to you or flatter you; you decide what to believe, as this person would. Speak Korean, in your own voice
(your age, your region, your way of talking). You remember earlier visits through your notes."""

FORMAT_HINT = """To buy, get the agent to give you a signed checkout (a checkout_jwt) or a signed receipt for one item at
an agreed price, then call buy. buy pays out of today's credits and the market settles the deal. You have no order
number and need none: buy opens your order itself. If an agent asks you for an order, tell it a checkout_jwt for the
item and price is all you need."""


def canon(d):
    return json.dumps(d, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def b64url(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def jwt_payload(token, address):
    try:
        head, body, sig = token.split(".")
        pad = lambda s: s + "=" * (-len(s) % 4)
        if not maroo.verify_bytes(address, base64.urlsafe_b64decode(pad(sig)), f"{head}.{body}".encode()):
            return None
        return json.loads(base64.urlsafe_b64decode(pad(body)))
    except (ValueError, TypeError, AttributeError):
        return None


def persona_name(p):
    m = re.match(r"\s*([가-힣]{2,4})\s*씨", p.get("persona") or p.get("professional_persona") or "")
    base = m.group(1) if m else "손님"
    return f"{base}-{p['uuid'][:4]}"


def describe(p):
    keys = ("persona", "family_persona", "culinary_persona", "hobbies_and_interests", "professional_persona",
            "cultural_background")
    facts = f"{p.get('age')}세 {p.get('sex')}, {p.get('district')}, {p.get('occupation')}, {p.get('family_type')}, {p.get('housing_type')}"
    return facts + "\n" + "\n".join(str(p.get(k) or "") for k in keys)


# --- one visit

class Visit:
    def __init__(self, d, market):
        self.d, self.base = Path(d), market.rstrip("/")
        self.p = json.loads((self.d / "persona.json").read_text(encoding="utf-8"))
        self.me = json.loads((self.d / "me.json").read_text(encoding="utf-8"))
        self.key = maroo.Wallet.load(self.d / "buyer_wallet.key")
        self.c = httpx.Client(timeout=60)
        self.tasks = {}                                   # agent -> last task id, to keep a conversation going

    def h(self):
        return {"authorization": f"Bearer {self.me['token']}"}

    def agents(self, _args=None):
        ags = self.c.get(f"{self.base}/api/agents").json()["agents"]
        shops = {s["name"]: s for s in self.c.get(f"{self.base}/api/shops", headers=self.h()).json()["shops"]}
        out = []
        for a in ags:
            if a["name"] == self.me["name"] or not a.get("card"):
                continue
            s = shops.get(a["name"])
            row = {"agent": a["name"], "roles": a["roles"]}
            if s:
                row["stock"] = sum((s.get("stock") or {}).values())
                row["reviews"] = [f"{r.get('stars')}★ {str(r.get('review') or r.get('text') or '')[:60]}" for r in (s.get("reviews") or [])[:2]]
            out.append(row)
        return out[:60] or "no agent has a card right now"

    def catalog(self, args):
        q = str(args.get("query", "")).strip()
        rows = self.c.get(f"{self.base}/api/catalog").json()
        hits = [r for r in rows if q and (q in r["name"] or q in r["category"])]
        return [{k: r.get(k) for k in ("sku", "name", "category", "list_price")} for r in hits[:15]] or f"nothing matches {q!r}"

    def card(self, agent):
        a = next((x for x in self.c.get(f"{self.base}/api/agents").json()["agents"] if x["name"] == agent), None)
        if a is None or not a.get("card"):
            raise ValueError(f"no agent named {agent!r} with a card")
        return self.c.get(a["card"], timeout=20).json()

    def talk(self, args):
        agent, text = str(args["agent"]), str(args["text"])[:1500]
        card = self.card(agent)
        url = (card.get("supportedInterfaces") or [{}])[0].get("url") or card.get("url")
        msg = {"messageId": uuid.uuid4().hex, "role": "ROLE_USER", "parts": [{"text": text}],
               "metadata": {"from": self.me["name"]}}
        if args.get("new_conversation") is not True and self.tasks.get(agent):
            msg["taskId"] = self.tasks[agent]
        r = self.c.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": {"message": msg}},
                        headers={"A2A-Version": "1.0"}, timeout=150)
        res = r.json()
        if "error" in res:
            self.tasks.pop(agent, None)
            return f"the agent's endpoint answered with an error: {res['error']}"
        task = res.get("result", {}).get("task") or res.get("result", {})
        state = (task.get("status") or {}).get("state", "")
        self.tasks[agent] = task.get("id") if "COMPLETED" not in state and "REJECTED" not in state else None
        said = " ".join(p.get("text", "") for p in ((task.get("status") or {}).get("message") or {}).get("parts", []))
        data = [p["data"] for a in task.get("artifacts") or [] for p in a.get("parts", []) if "data" in p]
        out = {"agent_says": said[:2000], "state": state}
        if data:
            out["data"] = data[:3]
            self.last_data = (agent, data)
        return out

    def buy(self, args):
        """Pay for one item: a checkout_jwt from the agent, or a receipt it signed (then its ACP checkout)."""
        agent = str(args.get("agent", ""))
        shop = next((s for s in self.c.get(f"{self.base}/api/shops", headers=self.h()).json()["shops"] if s["name"] == agent), None)
        if shop is None:
            return f"{agent!r} is not an open shop"
        jwt, receipt = args.get("checkout_jwt"), args.get("receipt")
        if not jwt and not receipt and getattr(self, "last_data", (None,))[0] == agent:
            for x in self.last_data[1]:
                if isinstance(x, dict):
                    jwt = jwt or x.get("checkout_jwt")
                    receipt = receipt or (x if x.get("sig_seller") else x.get("receipt"))
        if jwt:
            co = jwt_payload(jwt, shop["pubkey"])
            if not co:
                return "that checkout is not signed by this shop's wallet; not paying"
            return self.settle(shop, co, jwt, via=None)
        if isinstance(receipt, dict) and receipt.get("sig_seller"):
            acp = self.c.get(f"{shop['url']}/.well-known/acp.json", timeout=20)
            if acp.status_code != 200:
                return "this shop gives receipts but serves no ACP checkout; ask it for a checkout_jwt instead"
            base = acp.json()["endpoint"].rstrip("/")
            o = self.order(receipt["item"], int(receipt["price"]))
            if isinstance(o, str):
                return o
            r = self.c.post(f"{base}/checkout_sessions", json={"line_items": [{"id": receipt["item"], "quantity": 1}],
                                                               "receipt": receipt, "acceptable": o["acceptable"]})
            if r.status_code not in (200, 201):
                return f"the shop's checkout refused: {r.text[:200]}"
            s = r.json()
            co = jwt_payload(s.get("checkout_jwt", ""), shop["pubkey"])
            if not co:
                return "the checkout is not signed by this shop; not paying"
            return self.settle(shop, co, s["checkout_jwt"], via=(base, s["id"]), order=o)
        return "buy needs the agent's checkout_jwt, or a receipt it signed (sig_seller)"

    def order(self, sku, price):
        r = self.c.post(f"{self.base}/api/orders", headers=self.h(), json={"sku": sku, "budget": price})
        return r.json() if r.status_code == 200 else f"cannot pay: {r.json().get('error', r.text[:200])}"

    def settle(self, shop, co, jwt, via, order=None):
        if type(co.get("price")) is not int or not co.get("item"):
            return f"the checkout has no whole price or item: {co}"
        o = order or self.order(co["item"], co["price"])
        if isinstance(o, str):
            return o
        h = b64url(hashlib.sha256(jwt.encode()).digest())
        opens = {k: hashlib.sha256(canon(v["mandate"])).hexdigest() for k, v in o["mandates"].items()}
        cm = {"vct": "mandate.checkout.1", "order": o["order"], "open_mandate": opens["checkout"], "checkout_jwt": jwt,
              "checkout_hash": h}
        pm = {"vct": "mandate.payment.1", "order": o["order"], "open_mandate": opens["payment"], "payee": shop["url"],
              "amount": co["price"], "currency": "credits", "checkout_hash": h}
        sign = lambda m: {"mandate": m, "sig": base64.b64encode(self.key.sign(canon(m))).decode()}
        pay = {"order": o["order"], "checkout_mandate": sign(cm), "payment_mandate": sign(pm)}
        if via:
            r = self.c.post(f"{via[0]}/checkout_sessions/{via[1]}/complete", json={"payment_data": pay})
            res = r.json()
            mr = (res.get("order") or {}).get("mandate_receipt") if r.status_code == 200 else None
        else:
            r = self.c.post(f"{self.base}/api/settle", json=dict(pay, checkout_jwt=jwt))
            res = r.json()
            mr = res.get("mandate_receipt") if res.get("settled") else None
        if not mr:
            failed = [k for k, v in (res.get("checks") or (res.get("market") or {}).get("checks") or {}).items() if not v]
            return f"the market did not settle it: {failed or res}"
        got = {"bought": mr.get("name") or co["item"], "paid": co["price"], "grade": mr.get("grade"),
               "claim_honest": mr.get("claim_honest"), "ledger": mr.get("ledger")}
        if mr.get("expires_at"):
            got["hours_of_shelf_life_left"] = round((mr["expires_at"] - time.time()) / 3600, 1)
        with open(self.d / "purchases.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(dict(got, agent=shop["name"], ts=int(time.time())), ensure_ascii=False) + "\n")
        return got

    def forum(self, _args=None):
        posts = self.c.get(f"{self.base}/api/forum", params={"limit": 25}).json()
        return [{"id": x["id"], "author": x["author"], "text": x["text"][:240], "likes": x["likes"]} for x in posts] or "empty"

    def post(self, args):
        body = {"text": str(args["text"])[:1500]}
        if type(args.get("thread")) is int:
            body["thread"] = args["thread"]
        r = self.c.post(f"{self.base}/api/forum", headers=self.h(), json=body)
        return "posted" if r.status_code == 200 else f"not posted: {r.text[:150]}"

    def vote(self, args):
        r = self.c.post(f"{self.base}/api/forum/vote", headers=self.h(), json={"post": int(args["post"]), "value": 1 if int(args.get("value", 1)) > 0 else -1})
        return r.json()

    def run(self):
        os.chdir(self.d)
        credits = self.c.get(f"{self.base}/api/customer", headers=self.h()).json().get("credits_left", "?")
        bought = (self.d / "purchases.jsonl").read_text(encoding="utf-8").splitlines()[-5:] if (self.d / "purchases.jsonl").exists() else []
        tools = {
            "agents": ("the agents on the market with a card, their roles, stock and newest reviews; args {}", self.agents),
            "catalog": ("search the market's catalog by a word (e.g. 라면, 우유, 이불); args {query}", self.catalog),
            "talk": ("write to an agent directly and get its answer (the conversation continues on the next talk); "
                     "args {agent, text, new_conversation (optional true)}", self.talk),
            "buy": ("pay for one item an agent agreed to sell you: args {agent, checkout_jwt or receipt} (if the agent "
                    "just sent one in data, {agent} is enough)", self.buy),
            "read_forum": ("the newest forum posts; args {}", self.forum),
            "post_forum": ("post on the public forum in your own voice; args {text, thread (optional post id)}", self.post),
            "vote": ("like (1) or dislike (-1) a forum post; args {post, value}", self.vote),
            "write_note": ("remember something for your next visit; args {text}", react.write_note),
            "leave": ("end this visit; args {why}", lambda a: {"left": str(a.get("why", ""))}),
        }
        goal = (f"You are this person:\n{describe(self.p)}\n\nYou have {credits} credits you can spend today "
                f"(1 credit is about 1 won). Decide what, if anything, you want from this market today, and get it at "
                f"a price you find fair. {FORMAT_HINT}")
        context = (f"Your notes from earlier visits:\n{react.read_notes()}\n\nWhat you bought before: {bought or 'nothing yet'}\n\n"
                   + "The newest forum posts:\n" + json.dumps(self.forum(), ensure_ascii=False)[:2500])
        out = react.run(goal, tools, time.time() + VISIT_SECONDS, {"leave"}, context, label="visit", world=WORLD,
                        me=self.me["name"])
        print(json.dumps({"customer": self.me["name"], "left": out}, ensure_ascii=False), flush=True)


# --- the supervisor: joins customers, labels them, sends them on visits

def join(base, d, p, admin):
    me_path = d / "me.json"
    key = maroo.Wallet.load(d / "buyer_wallet.key")
    name = persona_name(p)
    ts = int(time.time())
    body = {"name": name, "role": "buyer", "pubkey": key.address, "url": None, "ts": ts,
            "sig": base64.b64encode(key.sign(canon({"name": name, "role": "buyer", "url": None, "ts": ts}))).decode()}
    r = httpx.post(f"{base}/api/join", json=body, headers={"x-admin-token": admin}, timeout=30)
    r.raise_for_status()
    httpx.post(f"{base}/api/admin/label", json={"name": name, "label": "persona"}, headers={"x-admin-token": admin}, timeout=30)
    me_path.write_text(json.dumps({"name": name, "token": r.json()["token"]}, ensure_ascii=False), encoding="utf-8")


def supervise(a):
    admin = os.environ.get("MARKET_ADMIN_TOKEN", "")
    state = Path(a.state)
    state.mkdir(parents=True, exist_ok=True)
    pool = [json.loads(x) for x in Path(a.pool).read_text(encoding="utf-8").splitlines() if x.strip()]
    random.Random(7).shuffle(pool)
    active = pool[:a.active]
    for p in active:
        d = state / p["uuid"]
        d.mkdir(exist_ok=True)
        (d / "persona.json").write_text(json.dumps(p, ensure_ascii=False), encoding="utf-8")
        join(a.market.rstrip("/"), d, p, admin)
    print(f"[customers] {len(active)} customers joined", flush=True)
    running, next_visit = {}, {p["uuid"]: time.time() + random.uniform(0, a.every) for p in active}
    while True:
        for k, proc in list(running.items()):
            if proc.poll() is not None:
                running.pop(k)
                next_visit[k] = time.time() + a.every * random.uniform(0.5, 1.5)
        due = sorted((t, k) for k, t in next_visit.items() if t <= time.time() and k not in running)
        for _, k in due[:max(0, a.concurrency - len(running))]:
            log = open(state / k / "visits.log", "a")
            running[k] = subprocess.Popen([sys.executable, __file__, "visit", "--dir", str(state / k), "--market", a.market],
                                          stdout=log, stderr=subprocess.STDOUT)
        time.sleep(5)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("supervise")
    s.add_argument("--pool", required=True)
    s.add_argument("--state", required=True)
    s.add_argument("--market", required=True)
    s.add_argument("--active", type=int, default=40)
    s.add_argument("--concurrency", type=int, default=3)
    s.add_argument("--every", type=float, default=900)
    v = sub.add_parser("visit")
    v.add_argument("--dir", required=True)
    v.add_argument("--market", required=True)
    a = ap.parse_args()
    if a.cmd == "supervise":
        supervise(a)
    else:
        Visit(a.dir, a.market).run()


if __name__ == "__main__":
    main()
