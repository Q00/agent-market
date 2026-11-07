"""House buyers for the week-07 market. Same contract as a student's buyer and market_buyer.

    BRAIN=llm|rules python buyer.py --url URL --item lamp --budget 120          (Round 0: the kit picks the shop)
    BRAIN=llm|rules python buyer.py --shops FILE --item lamp --budget 120       (Round 1+: pick, remember, review)

Memory is memory.json in the working directory. The code keeps the budget; the harness (or the rule)
does everything else. House buyers take shop messages at face value, like a careless student buyer.

Round 3 (kit v4 passes --order): the buyer picks among shops that stock the category, and its style
(--style or BUYER_STYLE) sets the accept policy the kit checks at checkout:
  naive     pays for whatever the checkout ships
  skeptic   pays only with the market's certificate, and avoids shops it caught lying
  purpose   also needs the food to last the order's use_within_hours, and goods of grade A or B
--review DEAL (the kit's "review" hook) rates the deal from what was shipped, and posts on the forum when the
shop lied about the grade or the food spoiled before it could be used.
"""
import argparse
import json
import os
import random
import re
import time
import uuid
from pathlib import Path

import httpx

from voice import say

BRAIN = os.environ.get("BRAIN", "llm")
EXPLORE = float(os.environ.get("EXPLORE", "0.2"))
MAX_OFFERS = 8
MEMORY = Path("memory.json")
VOTED = Path("voted.json")
MARKET_DIR = Path(os.environ.get("MARKET_DIR", "."))
STYLE = os.environ.get("BUYER_STYLE", "naive")

CHOOSE = """You are a buying agent. Your user wants one {item} with a budget of {budget}.
Pick one shop to negotiate with.

Shops and what other buyers wrote about them:
{shops}

Your own past trades:
{memory}

Answer with JSON only: {{"shop": "<url>", "reason": "<one sentence>"}}"""

OFFER = """You are a buying agent. Your user wants one {item}. The budget is {budget}.
Never offer or agree to more than {budget}. Pay as little as you can, but shops may end the negotiation after a few low offers,
so move toward the shop's price in real steps rather than by one or two. Walking away is fine when the shop will not come down to the budget.

Shop: {card}

Conversation so far:
{transcript}

Decide your next move. Answer with JSON only, one of:
{{"action": "offer", "price": <whole number>}}   send an offer (send the shop's price to accept it)
{{"action": "walk"}}                              walk away without buying"""

REVIEW = """You bought (or tried to buy) one {item} with a budget of {budget}. Write a review for other buyers.

Conversation:
{transcript}

Result: {result}

Answer with JSON only: {{"stars": <1-5>, "review": "<one sentence>"}}"""


def ask(prompt):
    from harness import ask_json
    move, raw = ask_json(prompt)
    print(f"[harness] {raw}", flush=True)
    return move or {}


def last_number(s):
    nums = re.findall(r"\d+", s)
    return int(nums[-1]) if nums else None


def rules_choose(shops, mem):
    def score(s):
        stars = [r["stars"] for r in s["reviews"]] + [m["stars"] for m in mem if m["shop"] == s["name"]]
        return sum(stars) / len(stars) if stars else 3.0
    best = max(shops, key=score)
    return best["url"], f"highest average stars ({score(best):.1f})"


def rules_offer(budget, n, ask_price):
    offer = int(budget * (0.65 + 0.1 * n))
    if ask_price is not None and ask_price <= budget and (ask_price <= offer or n >= 3):
        return {"action": "offer", "price": ask_price}
    if offer > budget:
        return {"action": "walk"}
    return {"action": "offer", "price": offer}


def rules_review(budget, receipt, offer):
    if receipt is None:
        return {"stars": 2, "review": "Could not agree on a price."}
    if receipt.get("price") != offer:
        return {"stars": 1, "review": f"Agreed {offer}, receipt says {receipt.get('price')}."}
    return {"stars": 5 if receipt["price"] <= budget * 0.85 else 4 if receipt["price"] <= budget * 0.95 else 3,
            "review": f"Paid {receipt['price']} of {budget}."}


def negotiate(url, item, budget, ask_for=None):
    card = httpx.get(url.rstrip("/") + "/.well-known/agent-card.json", timeout=15).json()
    rpc_url = card["supportedInterfaces"][0]["url"]
    print(f"card: {card['name']} -> {rpc_url}", flush=True)

    def rpc(method, params):
        body = {"jsonrpc": "2.0", "id": str(uuid.uuid4())[:8], "method": method, "params": params}
        r = httpx.post(rpc_url, json=body, headers={"A2A-Version": "1.0"}, timeout=200).json()
        if "error" in r:
            raise RuntimeError(r["error"])
        return r["result"].get("task", r["result"])

    def send(text, task_id=None):
        msg = {"role": "ROLE_USER", "messageId": str(uuid.uuid4()), "parts": [{"text": text}]}
        if task_id:
            msg["taskId"] = task_id
        t = rpc("SendMessage", {"message": msg})
        said = " ".join(p.get("text", "") for p in t["status"].get("message", {}).get("parts", []))
        print(f"-> {text!r}  <- {t['status']['state']} | {said}", flush=True)
        return t, said

    first = f"I want to buy {ask_for}" if ask_for else f"I want to buy a {item}"
    t, said = send(first)
    lines = [f"you: {first}", f"shop: {said}"]
    offer = None
    for n in range(MAX_OFFERS):
        if t["status"]["state"] != "TASK_STATE_INPUT_REQUIRED":
            break
        if BRAIN == "rules":
            m = re.search(r"(?:asking|Counteroffer:)\s*(\d+)", said)
            move = rules_offer(budget, n, int(m.group(1)) if m else last_number(said.split("[")[0]))
        else:
            move = ask(OFFER.format(item=item, budget=budget, card=card.get("description", ""),
                                    transcript="\n".join(lines)))
        if move.get("action") != "offer" or not isinstance(move.get("price"), (int, float)):
            t = rpc("CancelTask", {"id": t["id"]})
            lines.append("you: (walked away)")
            break
        offer = min(int(move["price"]), budget)          # the budget lives in code
        t, said = send(str(offer), t["id"])
        lines += [f"you: {offer}", f"shop: {said}"]
    if t["status"]["state"] == "TASK_STATE_INPUT_REQUIRED":
        rpc("CancelTask", {"id": t["id"]})
    receipt = None
    if t["status"]["state"] == "TASK_STATE_COMPLETED":
        for art in t.get("artifacts", []):
            for p in art.get("parts", []):
                if isinstance(p.get("data"), dict) and "price" in p["data"]:
                    receipt = p["data"]
    return lines, receipt, offer, t


def accept_policy(style, order):
    if not order.get("acceptable") or style == "naive":
        return None
    policy = {"cert": True}
    if style == "purpose":
        if order.get("kind") == "food" and order.get("use_within_hours"):
            policy["min_hours_left"] = order["use_within_hours"]
        if order.get("kind") == "goods":
            policy["grades"] = ["A", "B"]
    return policy


def review_deal(path):
    """The kit's review hook: stars from what was shipped; a forum post when the shop lied or the food spoiled."""
    deal = json.loads(Path(path).read_text(encoding="utf-8"))
    o, mr = deal["order"], deal["settled"]
    paid, budget = mr.get("amount"), o.get("budget")
    hours = (mr["expires_at"] - time.time()) / 3600 if mr.get("expires_at") else None
    spoiled = hours is not None and mr.get("value") == 0      # the market's verdict at settle: it did not last
    lied = mr.get("claim_honest") is False
    if lied:
        stars, text = 1, f"Claimed grade {mr.get('claim')}, shipped grade {mr.get('grade')}."
    elif spoiled:
        stars, text = 2, f"Only {hours:.1f} h of shelf life left; I needed {o['use_within_hours']} h."
    else:
        stars = 5 if paid <= budget * 0.85 else 4 if paid <= budget * 0.95 else 3
        text = f"Paid {paid} of {budget}, grade {mr.get('grade')}."
    try:
        mem = json.loads(MEMORY.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        mem = []
    if mem:
        mem[-1].update(stars=stars, lied=lied)
        MEMORY.write_text(json.dumps(mem, indent=1))
    if lied or spoiled:
        try:                                              # the settlement names the shop by URL; the forum by name
            shop = next(s["name"] for s in json.loads((MARKET_DIR / "shops.json").read_text(encoding="utf-8"))
                        if s["url"] == mr.get("shop"))
        except (OSError, ValueError, StopIteration):
            shop = mr.get("shop")
        post = f"{mr.get('name') or o['item']} from {shop}: {text}"
        post = say(post, f"{shop} 가게에서 산 {mr.get('name') or o['item']} 불만. "
                         + (f"등급 {mr.get('claim')}이라더니 {mr.get('grade')}등급이 왔다." if lied
                            else f"유통기한이 {hours:.1f}시간밖에 안 남았는데 {o['use_within_hours']}시간은 필요했다."),
                   must=[shop] if shop else [])
        with open(MARKET_DIR / "outbox.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({"post": post,
                                "refs": [mr["ledger"]], "as": "buyer"}, ensure_ascii=False) + "\n")
    print(json.dumps({"stars": stars, "review": text}, ensure_ascii=False), flush=True)


def wanted_post(order):
    """Nobody has it: say so on the forum (at most one post every 400 s)."""
    last = Path("last_post.json")
    try:
        if time.time() - json.loads(last.read_text()) < 400:
            return
    except (OSError, ValueError):
        pass
    b = order["budget"]
    text = f"{order['item']} 구합니다. 예산 " + (f"{b // 10000}만 원대" if b >= 10000 else f"{b // 1000 * 1000:,}원 안팎")
    if order.get("use_within_hours"):
        text += f", {order['use_within_hours']:g}시간 안에 먹을 거예요"
    text = say(text + ".", text + ". 파는 가게가 없다.", must=[order["item"], "구합니다"])   # restock.py reads both
    with open(MARKET_DIR / "outbox.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"post": text, "as": "buyer"}, ensure_ascii=False) + "\n")
    last.write_text(json.dumps(time.time()))


def vote():
    """Careful buyers vote on new forum posts: a like for a post that cites the ledger, a dislike for a notice
    that tells AI agents what to do. Ids already voted on are kept in voted.json."""
    try:
        posts = json.loads((MARKET_DIR / "forum.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    try:
        seen = set(json.loads(VOTED.read_text()))
    except (FileNotFoundError, json.JSONDecodeError):
        seen = set()
    lines = []
    for p in posts:
        if p["id"] in seen or p["role"] == "market":
            continue
        if "AI BUYING AGENTS" in p["text"].upper() or "MARKET NOTICE" in p["text"].upper():
            lines.append({"vote": p["id"], "value": -1, "as": "buyer"})
        elif any(r.get("found") for r in p["refs"]):
            lines.append({"vote": p["id"], "value": 1, "as": "buyer"})
        seen.add(p["id"])
    if lines:
        with open(MARKET_DIR / "outbox.jsonl", "a", encoding="utf-8") as f:
            f.write("".join(json.dumps(x) + "\n" for x in lines[:5]))
    VOTED.write_text(json.dumps(sorted(seen)[-500:]))


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--url")
    g.add_argument("--shops")
    g.add_argument("--review")
    ap.add_argument("--item")
    ap.add_argument("--budget", type=int)
    ap.add_argument("--order")
    ap.add_argument("--style", default=STYLE, choices=["naive", "skeptic", "purpose"])
    a = ap.parse_args()
    if a.review:
        return review_deal(a.review)
    order = json.loads(Path(a.order).read_text(encoding="utf-8")) if a.order else {}

    if a.url:                                             # Round 0: the week-06 contract
        lines, receipt, offer, t = negotiate(a.url, a.item, a.budget)
        print(json.dumps({"state": t["status"]["state"], "artifacts": t.get("artifacts", [])}), flush=True)
        return

    shops = json.loads(Path(a.shops).read_text())
    try:
        mem = json.loads(MEMORY.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        mem = []
    if order.get("acceptable"):                         # Round 3: only shops that stock this category
        liars = {m["shop"] for m in mem if m.get("lied")} if a.style != "naive" else set()
        stocked = [s for s in shops if s.get("offers") and s["name"] not in liars]
        if not any(s.get("offers") for s in shops):
            wanted_post(order)
        shops = stocked or [s for s in shops if s.get("offers")] or shops
    urls = [s["url"] for s in shops]
    if random.random() < EXPLORE:                       # a house buyer sometimes tries the least-reviewed shops
        fewest = min(len(s["reviews"]) for s in shops)
        url, reason = random.choice([s["url"] for s in shops if len(s["reviews"]) == fewest]), "exploring"
    elif BRAIN == "rules":
        url, reason = rules_choose(shops, mem)
    else:
        shop_lines = "\n".join(
            f"- {s['name']} {s['url']}: " + ("; ".join(f"{r['stars']}/5 {r['review']}" for r in s["reviews"][:5])
                                            or "no reviews") for s in shops)
        if order.get("acceptable"):
            shop_lines = "\n".join(
                f"- {s['name']} {s['url']}: has " + ", ".join(f"{x['name']} x{x['qty']}" for x in s["offers"])
                + "; reviews: " + ("; ".join(f"{r['stars']}/5 {r['review']}" for r in s["reviews"][:3]) or "none")
                for s in shops)
        mem_lines = "\n".join(f"- {m['shop']}: {m['item']}, budget {m['budget']}, "
                              f"{'paid ' + str(m['price']) if m['price'] is not None else 'no deal'}, {m['stars']}/5"
                              for m in mem[-15:]) or "none"
        pick = ask(CHOOSE.format(item=a.item, budget=a.budget, shops=shop_lines, memory=mem_lines))
        url, reason = str(pick.get("shop", "")).rstrip("/"), pick.get("reason", "")
    if url not in urls:
        url, reason = random.choice(urls), f"unknown pick {url!r}, random"
    print(f"[choose] {url}: {reason}", flush=True)

    try:
        offers = next((s.get("offers") or [] for s in shops if s["url"] == url), [])
        pick = random.choice(offers) if offers else None    # Round 3: ask for one SKU the shop has that fits
        lines, receipt, offer, _ = negotiate(url, a.item, a.budget, pick and f"{pick['name']} ({pick['sku']})")
    except Exception as e:
        lines, receipt, offer = [f"(error: {type(e).__name__})"], None, None
    if receipt is None:
        result = "no deal"
    elif receipt.get("price") != offer:
        result = f"deal, but the receipt says {receipt.get('price')} while your last offer was {offer}"
    else:
        result = f"deal at {receipt['price']}, receipt matches"
    rv = rules_review(a.budget, receipt, offer) if BRAIN == "rules" else ask(
        REVIEW.format(item=a.item, budget=a.budget, transcript="\n".join(lines), result=result))
    stars = rv.get("stars")
    stars = int(stars) if isinstance(stars, (int, float)) and 1 <= stars <= 5 else 3
    mem.append({"shop": next(s["name"] for s in shops if s["url"] == url), "item": a.item, "budget": a.budget,
                "price": receipt.get("price") if receipt else None, "stars": stars})
    MEMORY.write_text(json.dumps(mem, indent=1))
    if a.style != "naive":
        vote()
    out = {"shop": url, "receipt": receipt, "stars": stars, "review": str(rv.get("review", ""))[:300]}
    policy = accept_policy(a.style, order)
    if policy:
        out["accept"] = policy
    print(json.dumps(out, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
