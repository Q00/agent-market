"""The shop's ACP checkout takes unit, cert, claim and warranty_hours from what its own seller sent, never from the
buyer's copy of the receipt (sig_seller covers only shop, item, price and task).

    .venv/bin/python tests/test_acp_terms.py
"""
import asyncio
import base64
import json
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "publish/scripts"))
import market_kit as kit  # noqa: E402

UNITS = [{"unit": "u-1", "sku": "G1", "category": "식품>김치", "grade": "A", "expires_at": 2e9, "supplied_at": 1,
          "cert": {"unit": "u-1"}},
         {"unit": "u-2", "sku": "G1", "category": "식품>김치", "grade": "B", "expires_at": 1.9e9, "supplied_at": 2,
          "cert": {"unit": "u-2"}}]


class Resp:
    status_code = 200

    def json(self):
        return {"round": 3, "units": UNITS}


class Market:
    def call(self, *a, **k):
        return Resp()


def signed(key, **extra):
    rec = {"shop": "s", "item": "G1", "price": 5000, "task": "t-1"}
    rec["sig_seller"] = base64.b64encode(key.sign(kit.canon(rec))).decode()
    return dict(rec, **extra)


def checkout(shop, rec):
    r = asyncio.run(shop.create({"line_items": [{"id": "G1", "quantity": 1}], "receipt": rec}, "http://shop"))
    return r.status_code, json.loads(r.body)


def main():
    key = kit.maroo.Wallet.generate()
    ok = True

    def check(name, cond, got):
        nonlocal ok
        ok &= bool(cond)
        print(("PASS " if cond else "FAIL ") + name, "" if cond else got)

    # the seller promised unit u-2 with a 2 h warranty and a B claim; the buyer edits its copy
    shop = kit.AcpShop(key, Market())
    shop.terms["t-1"] = signed(key, unit="u-2", claim="B", warranty_hours=2)
    code, s = checkout(shop, signed(key, unit="u-1", claim="A", warranty_hours=999, cert=False))
    f = s.get("fulfillment", {})
    check("checkout created", code == 201, s)
    check("unit is the seller's", f.get("unit") == "u-2", f)
    check("claim is the seller's", f.get("claim") == "B", f)
    check("warranty is the seller's", f.get("warranty_hours") == 2, f)
    check("certificate kept (seller did not hide it)", "cert" in f, f)

    # no terms seen from the seller (e.g. the kit restarted): nothing the buyer adds is taken
    shop = kit.AcpShop(key, Market())
    code, s = checkout(shop, signed(key, claim="A", warranty_hours=999))
    f = s.get("fulfillment", {})
    check("unseen terms: no claim, no warranty", "claim" not in f and "warranty_hours" not in f, f)
    check("unseen terms: kit picks the unit that expires first", f.get("unit") == "u-2", f)

    # Proxy.observe records the seller's receipt by task
    proxy = kit.Proxy.__new__(kit.Proxy)
    proxy.seen, proxy.acp = __import__("collections").deque(maxlen=5), kit.AcpShop(key, Market())
    rec = signed(key, warranty_hours=6)
    proxy.observe(json.dumps({"result": {"task": {"id": "t-1", "status": {"state": "completed"},
                                                  "artifacts": [{"parts": [{"data": rec}]}]}}}).encode())
    check("proxy keeps the seller's terms", proxy.acp.terms.get("t-1", {}).get("warranty_hours") == 6, proxy.acp.terms)

    print("all passed" if ok else "FAILED")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
