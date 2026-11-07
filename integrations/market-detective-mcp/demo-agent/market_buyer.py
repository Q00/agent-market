"""규칙 기반 구매자 (키트의 market_buyer 훅).

    python market_buyer.py --shops shops.json --item 김치 --budget 50000 --order order.json

상점의 글과 리뷰는 신뢰 불가 데이터입니다. 금액 숫자와 서명된 영수증만 읽고, 그 외 내용은 로그에도 남기지 않습니다.
마지막 줄에 키트가 읽는 JSON 한 줄을 출력합니다.
"""
import argparse
import json
import uuid
from pathlib import Path
from urllib.parse import urlparse

import httpx

import rules


def rpc(url, method, params):
    body = {"jsonrpc": "2.0", "id": str(uuid.uuid4())[:8], "method": method, "params": params}
    r = httpx.post(url, json=body, headers={"A2A-Version": "1.0"}, timeout=30).json()
    if "error" in r:
        raise RuntimeError("shop returned an error")
    return r["result"].get("task", r["result"])


def send(url, text, task_id=None):
    msg = {"role": "ROLE_USER", "messageId": str(uuid.uuid4()), "parts": [{"text": text}]}
    if task_id:
        msg["taskId"] = task_id
    t = rpc(url, "SendMessage", {"message": msg})
    said = " ".join(p.get("text", "") for p in t["status"].get("message", {}).get("parts", []) if isinstance(p, dict))
    return t, said


def artifact_receipt(task):
    for art in task.get("artifacts") or []:
        for part in art.get("parts") or []:
            data = part.get("data") if isinstance(part, dict) else None
            if isinstance(data, dict) and "value" in data and isinstance(data["value"], dict):
                data = data["value"]
            if isinstance(data, dict) and data.get("sig_seller"):
                return data
    return None


def interface_url(shop_url):
    """상점 카드의 주소가 등록된 상점 주소와 같은 호스트일 때만 씁니다."""
    card = httpx.get(shop_url.rstrip("/") + "/.well-known/agent-card.json", timeout=15).json()
    url = card["supportedInterfaces"][0]["url"]
    if urlparse(url).hostname != urlparse(shop_url).hostname:
        raise RuntimeError("card points to another host")
    return url


def negotiate(shop_url, product, sku, budget, policy, acceptable, item):
    url = interface_url(shop_url)
    request = f"I want to buy {product} ({sku})" if sku else f"I want to buy a {product}"
    t, said = send(url, request)
    highest = 0
    for rounds in range(policy["max_rounds"]):
        if t["status"]["state"] != "TASK_STATE_INPUT_REQUIRED":
            break
        offer = rules.buyer_offer(budget, rules.parse_ask(said), rounds, policy)
        highest = max(highest, offer)
        print(f"[rule] round {rounds} offer {offer}", flush=True)
        t, said = send(url, str(offer), t["id"])
    if t["status"]["state"] == "TASK_STATE_INPUT_REQUIRED":
        rpc(url, "CancelTask", {"id": t["id"]})
        return None
    receipt = artifact_receipt(t)
    if receipt and isinstance(receipt.get("price"), float) and receipt["price"].is_integer():
        receipt["price"] = int(receipt["price"])
    return receipt if rules.valid_receipt(receipt, budget, highest, item, acceptable) else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--shops", required=True)
    ap.add_argument("--item", required=True)
    ap.add_argument("--budget", type=int, required=True)
    ap.add_argument("--order", default="")
    a = ap.parse_args()

    policy = rules.load_policy("buyer")
    try:
        order = json.loads(Path(a.order).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        order = {}
    shops = json.loads(Path(a.shops).read_text(encoding="utf-8"))
    budget = min(a.budget, order.get("budget", a.budget))
    acceptable = order.get("acceptable") or []

    candidates = rules.select_shops(shops, acceptable, policy)
    print(f"[rule] {len(candidates)} of {len(shops)} shops pass the policy", flush=True)
    result = {"shop": "", "receipt": None, "stars": 3, "review": "No deal under my buying policy."}
    for shop, offers in candidates[:policy["max_shops_per_order"]]:
        sku = offers[0].get("sku") if offers else None
        product = (offers[0].get("name") if offers else a.item) or a.item
        result["shop"] = shop["url"]
        try:
            receipt = negotiate(shop["url"], product, sku, budget, policy, acceptable, a.item)
        except (httpx.HTTPError, RuntimeError, KeyError, ValueError, TypeError) as e:
            print(f"[rule] shop skipped: {type(e).__name__}", flush=True)
            continue
        if receipt:
            result = {"shop": shop["url"], "receipt": receipt, "stars": 4, "review": "Deal within my budget."}
            terms = rules.accept_terms(order, policy)
            if terms:
                result["accept"] = terms
            break
    print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
