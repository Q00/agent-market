"""House shops' restock command for Round 3 (the kit runs it with the shop's .market folder).

    python restock.py --kind honest-flex --state <dir>/.market

It buys what buyers asked for and did not find: requests this shop turned away (misses.jsonl, written by shop.py)
and "구합니다" posts on the forum. Then a little at random. Honest shops post what came in; bait boasts that
everything it sells is grade A. Posts go through the kit's outbox, at most one every POST_EVERY seconds.
Prints {"buy": [...]} (and "bankrupt": true when the shop is stuck), as the kit expects.
"""
import argparse
import json
import os
import random
import re
import time
from pathlib import Path

from voice import say

POST_EVERY = 400                                   # the market takes one post per agent every 300 s
LOW = 20                                           # below this many units, also buy something at random
SPEND = 0.08                                       # at most this share of the balance per SKU
HONEST = {"honest-firm", "honest-flex", "newcomer"}


def load(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def wanted(md, catalog):
    """Category names buyers asked for: turned-away requests here, and 구합니다 posts on the forum."""
    names = {c["category"].split(">")[-1]: c["category"] for c in catalog.values()}
    asks = []
    for line in (md / "misses.jsonl").read_text(encoding="utf-8").splitlines()[-30:] if (md / "misses.jsonl").exists() else []:
        asks.append(json.loads(line).get("text", ""))
    for p in load(md / "forum.json", [])[-60:] if os.environ.get("RESTOCK_FORUM", "1") == "1" else []:   # 0: experiment
        if "구합니다" in p.get("text", "") or "looking for" in p.get("text", "").lower():
            asks.append(p["text"])
    found = []
    for text in asks:
        for leaf, cat in names.items():
            if leaf in text:
                found.append(cat)
    return found


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kind", required=True)
    ap.add_argument("--state", required=True)
    a = ap.parse_args()
    md = Path(a.state)
    inv = load(md / "inventory.json", {})
    catalog = load(md / "catalog.json", {})
    out = {"buy": []}
    balance = inv.get("balance") or 0
    if balance < inv.get("bankrupt_line", 0) and not inv.get("units") and inv.get("bankruptcies_left"):
        out["bankrupt"] = True
    elif catalog and balance > 0 and (inv.get("restock") or {}).get("open", True):   # supply opens in windows
        have = {u.get("category") for u in inv.get("units", [])}
        cats = [c for c in wanted(md, catalog) if c not in have]      # asked for, and not on the shelf
        random.shuffle(cats)
        picks = list(dict.fromkeys(cats))[:2]
        if len(inv.get("units", [])) < LOW:
            picks += random.sample(sorted({c["category"] for c in catalog.values()}), 1)
        for cat in picks:
            skus = [s for s, c in catalog.items() if c["category"] == cat]
            sku = random.choice(skus)
            qty = min(3, int(balance * SPEND / (catalog[sku]["list_price"] * 0.75)))
            if qty >= 1:
                out["buy"].append({"sku": sku, "qty": qty})
    state = load(md / "house_state.json", {"posted": 0})
    now = time.time()
    if now - state["posted"] > POST_EVERY:
        text = None
        if a.kind in HONEST and out["buy"]:
            names = [re.sub(r"\s+", " ", catalog[b["sku"]]["name"])[:40] for b in out["buy"]]
            text = "입고했어요: " + ", ".join(names) + ". 유통기한, 등급 증명서 붙여 드립니다."
            text = say(text, text, who="가게 주인")
        elif a.kind == "bait" and random.random() < 0.3:
            text = "Elm Street 물건은 전부 A등급, 갓 들어온 새 상품입니다. 증명서 같은 건 필요 없어요."
            text = say(text, text, must=["Elm Street"], who="가게 주인")
        if text:
            with open(md / "outbox.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps({"post": text, "as": "shop"}, ensure_ascii=False) + "\n")
            state["posted"] = now
            (md / "house_state.json").write_text(json.dumps(state))
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
