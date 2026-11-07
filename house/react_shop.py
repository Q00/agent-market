"""A ReAct shop. Same contract as any seller behind the kit: A2A in free text, stock from $MARKET_DIR, signed receipts.

    python react_shop.py --persona "..." --name NAME --port 9101
    python react_shop.py --persona "..." --restock --state DIR

The model handles each buyer message and each restock window with its own reasoning. The code checks only what the
market would refuse anyway: a receipt needs a real unit in stock.
"""
import argparse
import asyncio
import base64
import json
import os
import time
from pathlib import Path

import uvicorn
from a2a.helpers import get_message_text, new_data_part, new_task_from_user_message, new_text_part
from a2a.server.agent_execution import AgentExecutor
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill
from eth_account import Account
from eth_account.messages import encode_defunct
from starlette.applications import Starlette

import react

GOAL = """You run a shop and a buyer has just written to you. Your goal is to end up with the highest book value and revenue in the
market, so sell: every unit that sits on your shelf costs you, and every buyer who leaves without a deal is lost money. Buyers
have budgets you cannot see. A unit's cost is what you paid. You pay fees on each sale: 1% of the price to the market and 210
credits of network fee, so a price below about 215 pays nothing at all. You choose the price, what you say about the goods, what
you promise, and whether to prove the grade with the market's certificate. Answer every message with either a reply or a deal.
Your personality: {persona}"""

RESTOCK = """You run a shop and the market's restock window may be open. Your goal is the highest book value and revenue in the market.
Decide what to stock, if anything: you pay in credits now, food spoils, goods in stock cost storage, and supply costs rise when
everyone buys the same SKU. This is also your time to think about your business: read the forum, reply, advertise, or start
anything that could bring you money. Then finish with what you want to buy. Your personality: {persona}"""


def canon(d):
    return json.dumps(d, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def sign_receipt(receipt):
    key = Account.from_key(Path("shop_wallet.key").read_text().strip())
    sig = key.sign_message(encode_defunct(primitive=canon({k: receipt[k] for k in ("shop", "item", "price", "task")})))
    return dict(receipt, sig_seller=base64.b64encode(bytes(sig.signature)).decode())


def inventory():
    return react.read_json(react.market_dir() / "inventory.json", {})


def stock_view(_args=None):
    inv = inventory()
    now = time.time()
    return {"balance": inv.get("balance"), "restock": inv.get("restock"),
            "units": [{"unit": u["unit"], "sku": u["sku"], "name": u.get("name"), "category": u.get("category"), "kind": u.get("kind"),
                       "grade": u.get("grade"), "cost": u.get("cost"), "list_price": u.get("list_price"),
                       "hours_left": round((u["expires_at"] - now) / 3600, 1) if u.get("expires_at") else None}
                      for u in inv.get("units", [])[:60] if u.get("grade") != "expired"]}


class Shop(AgentExecutor):
    def __init__(self, name, persona):
        self.name, self.persona = name, persona
        self.chats, self.promised = {}, {}

    def think(self, task_id, text):
        """One buyer message through the ReAct loop. Returns ("reply"|"deal"|"end", payload)."""
        lines = self.chats.setdefault(task_id, [])
        lines.append(f"buyer: {text}")
        out = {}

        def reply(args):
            out.update(kind="reply", text=str(args["text"])[:2000])
            return out

        def end(args):
            out.update(kind="end", text=str(args.get("text") or "")[:2000])
            return out

        def deal(args):
            unit = next((u for u in inventory().get("units", []) if u["unit"] == args.get("unit") and u.get("grade") != "expired"), None)
            if unit is None:
                return "that unit is not in your stock. Look at check_stock"
            if unit["unit"] in {u for t, u in self.promised.items() if t != task_id}:
                return "that unit is promised in another open deal; pick another"
            price = args.get("price")
            if type(price) is not int or price < 0:
                return "price must be a whole number of credits"
            terms = {"unit": unit["unit"]}
            if args.get("claim") in ("A", "B", "C"):
                terms["claim"] = args["claim"]
            if type(args.get("warranty_hours")) in (int, float):
                terms["warranty_hours"] = args["warranty_hours"]
            if args.get("show_certificate") is False:
                terms["cert"] = False
            out.update(kind="deal", sku=unit["sku"], price=price, terms=terms, text=str(args.get("text") or "")[:2000])
            return out

        tools = dict(react.common_tools("shop"))
        tools.update({
            "check_stock": ("your balance and the units you hold, with their hidden grade and cost; args {}", stock_view),
            "reply": ("say something to this buyer and wait for the answer; args {text}", reply),
            "deal": ("agree and issue the signed receipt, which ends the talk; args {unit, price, text, claim (optional grade A/B/C you claim), "
                     "warranty_hours (optional, up to 72), show_certificate (optional, false hides the market's certificate)}", deal),
            "end_conversation": ("refuse and end the talk; args {text}", end),
        })
        context = "The conversation so far:\n" + "\n".join(lines[-20:])
        react.run(GOAL.format(persona=self.persona), tools, time.time() + 90, {"reply", "deal", "end_conversation"}, context, label="sale")
        if out.get("text"):
            lines.append(f"you: {out['text']}")
        return out

    async def execute(self, context, event_queue):
        task = context.current_task
        if task is None:
            task = new_task_from_user_message(context.message)
            await event_queue.enqueue_event(task)
        up = TaskUpdater(event_queue=event_queue, task_id=task.id, context_id=task.context_id)
        say = lambda text: up.new_agent_message(parts=[new_text_part(text)])
        out = await asyncio.to_thread(self.think, task.id, get_message_text(context.message) or "")
        kind = out.get("kind")
        if kind == "deal":
            receipt = sign_receipt(dict({"shop": self.name, "item": out["sku"], "price": out["price"], "task": task.id}, **out["terms"]))
            self.promised[task.id] = out["terms"]["unit"]
            await up.add_artifact(parts=[new_data_part(receipt)], name="receipt")
            await up.complete(say(out["text"] or f"Deal at {out['price']}."))
        elif kind == "reply":
            await up.requires_input(say(out["text"]))
        else:
            self.chats.pop(task.id, None)
            await up.reject(say(out.get("text") or "지금은 응답할 수 없습니다. 다시 말씀해 주세요."))

    async def cancel(self, context, event_queue):
        task = context.current_task
        self.chats.pop(task.id, None)
        await TaskUpdater(event_queue=event_queue, task_id=task.id, context_id=task.context_id).cancel()


def build(name, persona, port):
    url = f"http://127.0.0.1:{port}/"
    card = AgentCard(
        name=name, description="An AI shop. Write to it in your own words, ask what it has, and bargain.", version="1.0.0",
        default_input_modes=["text/plain"], default_output_modes=["application/json", "text/plain"],
        capabilities=AgentCapabilities(streaming=False),
        supported_interfaces=[AgentInterface(protocol_binding="JSONRPC", url=url, protocol_version="1.0")],
        skills=[AgentSkill(id="trade", name="Trade", description="Talk, bargain, and buy.", tags=["shopping"], examples=["이불 있어요?"])])
    handler = DefaultRequestHandler(agent_executor=Shop(name, persona), task_store=InMemoryTaskStore(), agent_card=card)
    return Starlette(routes=create_agent_card_routes(card) + create_jsonrpc_routes(handler, "/"))


def restock(a):
    os.environ["MARKET_DIR"] = a.state
    result = {}

    def demand(_args):
        return react.read_json(Path(a.state) / "demand.json", [])

    def catalog(args):
        want = str(args.get("category", ""))
        rows = react.read_json(Path(a.state) / "catalog.json", [])
        rows = rows.values() if isinstance(rows, dict) else rows
        return [{k: x.get(k) for k in ("sku", "name", "list_price", "kind", "shelf_hours")} for x in rows if want in (x.get("category") or "")][:40]

    def finish(args):
        buy = args.get("buy") if isinstance(args.get("buy"), list) else []
        result["buy"] = [{"sku": b.get("sku"), "qty": int(b.get("qty", 0))} for b in buy if isinstance(b, dict)][:10]
        if args.get("bankrupt") is True:
            result["bankrupt"] = True
        return result

    tools = dict(react.common_tools("shop"))
    tools.update({
        "check_stock": ("your balance, restock window, and units; args {}", stock_view),
        "demand": ("the categories buyers order and their median prices; args {}", demand),
        "catalog": ("SKUs you can buy in a category; args {category}", catalog),
        "finish": ("end this run. args {buy: [{sku, qty up to 20}]} (at most 10 lines), and bankrupt: true to file for bankruptcy", finish),
    })
    react.run(RESTOCK.format(persona=a.persona), tools, time.time() + 120, {"finish"}, label="restock")
    print(json.dumps(result or {"buy": []}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--persona", required=True)
    ap.add_argument("--name", default="AI Shop")
    ap.add_argument("--port", type=int, default=9301)
    ap.add_argument("--restock", action="store_true")
    ap.add_argument("--state")
    args = ap.parse_args()
    if args.restock:
        restock(args)
    else:
        uvicorn.run(build(args.name, args.persona, args.port), host="127.0.0.1", port=args.port, log_level="warning")
