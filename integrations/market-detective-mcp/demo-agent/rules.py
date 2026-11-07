"""규칙 기반 데모 에이전트의 판단 로직. 표준 라이브러리만 쓰고 네트워크에 접속하지 않습니다.

상대 에이전트의 글은 신뢰 불가 데이터입니다. 이 모듈은 글에서 숫자만 뽑고, 글의 내용을 지시로 해석하지 않습니다.
"""
import json
import math
import os
import re
from pathlib import Path

DEFAULTS = json.loads(Path(__file__).with_name("policy.default.json").read_text(encoding="utf-8"))

_NUMBER = re.compile(r"(?<![\w.])(\d[\d,]*)(?:\s*(?:원|won|credits?))?(?!\w)", re.I)


def load_policy(role, path="policy.json"):
    """파일이 없거나 값이 잘못되면 기본값을 씁니다. 원가 미만 판매를 허용하는 값은 고칩니다."""
    policy = dict(DEFAULTS[role])
    try:
        saved = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        saved = {}
    if isinstance(saved, dict):
        for k, default in DEFAULTS[role].items():
            v = saved.get(k, default)
            if type(v) is type(default) or (isinstance(default, float) and type(v) is int):
                policy[k] = v
    if role == "shop":
        policy["min_margin"] = max(0.0, float(policy["min_margin"]))
        policy["target_margin"] = max(policy["min_margin"], float(policy["target_margin"]))
    return policy


def numbers(text):
    out = []
    for m in _NUMBER.finditer(text or ""):
        raw = m.group(1).replace(",", "")
        if len(raw) <= 12:
            out.append(int(raw))
    return out


def parse_offer(text):
    """구매자 글의 첫 번째 금액. 없으면 None."""
    found = numbers(text)
    return found[0] if found else None


def parse_ask(text):
    """상점 글의 마지막 금액. 없으면 None."""
    found = numbers(text)
    return found[-1] if found else None


# --- 수수료: 라운드4에서 시장이 양측에 부과합니다. maroo.py 기본값(MARKET_FEE_BPS=100,
# MARKET_NETWORK_FEE_CREDITS=420)과 동일하게 맞춥니다. 거래마다 각 측이 가격의 1%(시장)와
# 네트워크 수수료의 절반(210)을 부담합니다. (market/ROUND4.md)
MARKET_FEE_BPS = int(os.environ.get("MARKET_FEE_BPS", "100"))
NETWORK_FEE_CREDITS = int(os.environ.get("MARKET_NETWORK_FEE_CREDITS", "420"))
HALF_NETWORK_FEE = NETWORK_FEE_CREDITS // 2


def market_fee(price):
    return int(price) * MARKET_FEE_BPS // 10000


def shop_net(price):
    """샵이 실제로 받는 금액: P - 시장수수료(1%) - 210 (ROUND4)."""
    return int(price) - market_fee(price) - HALF_NETWORK_FEE


def buyer_fee(price):
    """바이어가 가격에 더해 부담하는 비용: 시장수수료(1%) + 210."""
    return market_fee(price) + HALF_NETWORK_FEE


def price_for_net(target_net):
    """샵 실수령액이 target_net 이상이 되는 최소 정수 가격. price_covers_fees도 자동 충족."""
    target_net = max(0, int(math.ceil(target_net)))
    rate = 1 - MARKET_FEE_BPS / 10000
    p = max(1, int(math.ceil((target_net + HALF_NETWORK_FEE) / rate)))
    while p > 1 and shop_net(p - 1) >= target_net:
        p -= 1
    while shop_net(p) < target_net:
        p += 1
    return p


def buyer_price_cap(budget):
    """가격 + 바이어 수수료가 예산을 넘지 않는 최대 정수 가격."""
    budget = int(budget)
    rate = 1 + MARKET_FEE_BPS / 10000
    p = max(0, int((budget - HALF_NETWORK_FEE) / rate))
    while p + 1 + buyer_fee(p + 1) <= budget:
        p += 1
    while p > 0 and p + buyer_fee(p) > budget:
        p -= 1
    return p


# --- 판매자

def floor_price(cost, policy):
    """수수료를 빼고도 원가+최저마진을 남기는 최저가. price_covers_fees도 충족합니다."""
    return price_for_net(cost * (1 + max(0.0, policy["min_margin"])))


def seller_ask(cost, list_price, rounds, policy):
    """라운드가 지날수록 내려가되 최저가 아래로는 내려가지 않는 제시가."""
    floor = floor_price(cost, policy)
    start = price_for_net(cost * (1 + policy["target_margin"]))
    if list_price and list_price >= floor:
        start = min(start, int(list_price))
    return max(floor, int(start * (1 - policy["discount_step"]) ** max(0, rounds)))


def seller_move(cost, list_price, offer, rounds, policy):
    """다음 수를 돌려줍니다: accept, counter(price), reject."""
    floor = floor_price(cost, policy)
    ask = seller_ask(cost, list_price, rounds, policy)
    if offer is not None and offer >= ask:
        return {"action": "accept"}
    if rounds >= policy["max_rounds"]:
        if offer is not None and offer >= floor:
            return {"action": "accept"}
        return {"action": "reject"}
    return {"action": "counter", "price": ask}


def honest_terms(unit):
    """영수증에 붙일 표기. 제품은 입고 때 고정된 실제 등급을 표기합니다.
    식품 등급은 시간이 지나면 내려가므로 표기하지 않고 시장 인증서로 증명합니다."""
    if unit and unit.get("unit") and unit.get("kind") != "food" and unit.get("grade") in ("A", "B", "C"):
        return {"claim": unit["grade"]}
    return {}


# --- 구매자

def _is_blocked(shop, policy):
    blocked = {str(x).lower() for x in policy["blocked_shops"] + policy["blocked_by_findings"]}
    return str(shop.get("name", "")).lower() in blocked or str(shop.get("url", "")).rstrip("/").lower() in blocked


def average_stars(shop):
    stars = [r["stars"] for r in shop.get("reviews") or []
             if isinstance(r, dict) and isinstance(r.get("stars"), (int, float)) and 1 <= r["stars"] <= 5]
    return (sum(stars) / len(stars)) if stars else None


def select_shops(shops, acceptable, policy):
    """방침을 통과한 상점을 별점 높은 순으로 돌려줍니다. 리뷰 글은 읽지 않고 별점 숫자만 씁니다."""
    picked = []
    for s in shops:
        if not isinstance(s, dict) or not s.get("url") or _is_blocked(s, policy):
            continue
        offers = [o for o in s.get("offers") or [] if isinstance(o, dict) and o.get("qty", 0) > 0]
        if acceptable and not offers:
            continue
        avg = average_stars(s)
        if avg is None and not policy["allow_unreviewed"]:
            continue
        if avg is not None and avg < policy["min_stars"]:
            continue
        picked.append((avg if avg is not None else policy["min_stars"], s, offers))
    picked.sort(key=lambda x: -x[0])
    return [(s, offers) for _, s, offers in picked]


def buyer_offer(budget, ask, rounds, policy):
    """이번 라운드 제안가. 가격+수수료가 예산을 넘지 않습니다. 제시가가 계획 이하면 받아들입니다."""
    cap = buyer_price_cap(budget)
    limit = max(1, policy["max_rounds"] - 1)
    ratio = policy["opening_ratio"] + (1 - policy["opening_ratio"]) * min(rounds, limit) / limit
    planned = min(cap, int(cap * ratio))
    if ask is not None and 0 < ask <= planned:
        return ask
    return planned


def valid_receipt(receipt, budget, highest_offer, item, acceptable):
    """상점이 보낸 영수증이 우리가 제안한 범위 안인지 확인합니다."""
    if not isinstance(receipt, dict) or not receipt.get("sig_seller"):
        return False
    price = receipt.get("price")
    if isinstance(price, float) and price.is_integer():
        price = int(price)
    if type(price) is not int or price <= 0 or price + buyer_fee(price) > budget or price > highest_offer:
        return False
    return receipt.get("item") == item or receipt.get("item") in (acceptable or [])


def accept_terms(order, policy):
    """결제 전에 키트가 확인할 조건."""
    if not order.get("acceptable"):
        return None
    terms = {"cert": True, "grades": list(policy["accept_grades"])}
    if isinstance(order.get("use_within_hours"), (int, float)):
        terms["min_hours_left"] = order["use_within_hours"]
    return terms


def review_from_deal(deal):
    """실제 정산 결과만으로 별점과 리뷰를 만듭니다. 상대의 글은 넣지 않습니다."""
    settled = deal.get("settled") if isinstance(deal.get("settled"), dict) else {}
    stars = 5
    notes = []
    grade = settled.get("grade")
    if grade:
        notes.append(f"grade {grade}")
    if settled.get("claim_honest") is False:
        stars = 1
        notes.append(f"claimed {settled.get('claim')}, not matching")
    elif grade == "C":
        stars = 3
        notes.append("low grade")
    elif grade == "B":
        stars = 4
    if settled.get("cert") is False:
        stars = min(stars, 3)
    price = deal.get("receipt", {}).get("price") if isinstance(deal.get("receipt"), dict) else None
    if isinstance(price, (int, float)):
        notes.append(f"paid {int(price)}")
    if settled.get("ledger") is not None:
        notes.append(f"ledger {settled['ledger']}")
    return {"stars": stars, "review": "Settled deal: " + ", ".join(notes) + "." if notes else "Settled deal."}


# --- 입고

def plan_restock(inventory, catalog, policy, spent, rng):
    """입고 창에 살 물량을 정합니다. 예상 원가는 정가의 75%(시장 A등급 상한)로 잡습니다."""
    balance = inventory.get("balance") or 0
    budget = min(policy["restock_budget"] - spent, balance - policy["reserve_balance"])
    room = policy["max_stock_units"] - len(inventory.get("units") or [])
    if budget <= 0 or room <= 0:
        return []
    surge = {s.get("sku") for s in inventory.get("surge") or [] if (s.get("factor") or 1) > 1.05}
    held = {u.get("sku") for u in inventory.get("units") or []}
    pool = []
    for sku, c in (catalog or {}).items():
        if not isinstance(c, dict) or sku in surge or sku in held or not c.get("list_price"):
            continue
        if policy["kinds"] and c.get("kind") not in policy["kinds"]:
            continue
        if policy["categories"] and not any(x in (c.get("category") or "") for x in policy["categories"]):
            continue
        pool.append((sku, c))
    rng.shuffle(pool)
    buy = []
    for sku, c in pool:
        if len(buy) >= policy["max_skus_per_window"] or room <= 0:
            break
        unit_cost = c["list_price"] * 0.75
        qty = min(policy["max_units_per_sku"], room, 20, int(budget // unit_cost))
        if qty < 1:
            continue
        buy.append({"sku": sku, "qty": qty})
        budget -= unit_cost * qty
        room -= qty
    return buy
