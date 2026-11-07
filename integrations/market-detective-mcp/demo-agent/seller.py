"""규칙 기반 판매자. 시장 키트 starter_round3 판매자의 A2A·서명 코드를 재사용하고, 판단만 rules.py로 바꿨습니다.

    python seller.py --port 9301

원가 미만으로 팔지 않고, 제품은 실제 등급을 표기하며, 구매자 글에서는 숫자만 읽습니다.
"""
import argparse
import base64
import json
import os
from pathlib import Path

import uvicorn
from eth_account import Account
from eth_account.messages import encode_defunct
from starlette.applications import Starlette

from a2a.helpers import get_message_text, new_data_part, new_task_from_user_message, new_text_part
from a2a.server.agent_execution import AgentExecutor
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill

import rules

NAME = os.environ.get("DEMO_SHOP_NAME", "Demo Rule Shop")
RESERVE = {"lamp": 100, "chair": 180, "bookshelf": 260}     # 라운드 0~2의 재고 없는 상품
MARKET_DIR = Path(os.environ.get("MARKET_DIR", ".market"))


def canon(d):
    return json.dumps(d, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def sign(receipt):
    # Maroo 테스트넷 지갑(EVM)으로 EIP-191 서명. 키 파일은 키트가 만든 shop_wallet.key.
    key = Path("shop_wallet.key").read_text().strip()
    body = {k: receipt[k] for k in ("shop", "item", "price", "task")}
    sig = Account.from_key(key).sign_message(encode_defunct(primitive=canon(body))).signature
    return dict(receipt, sig_seller=base64.b64encode(bytes(sig)).decode())


def find_unit(text, taken=()):
    """구매자가 요청한 품목에 맞는 재고 중 유통기한이 가장 먼저 끝나는 단위."""
    want = text.lower()
    try:
        inv = json.loads((MARKET_DIR / "inventory.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        inv = {}
    if inv.get("round", 0) < 3:
        item = next((k for k in RESERVE if k in want), None)
        return item and {"sku": item, "name": item, "list_price": int(RESERVE[item] * 1.3), "cost": RESERVE[item],
                         "unit": None, "expires_at": None, "supplied_at": 0}
    units = inv.get("units", [])
    fit = [u for u in units if u["grade"] != "expired" and u["unit"] not in taken and
           ((u.get("category") or "").split(">")[-1].lower() in want or (u.get("name") or "").lower() in want
            or u["sku"].lower() in want)]
    named = [u for u in fit if u["sku"].lower() in want]
    fit = named or fit
    return min(fit, key=lambda u: (u["expires_at"] or float("inf"), u["supplied_at"]), default=None)


class Seller(AgentExecutor):
    def __init__(self):
        self.open = {}  # task id -> {"unit", "rounds", "offer"}

    async def execute(self, context, event_queue):
        task = context.current_task
        if task is None:
            task = new_task_from_user_message(context.message)
            await event_queue.enqueue_event(task)
        up = TaskUpdater(event_queue=event_queue, task_id=task.id, context_id=task.context_id)
        say = lambda text: up.new_agent_message(parts=[new_text_part(text)])
        text = get_message_text(context.message) or ""
        deal = self.open.get(task.id)
        policy = rules.load_policy("shop")

        if deal is None:
            unit = find_unit(text, {d["unit"]["unit"] for d in self.open.values() if d["unit"]["unit"]})
            if unit is None:
                await up.reject(say("Sorry, we have none of that in stock."))
                return
            deal = self.open[task.id] = {"unit": unit, "rounds": 0, "offer": None}
        else:
            deal["rounds"] += 1
            offer = rules.parse_offer(text)
            deal["offer"] = offer if offer is not None else deal["offer"]

        u = deal["unit"]
        move = rules.seller_move(u["cost"], u["list_price"], deal["offer"], deal["rounds"], policy)
        print(f"[rule] round {deal['rounds']} {move['action']}", flush=True)
        if move["action"] == "accept":
            price = deal["offer"]
            receipt = {"shop": NAME, "item": u["sku"], "price": price, "task": task.id}
            if u["unit"]:
                receipt["unit"] = u["unit"]
            receipt.update(rules.honest_terms(u))
            receipt = sign(receipt)
            await up.add_artifact(parts=[new_data_part(receipt)], name="receipt")
            await up.complete(say(f"Deal: {u['name']} at {price}."))
            del self.open[task.id]
            return
        if move["action"] == "reject":
            await up.reject(say("No deal."))
            del self.open[task.id]
            return
        reply = (f"{u['name']}: asking {move['price']}. Send your offer as a number." if deal["rounds"] == 0
                 else f"Counteroffer: {move['price']}.")
        await up.requires_input(say(reply))

    async def cancel(self, context, event_queue):
        task = context.current_task
        self.open.pop(task.id, None)
        await TaskUpdater(event_queue=event_queue, task_id=task.id, context_id=task.context_id).cancel()


def build(port):
    url = f"http://127.0.0.1:{port}/"
    card = AgentCard(
        name=NAME,
        description="A small online shop run by fixed rules. Ask for an item by name or SKU, then send a price.",
        version="1.0.0",
        default_input_modes=["text/plain"],
        default_output_modes=["application/json", "text/plain"],
        capabilities=AgentCapabilities(streaming=False),
        supported_interfaces=[AgentInterface(protocol_binding="JSONRPC", url=url, protocol_version="1.0")],
        skills=[AgentSkill(id="negotiate", name="Negotiate a purchase",
                           description="Ask for an item, then send a price. The shop answers with a counteroffer or a receipt.",
                           tags=["shopping", "negotiation"], examples=["I want to buy 종가 포기김치 10kg (G00001)", "52000"])],
    )
    handler = DefaultRequestHandler(agent_executor=Seller(), task_store=InMemoryTaskStore(), agent_card=card)
    return Starlette(routes=create_agent_card_routes(card) + create_jsonrpc_routes(handler, "/"))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9301)
    port = ap.parse_args().port
    uvicorn.run(build(port), host="127.0.0.1", port=port, log_level="warning")
