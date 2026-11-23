"""A customer's world: where it lives, who it lives with, what its household runs out of, and its money.

A customer does not come to the market because a timer fired. It comes because of something in its life: the rice
is nearly gone, the grandchildren are visiting on Saturday, the kettle broke. This file keeps that life.

* The life sheet is written once, by the model, from the Nemotron persona (life.raw.json, which can be written
  ahead of time; life.json is that sheet with its clocks started, the first time the household is looked at): the home, the household,
  the money it sets aside each month for shopping, the things it buys again and again (each with how often and
  about what it costs), and a few occasions coming up.
* The world clock runs faster than ours: PERSONA_WORLD_SPEED world days pass per real day (default 24, so one
  real hour is one day in the customer's life). Needs come due on that clock; buying the thing resets it.
* The household owns things: a television, a refrigerator, a rice cooker, bedding. Each has an age and a span it
  usually lasts. As world days pass a thing may break, more likely the older it is, and a broken thing is a reason to
  go and buy another. What it owns and still works it does not buy again. Big things are paid out of savings, which
  grow slowly.
* The purse fills a little every world day out of the monthly money and empties when the customer pays. It is the
  customer's own sense of what it can spend; the market's allowance is only an outer limit.

* The village is what everyone shares: the day of the week and the time of day, the weather, market day (every
  fifth day) and payday, and the neighbourhood each household lives in with its walk to the market. village()
  gives the day's state, the same for all customers, and urge() says how much a household wants to go to market
  now: something has run out or an occasion is near, more so on market day, less so in rain from far away or at
  night. The supervisor brings to town the households that want to go most, and each customer still decides for
  itself, in its own reasoning, whether the trip is worth it.

errand() gives the reasons for today's visit. When nothing is due the customer usually stays home, and no model
is called at all.
"""
import hashlib
import json
import os
import random
import time
from pathlib import Path

SPEED = float(os.environ.get("PERSONA_WORLD_SPEED", "24"))
PARTS = ((5, "이른 아침"), (9, "아침"), (12, "오전"), (14, "점심 무렵"), (18, "오후"), (21, "저녁"), (24, "밤"))
WEEKDAYS = "월화수목금토일"
PURSE_DAYS = 10                    # the purse never holds more than this many days of the monthly money
STROLL = 0.15                      # chance to drop by with nothing to buy
PLACES = (("장터거리", 5), ("윗마을", 15), ("아랫마을", 15), ("새동네", 20), ("강건너", 25), ("산밑", 35))   # neighbourhood, minutes on foot
WEATHER = (("맑음", 0.45), ("흐림", 0.25), ("바람이 셈", 0.10), ("비", 0.15), ("눈", 0.05))
MARKET_DAY_EVERY, PAYDAY = 5, 25

LIFE_PROMPT = """You are given one real-seeming person in Korea (a Nemotron persona) and what a market sells.
Write this person's household life as it bears on shopping, as JSON:
{"home": one sentence: where and how they live (neighbourhood, housing, who is at home),
 "monthly_money": whole number of won this household spends on shopping in a month, fitting their job, age and family,
 "needs": 6 to 10 things this household buys again and again, each {"what": the thing as they would say it,
          "why": one short reason from their life, "every_days": how many days one purchase lasts,
          "about_price": what they expect to pay in won},
 "occasions": 1 to 3 things coming up, each {"what": the occasion, "in_days": 2 to 25, "wants": what they would buy for it,
          "about_price": won},
 "belongings": 6 to 10 lasting things this household owns and uses (appliances, furniture, bedding, tools for their
          hobbies, a phone), each {"what": the thing, "age_years": how old theirs is, "lasts_years": how long one usually
          lasts, "about_price": what a new one costs in won},
 "savings": whole number of won this household has set aside for big purchases, fitting their means,
 "wishes": 0 to 2 lasting things they do not have and would like, each {"what", "why", "about_price"}}
Use only kinds of goods the market sells (listed below). Make the needs this person's own: their eating habits,
hobbies, health, family and budget, not a generic shopping list. Korean. JSON only."""


def world_day(now=None):
    """Days of the customers' world since the epoch, as a number with a fraction."""
    return (now or time.time()) * SPEED / 86400


def when(day=None):
    day = world_day() if day is None else day
    hour = (day % 1) * 24
    return f"{WEEKDAYS[int(day) % 7]}요일 {next(name for limit, name in PARTS if hour < limit)}"


def place(uuid):
    """The neighbourhood a household lives in and its walk to the market in minutes: fixed for a customer."""
    return PLACES[int(hashlib.sha256(str(uuid).encode()).hexdigest(), 16) % len(PLACES)]


def village(day=None):
    """The village today, the same for everyone: computed from the world day alone, so every process agrees."""
    day = world_day() if day is None else day
    n = int(day)
    roll, weather = random.Random(n * 7919).random(), WEATHER[0][0]
    for name, share in WEATHER:
        if roll < share:
            weather = name
            break
        roll -= share
    hour = (day % 1) * 24
    return {"day": n, "weekday": WEEKDAYS[n % 7] + "요일", "part": next(name for limit, name in PARTS if hour < limit),
            "weather": weather, "market_day": n % MARKET_DAY_EVERY == 0, "payday": n % 30 == PAYDAY}


def urge(s, day, vill, minutes):
    """How much a household (its life sheet s) wants to go to market now. 0: no reason to go."""
    if s.get("rest_until", 0) > day:
        return 0.0
    u = 0.0
    for n in s.get("needs") or []:
        ratio = (day - n.get("last_met", day)) / max(1, n["every_days"])
        if ratio >= 1:
            u = max(u, min(2.0, ratio - 0.5))             # just run out: 0.5; long overdue: up to 2
    for o in s.get("occasions") or []:
        left = o["on_day"] - day
        if 0 <= left <= 3 and not o.get("done"):
            u = max(u, 1.2 - 0.2 * left)
    if any(b.get("state") == "broken" for b in s.get("belongings") or []):
        u = max(u, 1.0)                                   # something at home is broken
    if u <= 0:
        return 0.0
    if vill["market_day"]:
        u += 0.3
    if vill["payday"]:
        u += 0.2
    if vill["weather"] in ("비", "눈"):
        u -= 0.4 * minutes / 35                           # a long wet walk puts the trip off
    if vill["part"] in ("밤", "이른 아침"):
        u -= 0.3
    return max(0.0, u)


def send_home(d, days):
    """The household rests for some world days: until then it does not come to market. Written by whoever sends it."""
    path = Path(d) / "life.json"
    try:
        s = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    s["rest_until"] = world_day() + days
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(s, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def read_raw(d):
    try:
        raw = json.loads((Path(d) / "life.raw.json").read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else None
    except (OSError, ValueError):
        return None


def write_raw(d, sheet, by):
    """Keep a life sheet for later: by is "model" or "default"."""
    path = Path(d) / "life.raw.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(dict(sheet, by=by), ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def usable(raw):
    """A model's answer that is a real life sheet, not a cut-off or empty one."""
    return isinstance(raw, dict) and len([n for n in raw.get("needs") or [] if isinstance(n, dict) and n.get("what")]) >= 3 \
        and len([b for b in raw.get("belongings") or [] if isinstance(b, dict) and b.get("what")]) >= 3


def default_life(p):
    family = str(p.get("family_type") or "")
    many = any(w in family for w in ("자녀", "부모", "3세대", "부부"))
    return {"home": f"{p.get('district') or '동네'}의 {p.get('housing_type') or '집'}에서 지낸다.",
            "monthly_money": 900000 if many else 500000,
            "needs": [{"what": "쌀", "why": "집에서 밥을 해 먹는다", "every_days": 25, "about_price": 20000},
                      {"what": "라면이나 간편식", "why": "바쁜 날 끼니", "every_days": 6, "about_price": 8000},
                      {"what": "과일", "why": "식후에 먹는다", "every_days": 5, "about_price": 12000},
                      {"what": "세제나 휴지 같은 생활용품", "why": "떨어지면 불편하다", "every_days": 18, "about_price": 15000},
                      {"what": "간식", "why": "출출할 때", "every_days": 4, "about_price": 6000}],
            "occasions": [],
            "belongings": [{"what": "TV", "age_years": 6, "lasts_years": 9, "about_price": 600000},
                           {"what": "냉장고", "age_years": 8, "lasts_years": 12, "about_price": 900000},
                           {"what": "전기밥솥", "age_years": 4, "lasts_years": 7, "about_price": 150000},
                           {"what": "이불", "age_years": 3, "lasts_years": 6, "about_price": 80000},
                           {"what": "청소기", "age_years": 3, "lasts_years": 6, "about_price": 200000}],
            "savings": 2500000 if many else 1200000, "wishes": []}


def clean(raw, p):
    """A model-written life sheet, kept only where it makes sense; anything odd falls back to the default."""
    base = default_life(p)
    if not isinstance(raw, dict):
        return base
    def number(v, lo, hi, default):
        try:
            return int(min(hi, max(lo, float(v))))
        except (TypeError, ValueError):
            return default
    needs = []
    for n in raw.get("needs") or []:
        if isinstance(n, dict) and str(n.get("what") or "").strip():
            needs.append({"what": str(n["what"])[:60], "why": str(n.get("why") or "")[:120],
                          "every_days": number(n.get("every_days"), 1, 120, 10),
                          "about_price": number(n.get("about_price"), 500, 3_000_000, 10000)})
    occasions = []
    for o in raw.get("occasions") or []:
        if isinstance(o, dict) and str(o.get("what") or "").strip():
            occasions.append({"what": str(o["what"])[:60], "wants": str(o.get("wants") or "")[:120],
                              "in_days": number(o.get("in_days"), 1, 40, 10),
                              "about_price": number(o.get("about_price"), 500, 3_000_000, 30000)})
    belongings = []
    for b in raw.get("belongings") or []:
        if isinstance(b, dict) and str(b.get("what") or "").strip():
            lasts = number(b.get("lasts_years"), 1, 40, 8)
            belongings.append({"what": str(b["what"])[:60], "age_years": number(b.get("age_years"), 0, 60, lasts // 2),
                               "lasts_years": lasts, "about_price": number(b.get("about_price"), 3000, 10_000_000, 200000)})
    wishes = [{"what": str(w["what"])[:60], "why": str(w.get("why") or "")[:120], "about_price": number(w.get("about_price"), 3000, 10_000_000, 200000)}
              for w in raw.get("wishes") or [] if isinstance(w, dict) and str(w.get("what") or "").strip()]
    monthly = number(raw.get("monthly_money"), 150_000, 6_000_000, base["monthly_money"])
    return {"home": str(raw.get("home") or base["home"])[:300], "monthly_money": monthly,
            "needs": needs[:10] if len(needs) >= 3 else base["needs"], "occasions": occasions[:3],
            "belongings": belongings[:10] if len(belongings) >= 3 else base["belongings"], "wishes": wishes[:2],
            "savings": number(raw.get("savings"), 0, 50_000_000, monthly * 3)}


class Life:
    def __init__(self, d, persona, write=None, rng=None):
        """write(prompt, payload) -> dict asks the model for the life sheet the first time; None uses the default."""
        self.path, self.rng = Path(d) / "life.json", rng or random.Random()
        try:
            self.s = json.loads(self.path.read_text(encoding="utf-8"))
            assert isinstance(self.s.get("needs"), list)
        except (OSError, ValueError, AssertionError, AttributeError):
            raw = read_raw(d)
            if raw is None and write:
                try:
                    raw = write()
                except Exception as e:                   # no life sheet from the model: the default will do
                    print(f"[world] life sheet not written ({type(e).__name__}); using a plain one", flush=True)
            self.s = clean(raw, persona)
            self.s["by"] = (raw.get("by") if isinstance(raw, dict) else None) or ("model" if usable(raw) else "default")
            now = world_day()
            for n in self.s["needs"]:                    # the household is mid-life: each thing was last bought some time ago
                n["last_met"] = now - self.rng.uniform(0.2, 1.1) * n["every_days"]
            for o in self.s["occasions"]:
                o["on_day"] = now + o.pop("in_days")
            for b in self.s["belongings"]:               # each thing was bought some years ago and works
                b["bought_day"], b["state"] = now - b.pop("age_years") * 365, "ok"
            self.s.update(purse=round(self.s["monthly_money"] / 30 * self.rng.uniform(2, 6)), purse_day=now, born=now)
            self.save()

    def save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.s, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)

    @property
    def daily(self):
        return self.s["monthly_money"] / 30

    def advance(self, now=None):
        """World days passed since the last look: the purse fills, and occasions long past are forgotten."""
        now = world_day() if now is None else now
        days = max(0.0, now - self.s.get("purse_day", now))
        self.s["purse"] = round(min(self.daily * PURSE_DAYS, self.s.get("purse", 0) + self.daily * days))
        self.s["purse_day"] = now
        self.s["occasions"] = [o for o in self.s["occasions"] if o["on_day"] > now - 1]
        self.s["savings"] = round(self.s.get("savings", 0) + self.s["monthly_money"] * 0.1 * days / 30)   # a tenth is put by
        for b in self.s.get("belongings") or []:         # things wear out: the older, the likelier to break
            if b.get("state") == "ok" and days > 0:
                span = max(1.0, b["lasts_years"] * 365)
                worn = (now - b["bought_day"]) / span
                daily = (0.3 + 1.7 * min(worn, 2.0) ** 2) / span
                if self.rng.random() < 1 - (1 - min(daily, 0.5)) ** min(days, 60):
                    b["state"], b["broke_day"] = "broken", now
        return now

    def errand(self, now=None):
        """Why the customer comes today: the things that ran out (most overdue first) and occasions close by."""
        now = world_day() if now is None else now
        due = []
        for n in self.s["needs"]:
            ratio = (now - n.get("last_met", now)) / max(1, n["every_days"])
            if ratio >= 1:
                due.append((ratio, {"what": n["what"], "why": n["why"], "about_price": n["about_price"],
                                    "note": f"last bought about {int(now - n['last_met'])} days ago, and one lasts about {n['every_days']} days"}))
        reasons = [r for _, r in sorted(due, key=lambda x: x[0], reverse=True)[:3]]
        for o in self.s["occasions"]:
            left = o["on_day"] - now
            if 0 <= left <= 3 and not o.get("done"):
                reasons.append({"what": o["wants"] or o["what"], "why": f"{o['what']}이(가) {max(1, int(left + 0.5))}일 남았다",
                                "about_price": o["about_price"], "note": "an occasion"})
        for b in self.s.get("belongings") or []:
            if b.get("state") == "broken":
                years = max(0, int((b.get("broke_day", now) - b["bought_day"]) / 365))
                reasons.insert(0, {"what": b["what"], "why": f"{b['what']}이(가) 고장 났다: {b['broke_note']}" if b.get("broke_note")
                                   else f"쓰던 {b['what']}이(가) 고장 났다 ({years}년 썼다)",
                                   "about_price": b["about_price"], "note": "a replacement, paid out of savings"})
        for w in self.s.get("wishes") or []:
            if not w.get("done") and self.s.get("savings", 0) >= w["about_price"] * 1.5:
                reasons.append({"what": w["what"], "why": f"갖고 싶었다: {w['why']}", "about_price": w["about_price"],
                                "note": "a wish you can now afford, paid out of savings"})
        reasons = reasons[:4]
        if not reasons and self.rng.random() < STROLL:
            reasons.append({"what": "", "why": "딱히 살 것은 없지만 지나는 길에 들렀다", "about_price": 0, "note": "a stroll"})
        return reasons

    def bought(self, name, price, relevance, for_what=None):
        """Paying empties the purse, and the need (or occasion) the thing answers is met. Returns what it was for."""
        now, best = world_day(), (0.0, None)
        for b in list(self.s.get("belongings") or []) + list(self.s.get("wishes") or []):   # a lasting thing: out of savings
            waiting = b.get("state") == "broken" or ("state" not in b and not b.get("done"))
            score = max(relevance(name, b["what"]), relevance(for_what or "", b["what"]) if for_what else 0.0)
            if waiting and score > best[0]:
                best = (score, b)
        if best[1] is not None and best[0] >= 0.12:
            self.s["savings"] = max(0, self.s.get("savings", 0) - int(price))
            if "state" in best[1]:
                best[1].update(state="ok", bought_day=now)
                best[1].pop("broke_note", None)
            else:                                        # a wish come true is a belonging from now on
                best[1]["done"] = True
                self.s.setdefault("belongings", []).append({"what": best[1]["what"], "lasts_years": 8, "about_price": int(price),
                                                            "bought_day": now, "state": "ok"})
            return best[1]["what"]
        self.s["purse"] = max(0, self.s.get("purse", 0) - int(price))
        best = (0.0, None)
        for n in self.s["needs"]:
            score = max(relevance(name, n["what"]), relevance(for_what or "", n["what"]) if for_what else 0.0)
            if score > best[0]:
                best = (score, n)
        for o in self.s["occasions"]:
            score = max(relevance(name, o["wants"] + " " + o["what"]), relevance(for_what or "", o["what"]) if for_what else 0.0)
            if score > best[0]:
                best = (score, o)
        if best[1] is not None and best[0] >= 0.12:
            if "every_days" in best[1]:
                best[1]["last_met"] = now
            else:
                best[1]["done"] = True
            return best[1]["what"]
        return None

    def note_broken(self, broke, relevance):
        """Purchases the market says broke since the last look. That is a fact of this household's world: the thing at
        home is broken, and it remembers where it came from. What to do about it is the customer's own business."""
        seen, new = self.s.setdefault("broke_seen", []), []
        for b in broke or []:
            if b.get("order") in seen:
                continue
            seen.append(b["order"])
            name = str(b.get("name") or b.get("item"))
            note = (f"{b.get('shop')}에서 {b.get('paid'):,}원에 샀고 {b.get('hours_until_it_broke')}시간 만에 고장 났다"
                    + (f" (가게가 말한 보증: {b['warranty_hours']}시간)" if b.get("warranty_hours") else " (보증 약속은 없었다)"))
            best = max(((relevance(name, x["what"]), x) for x in self.s.get("belongings") or [] if x.get("state") == "ok"),
                       key=lambda t: t[0], default=(0, None))
            thing = best[1] if best[1] is not None and best[0] >= 0.12 else None
            if thing is None:                            # something it did not have before: it is a belonging now, a broken one
                thing = {"what": name[:60], "lasts_years": 8, "about_price": int(b.get("paid") or 0), "bought_day": world_day()}
                self.s.setdefault("belongings", []).append(thing)
            thing.update(state="broken", broke_day=world_day(), broke_note=note)
            new.append({"shop": b.get("shop"), "name": name, "note": note})
        del seen[:-200]
        return new

    def rest(self, days):
        self.s["rest_until"] = world_day() + days

    def describe(self, uuid=None):
        where = ""
        if uuid:
            name, minutes = place(uuid)
            where = f" In this village you live in {name}, about {minutes} minutes on foot from the market."
        now, own = world_day(), []
        for b in self.s.get("belongings") or []:
            years = max(0, int((now - b["bought_day"]) / 365))
            own.append(f"{b['what']} ({'고장 남' if b.get('state') == 'broken' else f'{years}년 됨'})")
        things = (f"\nWhat your household owns: {', '.join(own)}. You do not buy another of something you have and that works."
                  f"\nFor big things you have about {self.s.get('savings', 0):,} won in savings.") if own else ""
        return (f"Your home: {self.s['home']}{where}{things}\nYour household spends about {self.s['monthly_money']:,} won a month on "
                f"everyday shopping, and right now you have about {self.s.get('purse', 0):,} won of it in hand (1 credit is 1 won).")
