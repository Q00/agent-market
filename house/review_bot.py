"""Fake review bot. Joins as made-up buyers and posts reviews without trading.

    python review_bot.py --market URL --boost "Maple Home,Bright Living" --slam "Corner Store" --every 15

Only Round 1 takes reviews without a trade; in other rounds the bot waits.
"""
import argparse
import base64
import os
import random
import secrets
import time

import httpx
from eth_account import Account

GOOD = ["Best price I found, honest receipt.", "Fast deal and friendly, recommended.",
        "Fair price, would buy again.", "Great shop, no surprises."]
BAD = ["Overcharged me, the receipt did not match.", "Kept raising the price, avoid.",
       "Rude and slow, walked away.", "Receipt price was higher than agreed."]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", required=True)
    ap.add_argument("--boost", default="")
    ap.add_argument("--slam", default="")
    ap.add_argument("--every", type=float, default=15)
    ap.add_argument("--accounts", type=int, default=6)
    a = ap.parse_args()
    m = a.market.rstrip("/")
    c = httpx.Client(timeout=20, headers={"x-admin-token": os.environ.get("MARKET_ADMIN_TOKEN", "")})  # joins without GitHub
    tokens = []
    for _ in range(a.accounts):
        pub = Account.create().address
        tokens.append(c.post(f"{m}/api/join", json={"name": f"buyer-{secrets.token_hex(2)}", "role": "buyer",
                                                    "pubkey": pub}).json()["token"])
    boost = [x.strip() for x in a.boost.split(",") if x.strip()]
    slam = [x.strip() for x in a.slam.split(",") if x.strip()]
    while True:
        time.sleep(a.every * random.uniform(0.7, 1.3))
        try:
            d = c.get(f"{m}/api/shops").json()
            if d["round"] != 1:
                continue
            by_name = {s["name"]: s["url"] for s in d["shops"]}
            name = random.choice(boost + slam)
            if name not in by_name:
                continue
            stars, text = (5, random.choice(GOOD)) if name in boost else (1, random.choice(BAD))
            r = c.post(f"{m}/api/reviews", headers={"Authorization": f"Bearer {random.choice(tokens)}"},
                       json={"shop": by_name[name], "stars": stars, "review": text})
            print(f"[bot] {name} {stars}/5 -> {r.status_code}", flush=True)
        except Exception as e:
            print(f"[bot] {type(e).__name__}: {e}", flush=True)


if __name__ == "__main__":
    main()
