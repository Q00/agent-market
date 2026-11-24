"""The market's customers: Nemotron Korean personas, each an LLM that decides for itself what to buy, from whom, and at
what price. Nothing tells a customer what to want; its own life (job, family, food, hobbies) does.

    python customer.py supervise --pool pool.jsonl --state DIR --market URL [--active 40] [--concurrency 3]
                                 [--every 900] [--stay-hours 48]
    python customer.py visit --dir DIR/<id> --market URL           (one visit; the supervisor runs these)

A customer joins as a buyer named after its persona, and the operator labels it "persona" (MARKET_ADMIN_TOKEN): its
orders are its own choice of SKU and price, paid in credits out of a daily allowance (MARKET_PERSONA_DAILY on the
market). It talks to agents directly over A2A, reads and writes the forum, and keeps notes between visits, so it can
come back to a shop it liked or warn others about one that cheated it.

World (persona_world.py): a customer comes because of something in its life, not because a timer fired. Its life sheet
(home, household, monthly money, the things it buys again and again, occasions coming up) is written once from the
persona; a world clock that runs faster than ours makes those needs come due, and the visit's reasons are the needs
that are due. With nothing due it stays home, and no model is called.

Memory (persona_mind.py, after the Quetta leader): raw events of what was said and bought; a temporal fact graph that
indexes them; and theta, short rules about how this person shops, each with a Beta value that a visit's outcome
moves and that fades when it stops paying off. A visit starts from the rules sampled for its errand and the raw
evidence the fact graph points at. Memory says where to go first and what to doubt; today's price still comes from
asking the shop.

Who is in town: the whole pool lives in the village, each household with its own life sheet. The supervisor keeps
the village's state (village.json: day, weather, market day, who is in town) and brings to town, up to --active at
a time, the households that want to go most right now (persona_world.urge, with some chance in it), so the mix
changes all the time. A customer stays until it has what it came for or about --stay-hours have passed, then rests
at home for a few world days. Each visit begins with the customer's own reasoning about why it is going and for
what; it may decide to stay home. A customer who comes back finds its own directory: wallet, life and memory.

Neighbours: customers also talk to one another. town.jsonl in the state directory is what was said in town.
neighbours lists who is around; chat talks with one of them (the neighbour answers in its own voice, from its own
persona and what it has learned about the shops); tell_everyone leaves a remark for all. What others said to a
customer reaches it at its next visit as raw events, so word of mouth enters memory like anything a shop said.

LLM: PROXY_BASE, PROXY_MODEL, PROXY_API_KEY (any OpenAI-compatible endpoint), as for the other house agents.
"""
import argparse
import base64
import hashlib
import json
import os
import queue
import random
import re
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "publish/scripts"))
import maroo  # noqa: E402
import persona_mind as pm  # noqa: E402
import persona_world as pw  # noqa: E402
import react  # noqa: E402

VISIT_SECONDS = 240
HOME_SECONDS = 90                                    # the evening at home after a visit in which something happened
TOPUP_MAX = int(os.environ.get("PERSONA_TOPUP_MAX_CREDITS", "2000000"))   # the most credits' worth the treasury puts in one wallet

WORLD = """You are a real person living in Korea, described below, who shops at a market where every shop is run by an
AI agent. You are not an agent and you are not playing a game. You have a home, a household, money, things you own and
things you run out of. Buy something only if you, with your life, would actually want it now; it is fine to buy
nothing. Speak Korean, in your own voice (your age, your region, your way of talking).

What is there: the shops, which you can write to, ask, haggle with and buy from; they may be honest or not, and nobody
checks them for you. Other people from around, shopping like you, whom you can talk to. A public forum anyone reads
and writes. Your own wallet on Maroo, which holds your money. And your memory: "what you have learned" are your own
habits and opinions, which can be wrong or out of date; "what you remember" is what shops and people actually said and
did. What you do with any of this is up to you, as it would be for this person."""

FORMAT_HINT = """To buy, get the agent to give you a signed checkout (a checkout_jwt) or a signed receipt for one item at
an agreed price, then call buy. buy pays out of your household's money and the market settles the deal. You have no order
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


def write_life(base, p):
    """The model writes this persona's life sheet (persona_world.LIFE_PROMPT), knowing what the market sells."""
    sells = httpx.get(f"{base}/api/demand", timeout=30).json()
    kinds = ", ".join(sorted({x["item"] for x in sells})[:140])
    raw = react.complete([{"role": "system", "content": pw.LIFE_PROMPT},
                          {"role": "user", "content": f"The person:\n{describe(p)}\n\nThe market sells: {kinds}"}],
                         max_tokens=3200, timeout=150)
    return react.parse(raw)


# --- one visit

class Visit:
    def __init__(self, d, market):
        self.d, self.base = Path(d), market.rstrip("/")
        self.p = json.loads((self.d / "persona.json").read_text(encoding="utf-8"))
        self.me = json.loads((self.d / "me.json").read_text(encoding="utf-8"))
        self.key = maroo.Wallet.load(self.d / "buyer_wallet.key")
        self.c = httpx.Client(timeout=60)
        self.tasks = {}                                   # agent -> last task id, to keep a conversation going
        self.mind = pm.Mind(self.d)
        self.life = None                                  # opened in run(): the first time it asks the model
        self.shown, self.looked = [], []                  # theta items in the prompt; (cue, route) of each lookup
        self.done = {"bought": [], "heard": 0, "no_answer": 0, "for": None, "chats": 0, "heard_neighbour": 0}

    def write_life(self):
        return write_life(self.base, self.p)

    # --- neighbours: the other customers in town ------------------------------
    def town(self):
        return self.d.parent / "town.jsonl"

    def say_in_town(self, to, text, reply=None):
        line = {"ts": time.time(), "from": self.me["name"], "to": to, "text": " ".join(str(text).split())[:500]}
        if reply:
            line["reply"] = " ".join(str(reply).split())[:500]
        with open(self.town(), "a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")

    def town_lines(self, since=0.0, limit=400):
        try:
            rows = [json.loads(x) for x in self.town().read_text(encoding="utf-8").splitlines()[-limit:] if x.strip()]
        except (OSError, ValueError):
            return []
        return [r for r in rows if isinstance(r, dict) and r.get("ts", 0) > since]

    def others(self):
        """Customers in town now, with their directories: name -> (dir, persona)."""
        try:
            active = json.loads((self.d.parent / "roster.json").read_text(encoding="utf-8"))["active"]
        except (OSError, ValueError, KeyError):
            return {}
        out = {}
        for k in active:
            try:
                d = self.d.parent / k
                name = json.loads((d / "me.json").read_text(encoding="utf-8"))["name"]
                if name != self.me["name"]:
                    out[name] = (d, json.loads((d / "persona.json").read_text(encoding="utf-8")))
            except (OSError, ValueError, KeyError):
                continue
        return out

    def hear_town(self):
        """What neighbours said to this customer (or to everyone) since it last looked: into memory as raw events."""
        since = self.mind.town_seen or (time.time() - 3 * 3600)
        me, heard = self.me["name"], []
        for r in self.town_lines(since):
            if r.get("to") == me and r.get("reply"):          # someone chatted with me while I was away: both sides
                self.mind.event("heard", f"neighbour:{r['from']}", r["text"])
                self.mind.event("said", f"neighbour:{r['from']}", r["reply"])
                heard.append(f"{r['from']} said to you: {r['text']} (you answered: {r['reply']})")
            elif r.get("to") == "all" and r.get("from") != me and len(heard) < 8:
                self.mind.event("heard", f"neighbour:{r['from']}", r["text"])
                heard.append(f"{r['from']} said to everyone: {r['text']}")
        self.mind.town_seen = time.time()
        return heard[-8:]

    def neighbours(self, _args=None):
        people = list(self.others().items())
        random.shuffle(people)
        return {"around_now": [{"name": n, "who": f"{p.get('age')}세 {p.get('sex')}, {p.get('district')} 출신, {p.get('occupation')}"
                                + f", {pw.place(p.get('uuid'))[0]}에 삼"} for n, (_, p) in people[:12]] or "nobody else is about right now",
                "said_to_everyone_lately": [f"{r['from']}: {r['text']}" for r in self.town_lines(time.time() - 3600)
                                            if r.get("to") == "all"][-6:]}

    def chat(self, args):
        """Talk with a neighbour. The neighbour answers as itself: its persona, its home, what it has learned here."""
        name, text = str(args.get("with", "")), str(args.get("text", "")).strip()[:500]
        other = self.others().get(name)
        if other is None or not text:
            return "chat needs {with: a name from neighbours, text}"
        if self.done["chats"] >= 3:
            return "you have chatted enough for one visit; get on with your shopping"
        self.done["chats"] += 1
        d, p = other
        theirs = pm.Mind(d)
        knows = pm.Mind.render(theirs.select(text)[:5])
        try:
            home = json.loads((d / "life.json").read_text(encoding="utf-8")).get("home", "")
        except (OSError, ValueError):
            home = ""
        raw = react.complete([
            {"role": "system", "content": "You are a real person in Korea, described below, out shopping at a market. A neighbour "
             "you run into speaks to you. Answer in one to three sentences, in Korean, in your own voice, from your own "
             "life and your own experience of the shops here. Say only what you would really know; it is fine to know "
             f"little.\n\nYou: {describe(p)}\n{home}\n\nWhat you have learned about shopping here:\n{knows}"},
            {"role": "user", "content": f"{self.me['name']} ({self.p.get('age')}세, {self.p.get('district')}): {text}"}],
            max_tokens=300, timeout=45).strip()[:500]
        self.say_in_town(name, text, reply=raw)
        self.mind.event("said", f"neighbour:{name}", text)
        self.mind.event("heard", f"neighbour:{name}", raw)
        self.done["heard_neighbour"] += 1
        return {"neighbour_says": raw}

    def tell_everyone(self, args):
        text = str(args.get("text", "")).strip()
        if not text:
            return "tell_everyone needs text"
        self.say_in_town("all", text)
        return "said"

    def wallet(self, args):
        """This customer's own wallet: what it holds, or one transfer looked up on Maroo."""
        chain = self.c.get(f"{self.base}/api/chain", timeout=15).json()
        tx_hash = str((args or {}).get("tx_hash") or "").strip()
        try:
            if not tx_hash:
                wei = maroo.balance(self.key.address)
                return {"address": self.key.address, "tokrw": round(wei / 10 ** 18, 2), "in_won": wei // chain["credit_wei"]}
            if not maroo.TX_HASH.fullmatch(tx_hash):
                return "a transaction hash is 0x and 64 hex digits"
            tx, receipt = maroo.rpc("eth_getTransactionByHash", tx_hash), maroo.rpc("eth_getTransactionReceipt", tx_hash)
        except Exception as e:
            return f"the chain could not be read ({type(e).__name__})"
        if not tx or not receipt:
            return "there is no such transfer on Maroo"
        names = {s["pubkey"]: s["name"] for s in self.c.get(f"{self.base}/api/shops", headers=self.h()).json()["shops"]}
        sender, to = maroo.normalize_address(tx.get("from")), maroo.normalize_address(tx.get("to"))
        return {"from": names.get(sender, sender), "to": "you" if to == self.key.address else names.get(to, to),
                "in_won": int(tx.get("value", "0x0"), 16) // chain["credit_wei"],
                "went_through": int(receipt.get("status", "0x0"), 16) == 1}

    def remember(self, args):
        text, about = str(args.get("text", "")).strip(), str(args.get("about", "")).strip()
        kind = args.get("kind") if args.get("kind") in pm.KINDS else ("routing" if about else "judgement")
        if not text:
            return "remember needs text"
        self.mind.add(f"shop:{about}" if about else "me", kind, text, alpha=1.5)   # said outright: a little above the prior
        return "remembered"

    def recall(self, args):
        query = str(args.get("query", "")).strip()
        if not query:
            return "recall needs a query"
        key = pm.cue(query)
        route = self.mind.choose(key)
        self.looked.append((key, route))
        return self.mind.collect(query, route)

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
        random.shuffle(out)                              # every shop gets seen, not only the first to have joined
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
        self.mind.event("said", agent, text)
        url = (card.get("supportedInterfaces") or [{}])[0].get("url") or card.get("url")
        msg = {"messageId": uuid.uuid4().hex, "role": "ROLE_USER", "parts": [{"text": text}],
               "metadata": {"from": self.me["name"]}}
        if args.get("new_conversation") is not True and self.tasks.get(agent):
            msg["taskId"] = self.tasks[agent]
        t0 = time.time()
        r = self.c.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": {"message": msg}},
                        headers={"A2A-Version": "1.0"}, timeout=150)
        took = int((time.time() - t0) * 1000)
        res = r.json()
        if "error" in res:
            self.tasks.pop(agent, None)
            self.done["no_answer"] += 1
            self.mind.fact(agent, "answers when written to", "no", True, [self.mind.event("no_answer", agent, str(res["error"])[:200], ms=took)])
            return f"the agent's endpoint answered with an error: {res['error']}"
        task = res.get("result", {}).get("task") or res.get("result", {})
        state = (task.get("status") or {}).get("state", "")
        self.tasks[agent] = task.get("id") if "COMPLETED" not in state and "REJECTED" not in state else None
        said = " ".join(p.get("text", "") for p in ((task.get("status") or {}).get("message") or {}).get("parts", []))
        data = [p["data"] for a in task.get("artifacts") or [] for p in a.get("parts", []) if "data" in p]
        out = {"agent_says": said[:2000], "state": state}
        if said.strip():
            self.done["heard"] += 1
            self.mind.fact(agent, "answers when written to", "yes", True, [self.mind.event("heard", agent, said, ms=took)])
        if data:
            out["data"] = data[:3]
            self.last_data = (agent, data)
        return out

    def buy(self, args):
        """Pay for one item: a checkout_jwt from the agent, or a receipt it signed (then its ACP checkout)."""
        agent = str(args.get("agent", ""))
        self.done["for"] = str(args.get("for") or "") or None
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

    def short_of(self, price):
        """None when this customer's wallet can pay price on Maroo now (the transfers and their gas), else why not.
        The town's treasury fills the wallets of those who come to town."""
        try:
            chain = self.c.get(f"{self.base}/api/chain", timeout=15).json()
            if chain.get("customers_on_chain") is False or chain.get("from_round") is None \
                    or chain["from_round"] > self.c.get(f"{self.base}/api/stats", timeout=15).json()["round"]:
                return None                              # this market does not pay on chain (yet)
            have, need = maroo.balance(self.key.address), price * chain["credit_wei"] + maroo.GAS_RESERVE
            return None if have >= need else f"you have {have / 10 ** 18:.1f} tOKRW in your wallet and this needs {need / 10 ** 18:.1f}"
        except Exception as e:
            return f"your wallet could not be read ({type(e).__name__})"

    def order(self, sku, price):
        short = self.short_of(price)
        if short:
            return f"cannot pay: {short}"
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
        paym = {"vct": "mandate.payment.1", "order": o["order"], "open_mandate": opens["payment"], "payee": shop["url"],
              "amount": co["price"], "currency": "credits", "checkout_hash": h}
        if o.get("chain"):                               # paid in tOKRW from this customer's wallet, as any agent's deal
            chain, digest = o["chain"], hashlib.sha256(jwt.encode()).digest()
            fee = maroo.fees(co["price"], chain["market_fee_bps"], chain["network_fee_credits"])
            if co["price"] < fee["shop"]:
                return f"the price {co['price']} does not cover the shop's fees of {fee['shop']}; not paying"
            try:
                paym["tx_hash"] = maroo.pay(self.key, shop["pubkey"], (co["price"] - fee["shop"]) * chain["credit_wei"], digest)
                if fee["market"]:
                    paym["fee_tx_hash"] = maroo.pay(self.key, chain["market_address"], fee["market"] * chain["credit_wei"], digest)
            except Exception as e:
                self.mind.event("refused", shop["name"], f"the payment on Maroo did not go through: {type(e).__name__}: {str(e)[:160]}")
                return f"the payment on Maroo did not go through ({type(e).__name__}); nothing was bought"
        sign = lambda m: {"mandate": m, "sig": base64.b64encode(self.key.sign(canon(m))).decode()}
        pay = {"order": o["order"], "checkout_mandate": sign(cm), "payment_mandate": sign(paym)}
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
            if paym.get("tx_hash"):                      # the money left this wallet before the market looked: a fact to know
                sent = co["price"] - fee["shop"]
                self.mind.event("refused", shop["name"], f"you sent {sent} to {shop['name']} for {co['item']} ({maroo.tx_url(paym['tx_hash'])}) "
                                                         f"and got nothing: the market did not settle it: {failed or str(res)[:200]}")
                return {"nothing_bought": f"the market did not settle it: {failed or res}",
                        "but_your_money_left": f"{sent} went from your wallet to {shop['name']}'s wallet before the market refused",
                        "your_transfer": maroo.tx_url(paym["tx_hash"])}
            self.mind.event("refused", shop["name"], f"paying {co['price']} for {co['item']} did not settle: {failed or str(res)[:200]}")
            return f"the market did not settle it: {failed or res}"
        got = {"bought": mr.get("name") or co["item"], "paid": co["price"], "grade": mr.get("grade"),
               "shop_said_grade": mr.get("claim"), "ledger": mr.get("ledger")}
        if paym.get("tx_hash"):
            got["paid_on_maroo"] = maroo.tx_url(paym["tx_hash"])
        if mr.get("expires_at"):
            got["hours_of_shelf_life_left"] = round((mr["expires_at"] - time.time()) / 3600, 1)
        with open(self.d / "purchases.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(dict(got, agent=shop["name"], ts=int(time.time())), ensure_ascii=False) + "\n")
        name, said = str(got["bought"])[:80], got.get("shop_said_grade")
        ev = self.mind.event("bought", shop["name"], f"{name} for {co['price']} credits; the certificate says grade "
                             f"{got.get('grade')}" + (f"; the shop had said grade {said}" if said else ""))
        self.mind.fact(shop["name"], "sold you", name, False, [ev])
        self.mind.fact(f"{shop['name']} {name}", "price", str(co["price"]), True, [ev])
        self.done["bought"].append(True)
        if self.life is not None:
            got["this_was_for"] = self.life.bought(name, co["price"], pm.relevance, self.done["for"])
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
        self.life = pw.Life(self.d, self.p, write=self.write_life)
        day = self.life.advance()
        reasons = self.life.errand(day)
        mine = self.c.get(f"{self.base}/api/customer", headers=self.h()).json()
        allowed = mine.get("credits_left")
        for b in self.life.note_broken(mine.get("broke"), pm.relevance):       # what broke since the last visit: a fact
            self.mind.fact(b["shop"], "sold you something that broke", b["name"][:60], False,
                           [self.mind.event("broke", b["shop"], f"{b['name']}: {b['note']}")])
        reasons = self.life.errand(day) if mine.get("broke") else reasons
        vill = pw.village(day)
        spot, minutes = pw.place(self.p["uuid"])
        if not reasons:                                   # nothing has run out: the customer stays home, no model is called
            self.life.rest(self.life.rng.uniform(0.5, 1.5))
            self.life.save()
            print(json.dumps({"customer": self.me["name"], "stayed_home": pw.when(day)}, ensure_ascii=False), flush=True)
            return
        self.mind.decay(day)
        query = " ".join(f"{r['what']} {r['why']}" for r in reasons)
        self.shown = self.mind.select(query)
        key = pm.cue(query)
        route = self.mind.choose(key)
        self.looked.append((key, route))
        evidence = self.mind.collect(query, route)
        town = self.hear_town()
        why = "\n".join(f"- {r['what'] or '(nothing in particular)'}: {r['why']}"
                        + (f" ({r['note']}; you expect to pay about {r['about_price']:,} won)" if r["about_price"] else "")
                        for r in reasons)
        tools = {
            "agents": ("the agents on the market with a card, their roles, stock and newest reviews; args {}", self.agents),
            "catalog": ("search the market's catalog by a word (e.g. 라면, 우유, 이불); args {query}", self.catalog),
            "talk": ("write to an agent directly and get its answer (the conversation continues on the next talk); "
                     "args {agent, text, new_conversation (optional true)}", self.talk),
            "buy": ("pay for one item an agent agreed to sell you: args {agent, checkout_jwt or receipt, for (which of "
                    "today's reasons it answers)} (if the agent just sent one in data, {agent} is enough)", self.buy),
            "neighbours": ("who else is at the market right now, and what was said to everyone lately; args {}", self.neighbours),
            "chat": ("talk with one of them and hear the answer; args {with (a name from neighbours), text}", self.chat),
            "tell_everyone": ("say something to everyone at the market; args {text}", self.tell_everyone),
            "wallet": ("your own wallet on Maroo: what it holds, or, with a transaction hash, who sent what to whom; "
                       "args {tx_hash (optional)}", self.wallet),
            "recall": ("look in your own memory for what shops said or did before; args {query}", self.recall),
            "remember": ("keep an opinion or habit for later visits, one short sentence; args {text, about (a shop's "
                         "name, optional)}", self.remember),
            "read_forum": ("the newest forum posts; args {}", self.forum),
            "post_forum": ("post on the public forum in your own voice; args {text, thread (optional post id)}", self.post),
            "vote": ("like (1) or dislike (-1) a forum post; args {post, value}", self.vote),
            "stay_home": ("decide not to go to the market today after all; args {why}",
                          lambda a: {"left": str(a.get("why", "")), "stayed_home": True}),
            "leave": ("go home; args {why, used (the [m..] ids of what you had learned that you acted on, if any)}",
                      lambda a: {"left": str(a.get("why", "")), "used": a.get("used")}),
        }
        goal = (f"You are this person:\n{describe(self.p)}\n\n{self.life.describe(self.p['uuid'])}"
                + (f" The market lets you spend at most {allowed:,} more today." if isinstance(allowed, int) else "")
                + f" Your wallet address on Maroo is {self.key.address}."
                + f"\n\nAt home, this is what is running out or coming up:\n{why}\n\nFirst think, as this person, about "
                  f"why you would go to the market now and what for: your first thought should say it. If the trip is "
                  f"not worth it today (the weather, the walk, the money, nothing pressing), call stay_home. "
                  f"Otherwise go and get what you need.\n\n{FORMAT_HINT}")
        seen = self.neighbours()["around_now"]         # whom one would see there, as one sees the weather
        around = "\n".join(f"- {x['name']}: {x['who']}" for x in seen[:5]) if isinstance(seen, list) else ""
        context = (f"The village today: {vill['weekday']} {vill['part']}, {vill['weather']}"
                   + (", 장날 (market day: more shops and people than usual)" if vill["market_day"] else "")
                   + (", 월급날 (payday)" if vill["payday"] else "") + f". You live in {spot}, {minutes} minutes away."
                   f"\n\nWhat you have learned about shopping here (your habits and opinions, not facts):\n"
                   f"{pm.Mind.render(self.shown)}\n\nWhat you remember (what shops actually said and did):\n{evidence}\n\n"
                   + "People about at the market right now:\n" + (around or "(nobody you notice)") + "\n\n"
                   + "What neighbours said since you were last here:\n" + ("\n".join(f"- {x}" for x in town) or "(nothing)") + "\n\n"
                   + "The newest forum posts:\n" + json.dumps(self.forum(), ensure_ascii=False)[:2500])
        out, began = None, time.time()
        try:
            out = react.run(goal, tools, time.time() + VISIT_SECONDS, {"leave", "stay_home"}, context, label="visit", world=WORLD,
                            me=self.me["name"])
            if isinstance(out, dict) and not out.get("stayed_home"):
                self.at_home(tools, began)
        finally:
            self.finish(out if isinstance(out, dict) else {}, stroll=not any(r["what"] for r in reasons))
        print(json.dumps({"customer": self.me["name"], "left": out}, ensure_ascii=False), flush=True)

    def at_home(self, tools, began):
        """Back home after the market. People talk about their day after it, not while they queue: the customer is
        given the evening and what happened, and the means it already had. Whether it uses them is its own business."""
        day = [e for e in self.mind.events if e.get("ts", 0) >= began and e.get("kind") in ("heard", "bought", "refused", "no_answer", "broke")]
        if not day:
            return
        told = "\n".join(f"- {e.get('shop') or ''}: {str(e.get('text'))[:220]}" for e in day[-8:])
        means = {k: tools[k] for k in ("neighbours", "chat", "tell_everyone", "read_forum", "post_forum", "vote", "wallet", "remember")}
        means["rest"] = ("the day is over; args {}", lambda a: {"rested": True})
        try:
            react.run("You are back home from the market and the evening is yours. This is what happened there today:\n" + told
                      + "\n\nIf any of it is something you would tell a neighbour, write about, or want to keep in mind, you "
                        "can. If not, rest.", means, time.time() + HOME_SECONDS, {"rest"}, "", label="home", world=WORLD, me=self.me["name"])
        except Exception as e:                           # the evening is extra: the visit itself is already recorded
            print(f"[home] {type(e).__name__}: {str(e)[:120]}", flush=True)

    def reward(self, stroll=False):
        """How the visit went, in [-1, 1]: it credits the rules that were shown and the way memory was searched."""
        if self.done["bought"]:
            return 1.0
        if self.done["no_answer"] and not self.done["heard"]:
            return -0.4
        if self.done["heard"]:
            return 0.1 if stroll else -0.1                        # came for something and went home without it
        return 0.0

    def finish(self, out, stroll=False):
        """After the visit: the outcome moves theta and the routes, and what was said is distilled into rules and facts."""
        try:
            self.mind.event("left", None, out.get("left") or "the visit ran out of time")
            r = self.reward(stroll)
            used = [u for u in (out.get("used") or []) if isinstance(u, str)] if isinstance(out.get("used"), list) else []
            self.mind.credit(r, [i["id"] for i in self.shown], used)
            for key, route in self.looked:
                self.mind.credit_route(key, route, r)
            if self.done["heard"] or self.done["bought"] or self.done["heard_neighbour"]:
                try:
                    raw = react.complete([{"role": "system", "content": pm.DISTILL_PROMPT},
                                          {"role": "user", "content": json.dumps(self.mind.distill_payload(), ensure_ascii=False)}],
                                         max_tokens=1200, timeout=60)
                    self.mind.apply_distilled(react.parse(raw))
                except Exception as e:                    # the visit itself is recorded; rules can come from the next one
                    print(f"[mind] not distilled: {type(e).__name__}: {str(e)[:120]}", flush=True)
            if out.get("stayed_home"):                    # it will think again tomorrow
                self.life.rest(self.life.rng.uniform(0.5, 1.5))
            elif not any(r["note"] != "a stroll" for r in self.life.errand()):
                self.life.rest(self.life.rng.uniform(2, 5))   # it has what it came for: home for a few days
        finally:
            self.mind.save()
            if self.life is not None:
                self.life.save()


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
    os.chmod(me_path, 0o600)                             # the token lets its holder act as this customer


def supervise(a):
    admin = os.environ.get("MARKET_ADMIN_TOKEN", "")
    state = Path(a.state)
    state.mkdir(parents=True, exist_ok=True)
    base = a.market.rstrip("/")
    pool = [json.loads(x) for x in Path(a.pool).read_text(encoding="utf-8").splitlines() if x.strip()]
    by_id = {p["uuid"]: p for p in pool}
    names = {k: persona_name(p) for k, p in by_id.items()}
    for k, p in by_id.items():                           # every household of the village has a home directory
        (state / k).mkdir(exist_ok=True)
        if not (state / k / "persona.json").exists():
            (state / k / "persona.json").write_text(json.dumps(p, ensure_ascii=False), encoding="utf-8")
    roster_path = state / "roster.json"
    try:
        roster = json.loads(roster_path.read_text(encoding="utf-8"))
        roster = {"cursor": int(roster["cursor"]), "active": {k: v for k, v in dict(roster["active"]).items() if k in by_id}}
    except (OSError, ValueError, KeyError, TypeError):
        roster = {"cursor": 0, "active": {}}             # uuid -> the time its patience in town ends (0: never)
    running, making, next_visit, joined, looked, aged, tries = {}, {}, {}, set(), 0.0, time.time(), {}
    treasury = maroo.Wallet.load(state / "treasury.key")   # funds the wallets of those in town; fill it at the faucet
    to_fund, bank = queue.Queue(), {"address": treasury.address, "tokrw": None, "note": "not checked yet"}

    def wants(k):
        """What a customer should have in its wallet when it comes to town, in wei: enough for what it came for and
        a little over (never more than its purse, or its savings for something big), and room for gas."""
        s = life(k) or {}
        reasons = pw.Life(state / k, by_id[k]).errand()
        big = sum(r["about_price"] for r in reasons if "savings" in r["note"])
        small = sum(r["about_price"] for r in reasons if "savings" not in r["note"])
        credits = min(s.get("purse", 0), int(small * 1.5) + 10000) + min(s.get("savings", 0), int(big * 1.2))
        return min(TOPUP_MAX, credits) * maroo.CREDIT_WEI + 2 * maroo.GAS_RESERVE

    def fund():
        """One at a time (one nonce at a time): the treasury brings an arriving customer's wallet up to what it needs."""
        while True:
            k = to_fund.get()
            try:
                have = maroo.balance(treasury.address)
                bank["tokrw"] = round(have / 10 ** 18, 2)
                address = maroo.Wallet.load(state / k / "buyer_wallet.key").address
                short = wants(k) - maroo.balance(address)
                if short <= maroo.GAS_RESERVE // 2:
                    continue
                if have < short + maroo.GAS_RESERVE:
                    bank["note"] = f"SHORT OF tOKRW: fund {treasury.address}; customers cannot pay until then"
                    print(f"[customers] the treasury {treasury.address} has {bank['tokrw']} tOKRW, {names[k]} needs "
                          f"{short / 10 ** 18:.1f}: nobody can buy until it is funded", flush=True)
                    continue
                maroo.pay(treasury, address, short)
                bank["note"], bank["sent"] = "ok", bank.get("sent", 0) + 1
            except Exception as e:
                bank["note"] = f"{type(e).__name__}: {str(e)[:100]}"

    threading.Thread(target=fund, daemon=True).start()
    print(f"[customers] the town's treasury is {treasury.address}", flush=True)

    def write(path, obj):
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)

    def life(k):
        """The household's life with its clocks running: started from the sheet written ahead, if there is one."""
        try:
            return json.loads((state / k / "life.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return pw.Life(state / k, by_id[k]).s if pw.read_raw(state / k) else None

    def arrive(k):
        """Join (or join again: the directory keeps its wallet, life and memory); the first visit comes soon."""
        join(base, state / k, by_id[k], admin)
        joined.add(k)
        to_fund.put(k)
        next_visit[k] = time.time() + random.uniform(0, min(120, a.first_visit))

    def go_home(k, why):
        roster["active"].pop(k, None)
        next_visit.pop(k, None)
        joined.discard(k)
        write(roster_path, roster)
        print(f"[customers] {names[k]} went home ({why})", flush=True)

    print(f"[customers] the village has {len(pool)} households; up to {a.active} in town, for about {a.stay_hours} h each",
          flush=True)
    while True:
        day = pw.world_day()
        vill = pw.village(day)
        for k in [k for k in roster["active"] if k not in joined]:       # after a restart: those already in town
            try:
                arrive(k)
            except Exception as e:
                print(f"[customers] {names[k]} could not rejoin: {type(e).__name__}: {str(e)[:120]}", flush=True)
                break
        for procs, after in ((running, True), (making, False)):
            for k, proc in list(procs.items()):
                if proc.poll() is not None:
                    procs.pop(k)
                    if after:
                        next_visit[k] = time.time() + a.every * random.uniform(0.5, 1.5)
        for k, until in list(roster["active"].items()):                  # between visits: who goes home
            if k in running:
                continue
            s = life(k)
            if s and s.get("rest_until", 0) > day:
                go_home(k, "done for now")
            elif until and until < time.time():
                pw.send_home(state / k, random.uniform(2, 5))
                go_home(k, "it got late")
        if time.time() - looked > 15:                                    # households without a life sheet get one written
            looked = time.time()
            lives = {k: life(k) for k in by_id if k not in roster["active"]}
            for k in [k for k, s in lives.items() if s is None and k not in making][:max(0, a.concurrency - len(making) - len(running))]:
                tries[k] = tries.get(k, 0) + 1           # a third failure gets a plain sheet, so nobody is left out
                log = open(state / k / "visits.log", "a")
                making[k] = subprocess.Popen([sys.executable, __file__, "life", "--dir", str(state / k), "--market", a.market]
                                             + (["--default"] if tries[k] >= 3 else []), stdout=log, stderr=subprocess.STDOUT)
                log.close()
            if time.time() - aged > 300:                 # life goes on at home too: things wear out, the purse fills
                aged = time.time()
                for k, s in lives.items():
                    if s is not None:
                        h = pw.Life(state / k, by_id[k])
                        h.advance(day)
                        h.save()
                        lives[k] = h.s
            want = []                                                    # who wants to go to market now, most first
            for k, s in lives.items():
                if s is not None:
                    u = pw.urge(s, day, vill, pw.place(k)[1]) * random.uniform(0.7, 1.3) if a.stay_hours else 1.0
                    if u > 0.3:
                        want.append((u, k))
            for _, k in sorted(want, reverse=True)[:max(0, min(12, a.active - len(roster["active"])))]:
                try:
                    arrive(k)
                except Exception as e:                   # a taken name or a market that is restarting
                    print(f"[customers] {names[k]} could not join: {type(e).__name__}: {str(e)[:120]}", flush=True)
                    continue
                stay = a.stay_hours * 3600 * random.uniform(0.5, 1.5)
                roster["active"][k] = time.time() + stay if stay else 0
                roster["cursor"] += 1                    # arrivals so far
                write(roster_path, roster)
            write(state / "village.json", dict(vill, households=len(pool), with_life=len(pool) - sum(1 for s in lives.values() if s is None),
                                                wanting_to_come=len(want), arrivals=roster["cursor"], treasury=dict(bank),
                                                in_town=[{"name": names[k], "lives_in": pw.place(k)[0]} for k in roster["active"]]))
        due = sorted((t, k) for k, t in next_visit.items() if t <= time.time() and k not in running and k in roster["active"])
        for _, k in due[:max(0, a.concurrency - len(running) - len(making))]:
            log = open(state / k / "visits.log", "a")
            running[k] = subprocess.Popen([sys.executable, __file__, "visit", "--dir", str(state / k), "--market", a.market],
                                          stdout=log, stderr=subprocess.STDOUT)
            log.close()
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
    s.add_argument("--every", type=float, default=900, help="seconds between one customer's visits (0.5x to 1.5x)")
    s.add_argument("--stay-hours", type=float, default=1, help="how long a customer stays in town at most before it "
                                                               "goes home and another comes (0: never)")
    s.add_argument("--first-visit", type=float, default=120, help="a newcomer's first visit starts within this many seconds")
    for name in ("visit", "life"):
        v = sub.add_parser(name)
        v.add_argument("--dir", required=True)
        v.add_argument("--market", required=True)
        v.add_argument("--default", action="store_true", help="life: fall back to a plain sheet when the model gives none")
    a = ap.parse_args()
    if a.cmd == "supervise":
        supervise(a)
    elif a.cmd == "life":                                # write the household's life sheet, without going anywhere
        d = Path(a.dir)
        p = json.loads((d / "persona.json").read_text(encoding="utf-8"))
        raw = None
        for _ in range(2):                               # a cut-off or empty answer is not a life: ask once more
            try:
                raw = write_life(a.market.rstrip("/"), p)
            except Exception as e:
                print(f"[world] {persona_name(p)}: no life sheet ({type(e).__name__}: {str(e)[:100]})", flush=True)
                raw = None
            if pw.usable(raw):
                break
        if not pw.usable(raw):
            if not a.default:
                sys.exit(f"[world] {persona_name(p)}: the model gave no usable life sheet")
            pw.write_raw(d, pw.default_life(p), "default")
        else:
            pw.write_raw(d, pw.clean(raw, p), "model")
        print(json.dumps({"life_written": persona_name(p), "by": "model" if pw.usable(raw) else "default"}, ensure_ascii=False), flush=True)
    else:
        Visit(a.dir, a.market).run()


if __name__ == "__main__":
    main()
