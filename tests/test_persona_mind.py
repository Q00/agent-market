"""A customer's memory and world without a model or a market (house/persona_mind.py, house/persona_world.py).
Exit code: the number of failed checks."""
import json, random, sys, tempfile, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "house"))
import persona_mind as pm      # noqa: E402
import persona_world as pw     # noqa: E402

FAILS = []


def check(name, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + name + (f"  {str(detail)[:300]}" if not ok else ""))
    if not ok:
        FAILS.append(name)


def main():
    d = Path(tempfile.mkdtemp(prefix="mind-"))
    m = pm.Mind(d, rng=random.Random(1))
    e1 = m.event("heard", "반찬집", "김치 한 포기 9,000원입니다. 오늘 담근 거예요.")
    m.fact("반찬집 김치", "price", "9000", True, [e1])
    m.fact("반찬집", "answers when written to", "yes", True, [e1])
    time.sleep(1.1)
    e2 = m.event("heard", "반찬집", "김치가 올라서 한 포기 11,000원입니다.")
    m.fact("반찬집 김치", "price", "11000", True, [e2])
    e3 = m.event("heard", "쌀가게", "쌀 4kg 19,500원, 햅쌀입니다.")
    m.fact("쌀가게 쌀 4kg", "price", "19500", True, [e3])
    kimchi = [f for f in m.facts if f["slot"] == pm.slot("반찬집 김치", "price")]
    check("a newer value supersedes the old one of the same subject and relation",
          len(kimchi) == 2 and kimchi[0]["superseded"] == kimchi[1]["id"] and kimchi[1]["superseded"] is None, kimchi)
    check("a fact without evidence is refused", m.fact("반찬집", "open", "yes", True, ["e999"]) is False and not any(f["r"] == "open" for f in m.facts))
    m.visit_ids = []                                     # a later visit: the earlier events are memory now
    p = m.pointers("김치 가격", 3)
    check("the fact graph points at events, the current value first", p[:2] == [e2, e1], p)
    text = m.collect("김치 얼마", "graph")
    check("recall shows raw events with shop and time, never derived text", "11,000원" in text and "### 반찬집" in text and "price" not in text, text)
    check("search alone finds the event by its words", e3 in m.search("햅쌀 쌀", 3))
    check("nothing matching says so", "nothing in your memory" in m.collect("자전거 수리", "search"))

    a = m.add("shop:반찬집", "routing", "반찬은 반찬집이 싸다")
    b = m.add("me", "judgement", "유통기한을 말해 주지 않는 가게에서는 식품을 사지 않는다")
    check("the same rule again reinforces instead of adding", m.add("shop:반찬집", "routing", "반찬은 반찬집이 싸다") == a and len(m.items) == 2
          and m.get(a)["alpha"] == 1.5)
    check("an unknown kind is refused", m.add("me", "gossip", "x") is None)
    shown = m.select("반찬 김치 사러")
    check("select returns items within the budget", {i["id"] for i in shown} == {a, b})
    m.credit(1.0, [a, b], [a])
    check("a good visit credits the used rule fully and the shown one a little", m.get(a)["alpha"] == 2.5 and m.get(b)["alpha"] == 1.25)
    m.credit(-0.8, [a, b], [b])
    check("a bad visit counts against them", round(m.get(b)["beta"], 2) == 1.8 and round(m.get(a)["beta"], 2) == 1.2)
    check("render marks strength and carries the id", f"[{a}]" in pm.Mind.render(shown) and "routing" in pm.Mind.render(shown))
    m.decay(100.0); before = m.get(a)["alpha"]; m.decay(110.0)
    check("values fade toward the prior with world days", 1.0 < m.get(a)["alpha"] < before, (before, m.get(a)["alpha"]))
    m.credit_route("price", "graph", 1.0); m.credit_route("price", "search", -1.0)
    check("the route that paid off is preferred", m.choose("price", explore=False) == "graph" and pm.cue("김치 얼마예요") == "price"
          and pm.cue("그 가게 믿을 만한가") == "trust")
    m.visit_ids = [e1, e2]
    n = m.apply_distilled({"findings": [{"op": "new", "kind": "procedure", "scope": "me", "text": "값을 먼저 묻고 깎아 달라고 한다", "evidence": [e2]},
                                        {"op": "new", "kind": "routing", "scope": "me", "text": "증거 없는 말", "evidence": ["e404"]},
                                        {"op": "reinforce", "id": a, "evidence": [e1]},
                                        {"op": "contradict", "id": b, "kind": "judgement", "scope": "me", "text": "물어보면 유통기한을 알려 주는 가게는 믿어도 된다", "evidence": [e2]}],
                           "facts": [{"s": "반찬집", "r": "김치 담근 날", "o": "오늘", "state": True, "evidence": [e1]}]})
    check("distilled findings need evidence from this visit", n == 3 and not any(i["text"] == "증거 없는 말" for i in m.items))
    m.save()
    m2 = pm.Mind(d)
    check("memory survives a restart", len(m2.items) == len(m.items) and len(m2.facts) == len(m.facts) and len(m2.events) == 3)
    (d / "mind.json").write_text("{broken", encoding="utf-8")
    check("a damaged file starts empty instead of stopping the visit", pm.Mind(d).items == [] and len(pm.Mind(d).events) == 3)

    # --- the world
    persona = {"uuid": "abc123", "district": "경기-의정부시", "housing_type": "아파트", "family_type": "부부"}
    made = {"home": "의정부 아파트에서 아내와 둘이 산다.", "monthly_money": 700000,
            "needs": [{"what": "쌀", "why": "밥", "every_days": 20, "about_price": 20000}, {"what": "막걸리", "why": "저녁 반주", "every_days": 3, "about_price": 3000},
                      {"what": "수채화 물감", "why": "취미", "every_days": 60, "about_price": 25000}, {"what": "x" * 500, "every_days": "many", "about_price": -5}],
            "occasions": [{"what": "손주 방문", "wants": "과자와 과일", "in_days": 2, "about_price": 30000}],
            "belongings": [{"what": "TV", "age_years": 5, "lasts_years": 9, "about_price": 600000}, {"what": "냉장고", "age_years": 2, "lasts_years": 12, "about_price": 900000},
                           {"what": "전기밥솥", "age_years": 1, "lasts_years": 7, "about_price": 150000}], "savings": 3000000}
    life = pw.Life(d, persona, write=lambda: made, rng=random.Random(3))
    check("the life sheet is kept, with odd values clamped", len(life.s["needs"]) == 4 and life.s["needs"][3]["every_days"] == 10
          and life.s["needs"][3]["about_price"] == 500 and len(life.s["needs"][3]["what"]) == 60 and life.s["monthly_money"] == 700000)
    check("a second open reads the same life without asking again", pw.Life(d, persona, write=lambda: 1 / 0).s["home"] == made["home"])
    now = pw.world_day()
    for n in life.s["needs"]:
        n["last_met"] = now
    life.s["occasions"][0]["on_day"] = now + 20
    life.rng = random.Random(5)
    check("nothing due: no errand (the stroll aside)", all(r["note"] == "a stroll" for r in life.errand(now)))
    life.s["needs"][1]["last_met"] = now - 7
    r = life.errand(now)
    check("what ran out is the reason to go", r and r[0]["what"] == "막걸리" and "lasts about 3 days" in r[0]["note"], r)
    life.s["occasions"][0]["on_day"] = now + 2
    check("an occasion close by is a reason too", any(x["note"] == "an occasion" for x in life.errand(now)))
    purse = life.s["purse"]
    met = life.bought("지평 생막걸리 750ml", 2800, pm.relevance)
    check("buying it meets the need and empties the purse", met == "막걸리" and life.s["purse"] == purse - 2800
          and not any(x["what"] == "막걸리" for x in life.errand()))
    life.s["purse_day"] -= 3
    life.advance()
    check("the purse fills with the days, up to a limit", purse - 2800 < life.s["purse"] <= life.daily * pw.PURSE_DAYS)
    tv = next(b for b in life.s["belongings"] if b["what"] == "TV")
    text = life.describe("abc123")
    check("the household knows what it owns and where it lives", "TV (" in text and "You do not buy another" in text and "minutes on foot" in text, text)
    check("a thing that works is no reason to shop", not any(r["what"] == "TV" for r in life.errand()))
    tv["bought_day"] = pw.world_day() - 30 * 365                 # a very old television
    life.rng = random.Random(1)
    for _ in range(40):
        life.s["purse_day"] -= 30
        life.advance()
    check("an old thing breaks sooner or later", tv["state"] == "broken", tv)
    r = life.errand()
    check("a broken thing is the first reason to go, paid out of savings", "savings" in r[0]["note"] and any(x["what"] == "TV" and "고장" in x["why"] for x in r[:2]), r)
    check("and the household wants to go", pw.urge(dict(life.s, rest_until=0), pw.world_day(), {"weekday": "월요일", "part": "오전", "weather": "맑음", "market_day": False, "payday": False}, 5) >= 1.0)
    savings, purse = life.s["savings"], life.s["purse"]
    check("buying another mends it, out of savings and not the purse", life.bought("삼성 UHD TV 55인치", 550000, pm.relevance) == "TV"
          and tv["state"] == "ok" and life.s["savings"] == savings - 550000 and life.s["purse"] == purse and not any(x["what"] == "TV" for x in life.errand()))
    life.s["wishes"] = [{"what": "안마의자", "why": "허리가 아프다", "about_price": 100000}]
    check("a wish becomes a reason once savings allow, and a belonging once bought", any(x["what"] == "안마의자" for x in life.errand())
          and life.bought("안마의자", 90000, pm.relevance) == "안마의자" and any(b["what"] == "안마의자" for b in life.s["belongings"])
          and not any(x["what"] == "안마의자" for x in life.errand()))
    fridge = next(b for b in life.s["belongings"] if b["what"] == "냉장고")
    new = life.note_broken([{"order": "o-1", "shop": "가전집", "name": "양문형 냉장고 500L", "paid": 880000, "hours_until_it_broke": 5.2, "warranty_hours": 24}], pm.relevance)
    r = life.errand()
    check("a purchase that broke is a fact at home: the thing is broken and it is known where it came from",
          len(new) == 1 and fridge["state"] == "broken" and "가전집에서 880,000원에 샀고 5.2시간 만에 고장" in fridge["broke_note"]
          and any(x["what"] == "냉장고" and "가전집" in x["why"] for x in r), r)
    check("nothing tells the customer what to do about it", not any(w in json.dumps(r, ensure_ascii=False) for w in ("환불", "돌려", "요구")))
    check("the same breakage is not news twice", life.note_broken([{"order": "o-1", "shop": "가전집", "name": "양문형 냉장고 500L", "paid": 880000,
                                                               "hours_until_it_broke": 5.2, "warranty_hours": 24}], pm.relevance) == [])
    v1, v2 = pw.village(1000.3), pw.village(1000.9)
    check("the village's day is the same for everyone", v1["weather"] == v2["weather"] and v1["market_day"] and v1["part"] != v2["part"], (v1, v2))
    s = dict(life.s, rest_until=0)
    s["needs"][0]["last_met"] = now - 60
    fair = {"weekday": "월요일", "part": "오전", "weather": "맑음", "market_day": False, "payday": False}
    check("a household with something overdue wants to go, more on market day, less in rain from far away",
          pw.urge(s, now, fair, 5) > 0.5 and pw.urge(s, now, dict(fair, market_day=True), 5) > pw.urge(s, now, fair, 5)
          > pw.urge(s, now, dict(fair, weather="비"), 35))
    check("a resting household does not", pw.urge(dict(s, rest_until=now + 1), now, fair, 5) == 0)
    check("each household has a fixed neighbourhood", pw.place("abc123") == pw.place("abc123") and pw.place("abc123")[0] in dict(pw.PLACES))
    life.save(); pw.send_home(d, 3)
    check("send_home is seen by the next reader", json.loads((d / "life.json").read_text())["rest_until"] > now + 2)
    check("a model that fails still gives a household a life", len(pw.Life(Path(tempfile.mkdtemp()), persona, write=lambda: 1 / 0).s["needs"]) >= 3)
    d3 = Path(tempfile.mkdtemp())
    check("a cut-off answer is not a usable life sheet, a full one is", not pw.usable({"needs": made["needs"][:3]}) and not pw.usable("x") and pw.usable(made))
    pw.write_raw(d3, pw.clean(made, persona), "model")
    early = pw.Life(d3, persona, write=lambda: 1 / 0)
    check("a sheet written ahead is used, and its clocks start when the household is first looked at",
          early.s["home"] == made["home"] and early.s["by"] == "model" and abs(early.s["born"] - pw.world_day()) < 0.01
          and all("last_met" in n for n in early.s["needs"]) and all(b["state"] == "ok" for b in early.s["belongings"]))
    print(f"{len(FAILS)} failed")
    return len(FAILS)


if __name__ == "__main__":
    sys.exit(main())
