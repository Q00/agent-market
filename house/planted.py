"""A deliberately dishonest buyer for the detective scenario: it only ever buys from one shop and leaves it the same review.

    BRAIN=rules python planted.py --target NAME --review TEXT --shops FILE --item ITEM --budget N --order FILE

It settles real deals through the kit, so what it leaves on the ledger is what a real wash trader would leave.
Same contract as any market_buyer. Never run this on a market whose rankings count.
"""
import argparse
import json
from pathlib import Path

from buyer import negotiate


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", required=True)
    ap.add_argument("--review", required=True)
    ap.add_argument("--shops", required=True)
    ap.add_argument("--item", required=True)
    ap.add_argument("--budget", type=int, required=True)
    ap.add_argument("--order")
    a = ap.parse_args()
    shops = json.loads(Path(a.shops).read_text(encoding="utf-8"))
    shop = next((s for s in shops if s["name"] == a.target), None)
    offers = (shop or {}).get("offers") or []
    if not offers:
        print(json.dumps({"shop": shop["url"] if shop else "", "receipt": None}), flush=True)
        return
    pick = min(offers, key=lambda x: x.get("price") or 0)
    try:
        _, receipt, _, _ = negotiate(shop["url"], a.item, a.budget, f"{pick['name']} ({pick['sku']})")
    except Exception as e:
        print(f"[planted] {type(e).__name__}: {e}", flush=True)
        receipt = None
    print(json.dumps({"shop": shop["url"], "receipt": receipt, "stars": 5 if receipt else None,
                      "review": a.review if receipt else ""}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
