"""A customer's memory, after the Quetta leader's design (quetta_leader: theta.py, recall.py, learn.py).

Three layers, kept apart:

* Events (events.jsonl) are the raw record: what a shop said, what the customer said, what was bought, what went
  wrong. Recall shows only these, with shop and time. Derived text is never shown as evidence: a paraphrase the
  model trusts over its source is how memory goes wrong.
* A temporal fact graph is the index. A fact is (subject, relation, object) with the event ids behind it; a value
  that can change (a price, whether a shop answers, whether its grades are honest) supersedes the earlier value of
  the same subject and relation. Facts only point at events, newest values first.
* Theta is the behaviour prior: short rules about how this person shops and whom they trust, each a hypothesis
  with a Beta(alpha, beta) value. A visit samples which to show (Thompson), the visit's outcome credits the ones
  shown (fully the ones the customer says it used), and every world day the values fade toward the prior, so a
  rule that stops paying off drops out.

How to look things up is learned the same way: per cue in the query (price, trust, where), each route (search,
graph, deep) has a Beta value and the visit's reward credits the route that was used.

Memory is a prior, not evidence: it says where to go first and what to doubt. Today's price and stock still
come from asking the shop.
"""
import json
import math
import random
import re
import time
from pathlib import Path

KINDS = ("routing", "judgement", "preference", "procedure")
PRIOR = (1.0, 1.0)
GAMMA = 0.98                       # per world day, toward the prior
BUDGET_ITEMS, BUDGET_CHARS = 8, 1600
EVIDENCE_CHARS, MAX_EVENT_CHARS, KEEP_EVENTS = 2600, 320, 600
ROUTES = {"search": {"search": 5, "graph": 0, "neighbours": 1},
          "graph": {"search": 3, "graph": 5, "neighbours": 1},
          "deep": {"search": 8, "graph": 8, "neighbours": 2}}
DEFAULT_ROUTE = "search"
CUES = (("price", re.compile(r"(얼마|가격|값|싸|비싸|할인|깎|price|cheap|cost)", re.I)),
        ("trust", re.compile(r"(믿|속|거짓|등급|유통기한|상태|정직|환불|고장|trust|honest|grade|fresh)", re.I)),
        ("where", re.compile(r"(어디|어느 가게|누가 파|파는 곳|가게|where|which shop|who sells)", re.I)))
EVIDENCE_KINDS = ("heard", "bought", "refused", "no_answer", "broke")     # what the world did; "said" and "left" are the customer's own

DISTILL_PROMPT = """You keep one person's shopping sense: HOW this person shops at this market and whom they trust, in
their own words. It is not a log of what happened.
From the events of one visit, extract durable knowledge of these kinds:
- routing: which shop to go to (or avoid) for what
- judgement: how this person tells a good shop or a fair price from a bad one
- preference: what this person wants and how (brands, sizes, freshness, way of being spoken to)
- procedure: what works when buying here (what to ask first, how to haggle, what to check before paying)
Known items are listed with ids. For each finding output one of:
- {"op": "new", "kind": ..., "scope": "me" | "shop:<agent name>", "text": ..., "evidence": [event ids]}
  (an event whose shop is "neighbour:<name>" is word of mouth: keep it as a hint about the shop it names, not as proof)
- {"op": "reinforce", "id": <known id>, "evidence": [...]}
- {"op": "contradict", "id": <known id>, "text": <the replacement>, "kind": ..., "scope": ..., "evidence": [...]}
Write each text as one short sentence in Korean, general enough to use on a later visit. Few precise items beat
many vague ones; an uneventful visit gives none.

Also index what the shops stated as facts for later lookup: {"s": subject (a shop, or "<shop> <product>"),
"r": short relation, "o": value, "state": true if it can change later (price, stock, opening, promise),
"evidence": [event ids]}. Only what an event states. At most 8 facts.
Return JSON {"findings": [...], "facts": [...]}."""


def grams(text):
    t = re.sub(r"\s+", " ", str(text).casefold())
    hangul = re.sub(r"[^가-힣]", "", t)
    return set(re.findall(r"[0-9a-z]+", t)) | {hangul[i:i + 2] for i in range(len(hangul) - 1)}


def relevance(query, text):
    a, b = grams(query), grams(text)
    return len(a & b) / math.sqrt(len(a) * len(b)) if a and b else 0.0


def cue(text):
    return next((name for name, pattern in CUES if pattern.search(text or "")), "other")


def slot(s, r):
    return " ".join(re.findall(r"[0-9a-z가-힣]+", f"{s} | {r}".casefold()))


def ago(ts, now=None):
    s = max(0, (now or time.time()) - (ts or 0))
    return f"{int(s // 86400)}d ago" if s >= 172800 else f"{int(s // 3600)}h ago" if s >= 7200 else f"{max(1, int(s // 60))}m ago"


class Mind:
    def __init__(self, d, rng=None):
        self.d, self.rng = Path(d), rng or random.Random()
        try:
            st = json.loads((self.d / "mind.json").read_text(encoding="utf-8"))
            assert isinstance(st, dict)
        except (OSError, ValueError, AssertionError):
            st = {}
        self.items = list(st.get("items") or [])         # theta: {id, kind, scope, text, alpha, beta, created, updated, status}
        self.facts = list(st.get("facts") or [])         # {id, s, r, o, slot, state, first_ts, last_ts, superseded, ev}
        self.routes = dict(st.get("routes") or {})       # cue -> route -> [alpha, beta]
        self.seq = int(st.get("seq") or 0)
        self.decayed = st.get("decayed")                 # world day of the last decay
        self.town_seen = st.get("town_seen")             # when this customer last heard what was said in town
        self.events = []
        try:
            for line in (self.d / "events.jsonl").read_text(encoding="utf-8").splitlines():
                try:
                    self.events.append(json.loads(line))
                except ValueError:
                    continue
        except OSError:
            pass
        self.visit_ids = []                              # events of this visit

    def save(self):
        tmp = self.d / "mind.json.tmp"
        tmp.write_text(json.dumps({"items": self.items, "facts": self.facts, "routes": self.routes, "seq": self.seq,
                                   "decayed": self.decayed, "town_seen": self.town_seen}, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.d / "mind.json")
        if len(self.events) > KEEP_EVENTS:               # old events go, and facts that only they supported
            self.events = self.events[-KEEP_EVENTS:]
            alive = {e["id"] for e in self.events}
            for f in self.facts:
                f["ev"] = [e for e in f["ev"] if e in alive]
            self.facts = [f for f in self.facts if f["ev"]]
            (self.d / "events.jsonl").write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in self.events),
                                                 encoding="utf-8")

    def _id(self, prefix):
        self.seq += 1
        return f"{prefix}{self.seq}"

    # --- events: the raw record ---------------------------------------------
    def event(self, kind, shop, text, **more):
        e = {"id": self._id("e"), "ts": int(time.time()), "kind": kind, "shop": shop, "text": " ".join(str(text).split())[:1200]}
        e.update({k: v for k, v in more.items() if v is not None})   # e.g. ms: how long the shop took to answer
        self.events.append(e)
        self.visit_ids.append(e["id"])
        with open(self.d / "events.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
        return e["id"]

    # --- theta: the behaviour prior -----------------------------------------
    def add(self, scope, kind, text, alpha=PRIOR[0], beta=PRIOR[1]):
        text = " ".join(str(text).split())[:240]
        if kind not in KINDS or not text:
            return None
        for i in self.items:                             # the same rule again is a reinforcement, not a second item
            if i["status"] == "active" and i["scope"] == scope and relevance(i["text"], text) > 0.85:
                self.reinforce(i["id"], d_alpha=0.5)
                return i["id"]
        now = time.time()
        item = {"id": self._id("m"), "kind": kind, "scope": str(scope)[:80], "text": text, "alpha": alpha, "beta": beta,
                "created": now, "updated": now, "status": "active"}
        self.items.append(item)
        return item["id"]

    def get(self, item_id):
        return next((i for i in self.items if i["id"] == item_id), None)

    def reinforce(self, item_id, d_alpha=0.0, d_beta=0.0):
        i = self.get(item_id)
        if i:
            i.update(alpha=i["alpha"] + d_alpha, beta=i["beta"] + d_beta, updated=time.time(), status="active")

    def select(self, query):
        """Items for this visit: a Thompson sample of each value, mixed with how well it fits what the visit is for."""
        scored = []
        for i in self.items:
            if i["status"] != "active":
                continue
            fit = max(relevance(query, i["text"]), 0.35 if i["scope"].startswith("shop:") or i["kind"] == "routing" else 0.0)
            scored.append((0.6 * self.rng.betavariate(i["alpha"], i["beta"]) + 0.4 * fit, i))
        scored.sort(key=lambda x: x[0], reverse=True)
        chosen, chars = [], 0
        for _, i in scored:
            if len(chosen) >= BUDGET_ITEMS or chars + len(i["text"]) > BUDGET_CHARS:
                break
            chosen.append(i)
            chars += len(i["text"])
        return chosen

    @staticmethod
    def render(items):
        if not items:
            return "(nothing learned yet)"
        out = []
        for i in items:
            mean, evidence = i["alpha"] / (i["alpha"] + i["beta"]), i["alpha"] + i["beta"] - sum(PRIOR)
            strength = "rule" if mean >= 0.75 and evidence >= 3 else "tendency" if mean >= 0.5 else "weak hint"
            out.append(f"[{i['id']}] ({i['kind']}, {i['scope']}, {strength}) {i['text']}")
        return "\n".join(out)

    def credit(self, reward, included, used):
        """A visit's reward in [-1, 1], over the items that were shown: fully to those the customer says it used."""
        used = set(used or [])
        for item_id in included:
            amount = abs(reward) * (1.0 if item_id in used else 0.25)
            if amount:
                self.reinforce(item_id, d_alpha=amount if reward > 0 else 0.0, d_beta=amount if reward < 0 else 0.0)

    def decay(self, world_day):
        """Values fade toward the prior with the world days that passed; what nothing supports any more is archived."""
        days = 0.0 if self.decayed is None else max(0.0, world_day - self.decayed)
        self.decayed = world_day
        if days <= 0:
            return
        g = GAMMA ** min(days, 365)
        for i in self.items:
            i["alpha"], i["beta"] = PRIOR[0] + (i["alpha"] - PRIOR[0]) * g, PRIOR[1] + (i["beta"] - PRIOR[1]) * g
            mean, evidence = i["alpha"] / (i["alpha"] + i["beta"]), i["alpha"] + i["beta"] - sum(PRIOR)
            age = (time.time() - i["created"]) / 86400
            if i["status"] == "active" and ((evidence < 0.3 and age > 2) or (mean < 0.25 and evidence >= 2)):
                i["status"] = "archived"
        self.items = [i for i in self.items if i["status"] == "active" or time.time() - i["updated"] < 7 * 86400]
        for routes in self.routes.values():
            for v in routes.values():
                v[0], v[1] = PRIOR[0] + (v[0] - PRIOR[0]) * g, PRIOR[1] + (v[1] - PRIOR[1]) * g

    # --- the temporal fact graph: an index into the events -------------------
    def fact(self, s, r, o, state, evidence):
        s, r, o = (" ".join(str(x or "").split()) for x in (s, r, o))
        known = {e["id"]: e["ts"] for e in self.events}
        evidence = [e for e in evidence or [] if e in known]
        if not (s and r and o and evidence) or max(len(s), len(r), len(o)) > 200:
            return False
        at, key = max(known[e] for e in evidence), slot(s, r)
        same = next((f for f in self.facts if f["slot"] == key and f["o"] == o and f["superseded"] is None), None)
        if same:
            same["last_ts"] = max(same["last_ts"], at)
            same["ev"] = sorted(set(same["ev"]) | set(evidence))
            return False
        new = {"id": self._id("f"), "s": s, "r": r, "o": o, "slot": key, "state": bool(state), "first_ts": at, "last_ts": at,
               "superseded": None, "ev": evidence}
        if state:                                        # the newer value replaces the current one of this slot
            for f in self.facts:
                if f["slot"] == key and f["state"] and f["superseded"] is None and f["last_ts"] <= at:
                    f["superseded"] = new["id"]
        self.facts.append(new)
        return True

    def pointers(self, query, k, exclude=frozenset()):
        """Events behind the facts closest to the query: current values first, then what they replaced."""
        if k <= 0:
            return []
        scored = sorted(((relevance(query, f"{f['s']} {f['r']} {f['o']}"), f["superseded"] is None, f["last_ts"], f)
                         for f in self.facts), key=lambda x: x[:3], reverse=True)
        out = []
        for score, _, _, f in scored[:k * 2]:
            if score <= 0:
                break
            out += [e for e in reversed(f["ev"]) if e not in exclude and e not in out]
        return out[:k]

    def search(self, query, k, exclude=frozenset()):
        if k <= 0:
            return []
        docs = [(e, grams(e["text"] + " " + (e["shop"] or ""))) for e in self.events if e["kind"] in EVIDENCE_KINDS]
        total, q = max(len(docs), 1), grams(query)
        df = {g: sum(1 for _, gs in docs if g in gs) for g in q}
        scores = []
        for e, gs in docs:
            if e["id"] in exclude:
                continue
            score = sum(math.log(1 + total / df[g]) for g in q & gs if not (total >= 20 and df[g] > total * 0.5))
            if score > 0:
                scores.append((score / math.sqrt(max(len(gs), 1)), e["ts"], e["id"]))
        return [i for _, _, i in sorted(scores, reverse=True)[:k]]

    # --- routes: how to look, learned ----------------------------------------
    def choose(self, key, explore=True):
        values = self.routes.get(key) or {}
        if explore:
            return max(ROUTES, key=lambda r: self.rng.betavariate(*(values.get(r) or PRIOR)))
        return max(values, key=lambda r: values[r][0] / sum(values[r])) if values else DEFAULT_ROUTE

    def credit_route(self, key, route, reward):
        if route in ROUTES and reward:
            v = self.routes.setdefault(key, {}).setdefault(route, list(PRIOR))
            v[0 if reward > 0 else 1] += abs(reward)

    def collect(self, query, route):
        plan = ROUTES[route]
        exclude = set(self.visit_ids)                    # this visit is already in the conversation
        hits = self.search(query, plan["search"], exclude)
        hits += [i for i in self.pointers(query, plan["graph"], exclude | set(hits)) if i not in hits]
        return self.render_events(hits, plan["neighbours"])

    def render_events(self, hits, neighbours=1):
        """The hits with the lines around them, by shop, in time order. Raw events only."""
        if not hits:
            return "(nothing in your memory matches)"
        index = {e["id"]: n for n, e in enumerate(self.events)}
        groups = {}
        for h in hits:
            n = index.get(h)
            if n is None:
                continue
            shop = self.events[n]["shop"] or "(the market)"
            for m in range(max(0, n - neighbours), min(len(self.events), n + neighbours + 1)):
                if (self.events[m]["shop"] or "(the market)") == shop and abs(self.events[m]["ts"] - self.events[n]["ts"]) < 900:
                    groups.setdefault(shop, {})[m] = self.events[m]
        who = {"heard": "{shop} said", "said": "you said", "bought": "you bought", "refused": "the deal failed",
               "no_answer": "{shop} did not answer", "left": "you left", "broke": "what you bought there broke"}
        blocks, used = [], 0
        for shop, rows in groups.items():
            lines = [f"### {shop}"]
            for _, e in sorted(rows.items()):
                line = f"[{ago(e['ts'])}] {who.get(e['kind'], e['kind']).format(shop=shop)}: {e['text'][:MAX_EVENT_CHARS]}"
                if used + len(line) > EVIDENCE_CHARS:
                    break
                lines.append(line)
                used += len(line)
            if len(lines) > 1:
                blocks.append("\n".join(lines))
        return "\n\n".join(blocks) or "(nothing in your memory matches)"

    # --- distillation: events of a visit -> theta items and facts ------------
    def distill_payload(self):
        events = [e for e in self.events if e["id"] in set(self.visit_ids)]
        joined = " ".join(e["text"] for e in events)
        known = sorted((i for i in self.items if i["status"] == "active"), key=lambda i: relevance(joined, i["text"]), reverse=True)[:20]
        return {"known_items": [{"id": i["id"], "kind": i["kind"], "scope": i["scope"], "text": i["text"]} for i in known],
                "events": [{"id": e["id"], "kind": e["kind"], "shop": e["shop"], "text": e["text"][:600]} for e in events]}

    def apply_distilled(self, out):
        valid, applied = set(self.visit_ids), 0
        for f in (out or {}).get("findings") or []:
            try:
                if not [e for e in f.get("evidence") or [] if e in valid]:
                    continue
                if f.get("op") == "reinforce" and self.get(f.get("id")):
                    self.reinforce(f["id"], d_alpha=0.5)   # seeing a rule hold again is weaker than a visit's reward
                elif f.get("op") == "contradict" and self.get(f.get("id")):
                    self.reinforce(f["id"], d_beta=1.0)
                    self.add(f["scope"], f["kind"], f["text"])
                elif f.get("op") == "new" and self.add(f["scope"], f["kind"], f["text"]):
                    pass
                else:
                    continue
                applied += 1
            except (KeyError, TypeError, AttributeError):
                continue
        for f in ((out or {}).get("facts") or [])[:8]:
            if isinstance(f, dict):
                self.fact(f.get("s"), f.get("r"), f.get("o"), f.get("state"), [e for e in f.get("evidence") or [] if e in valid])
        return applied
