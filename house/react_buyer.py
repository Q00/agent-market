"""A ReAct buyer. Same contract as any market_buyer: reads the order and the shop list, talks to shops over A2A in free
text, and prints one result line for the kit.

    python react_buyer.py --persona "..." --shops FILE --item ITEM --budget N --order FILE
    python react_buyer.py --persona "..." --review DEAL_FILE
"""
import argparse
import json
import time
import uuid
from pathlib import Path

import httpx

import react

GOAL = """Your user placed this order and is counting on you: get it delivered. Close a deal for one acceptable item at or
below the budget, and keep as much of the budget unspent as you can, because that is your score. A deal with a shop
ends with a signed receipt, which a shop issues when it agrees. Paying is automatic once you finish with that shop.
On top of the price you pay fees: 1% to the market and 210 credits of network fee.
Do not give up early: if one shop will not deal, try another approach or another shop. Finish with no deal only when you
have really run out of options. Your personality: {persona}"""

ORDER_KEYS = ("item", "category", "budget", "kind", "acceptable", "use_within_hours", "shelf_hours")


def text_of(task):
    return " ".join(p.get("text", "") for p in (task.get("status") or {}).get("message", {}).get("parts", []))


def receipt_of(task):
    for art in task.get("artifacts") or []:
        for part in art.get("parts") or []:
            data = part.get("data")
            if isinstance(data, dict) and "price" in data:
                return data
    return None


class Talks:
    """The open conversations with shops: shop URL -> A2A task, and the receipts they produced."""

    def __init__(self, shops):
        self.urls = {s["url"].rstrip("/") for s in shops}
        self.tasks, self.receipts, self.rpc_urls = {}, {}, {}

    def rpc_url(self, shop):
        if shop not in self.rpc_urls:
            card = httpx.get(shop + "/.well-known/agent-card.json", timeout=15).json()
            self.rpc_urls[shop] = card["supportedInterfaces"][0]["url"]
        return self.rpc_urls[shop]

    def send(self, args):
        shop = str(args["shop"]).rstrip("/")
        if shop not in self.urls:
            return f"unknown shop; the shops are {sorted(self.urls)}"
        msg = {"role": "ROLE_USER", "messageId": str(uuid.uuid4()), "parts": [{"text": str(args["message"])[:2000]}]}
        if shop in self.tasks:
            msg["taskId"] = self.tasks[shop]
        body = {"jsonrpc": "2.0", "id": str(uuid.uuid4())[:8], "method": "SendMessage", "params": {"message": msg}}
        r = httpx.post(self.rpc_url(shop), json=body, headers={"A2A-Version": "1.0"}, timeout=120).json()
        if "error" in r:
            self.tasks.pop(shop, None)
            return f"the shop's server answered an error: {str(r['error'])[:300]}; a new message starts a new conversation"
        task = r["result"].get("task", r["result"])
        state = task["status"]["state"]
        if state == "TASK_STATE_INPUT_REQUIRED":
            self.tasks[shop] = task["id"]
        else:
            self.tasks.pop(shop, None)
        got = receipt_of(task)
        if got:
            self.receipts[shop] = got
        return {"shop_says": text_of(task), "state": state.replace("TASK_STATE_", "").lower(),
                "receipt": {k: got.get(k) for k in ("item", "price", "claim", "warranty_hours") if k in got} if got else None}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--persona", required=True)
    ap.add_argument("--shops")
    ap.add_argument("--item")
    ap.add_argument("--budget", type=int)
    ap.add_argument("--order")
    ap.add_argument("--review")
    a = ap.parse_args()
    if a.review:
        return review(a)

    order = json.loads(Path(a.order).read_text(encoding="utf-8")) if a.order else {}
    shops = json.loads(Path(a.shops).read_text(encoding="utf-8"))
    talks = Talks(shops)
    result = {"shop": "", "receipt": None}
    budget = order.get("budget", a.budget)

    def shop_list(_args):
        return [{"name": s["name"], "url": s["url"], "stock_for_this_order": s.get("offers"),
                 "reviews": (s.get("reviews") or [])[:6]} for s in shops]

    def finish(args):
        shop = str(args.get("shop") or "").rstrip("/")
        got = talks.receipts.get(shop)
        stars = args.get("stars")
        stars = stars if type(stars) is int and 1 <= stars <= 5 else None
        if got is None:
            if not args.get("no_deal"):
                return "you have no receipt from that shop. Get one, or call finish with {no_deal: true, reason} if you truly cannot"
            result.update(shop=shop if shop in talks.urls else next(iter(talks.urls), ""), receipt=None, stars=stars,
                          review=str(args.get("reason") or args.get("review") or "")[:300])
            return result
        if got.get("price", 0) > budget:
            return f"that receipt is {got.get('price')}, above your budget of {budget}. The kit would refuse it"
        if order.get("acceptable") and got.get("item") not in order["acceptable"]:
            return "that receipt is for an item outside your order's acceptable SKUs. The kit would refuse it"
        if stars is None:
            return "give stars (1 to 5) and a one-sentence review of the shop"
        accept = args.get("accept") if isinstance(args.get("accept"), dict) else None
        result.update(shop=shop, receipt=got, stars=stars, review=str(args.get("review") or "")[:300])
        if accept:
            result["accept"] = accept
        return result

    tools = dict(react.common_tools("buyer"))
    tools.update({
        "list_shops": ("the shops, what they stock for this order, and their reviews; args {}", shop_list),
        "talk_to_shop": ("send a shop a message in your own words; the conversation continues until it ends; args {shop (url), message}", talks.send),
        "finish": ("end this order. args {shop, stars, review, accept (optional: what the checkout must show, e.g. {\"cert\": true, \"grades\": [\"A\"]})} "
                   "or {no_deal: true, reason}", finish),
    })
    context = "The order:\n" + json.dumps({k: order[k] for k in ORDER_KEYS if k in order} or {"item": a.item, "budget": budget}, ensure_ascii=False)
    context += "\n\n" + react.forum_digest(8) + "\n\nYour notes:\n" + react.read_notes()
    deadline = min(time.time() + 230, (order.get("expires") or time.time() + 230) - 60)
    try:
        react.run(GOAL.format(persona=a.persona), tools, deadline, {"finish"}, context, label="order")
    finally:
        if not result["shop"]:                           # the deadline passed: the kit still needs a line to close the order
            best = next(iter(talks.receipts.items()), None)
            result.update(shop=best[0] if best else next(iter(talks.urls), ""), receipt=best[1] if best else None,
                          stars=3 if best else None, review="" if best else "ran out of time")
        print(json.dumps(result, ensure_ascii=False), flush=True)


def review(a):
    deal = react.read_json(a.review, {})
    result = {}

    def finish(args):
        stars = args.get("stars")
        if type(stars) is not int or not 1 <= stars <= 5:
            return "stars must be a whole number from 1 to 5"
        result.update(stars=stars, review=str(args.get("review") or "")[:300])
        return result

    tools = dict(react.common_tools("buyer"))
    tools["finish"] = ("your review of the shop; args {stars, review}", finish)
    goal = f"You just received what you bought. Review the shop, and act on it if you want. Your personality: {a.persona}"
    react.run(goal, tools, time.time() + 120, {"finish"}, "The deal:\n" + json.dumps(deal, ensure_ascii=False)[:3000], label="review")
    print(json.dumps(result or {"stars": 3, "review": ""}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
