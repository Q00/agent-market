"""Round 4 kit v5 end to end: two agent kits with no market tools forge their own and settle a deal.

    .venv/bin/python tests/test_r4_forge.py

A real market process, tests/fake_chain.py for Maroo, and a scripted model (a local OpenAI-compatible endpoint that
answers each turn with the next move of a fixed script). The shop agent forges an A2A handler that buys stock and signs a
checkout; the buyer agent forges a tool that gets an order, writes to the shop over A2A, pays on chain and settles. The
sandbox is checked too: a tool may not read a key file or reach a host it did not declare.
The exit code is the number of failed checks.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE / "tests"))
sys.path.insert(0, str(HERE / "publish/scripts"))
PORT, RPC_PORT, LLM_PORT = 8750, 8752, 8753
BASE = f"http://127.0.0.1:{PORT}"
os.environ["MAROO_RPC_URL"] = f"http://127.0.0.1:{RPC_PORT}"

import httpx  # noqa: E402
import maroo  # noqa: E402
from fake_chain import FakeChain  # noqa: E402

ADMIN = "forge-admin"
RESULTS = []


def check(name, ok, evidence=""):
    RESULTS.append((name, bool(ok), str(evidence)[:300]))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {str(evidence)[:300]}", flush=True)
    return ok


def move(action, args, thought="scripted"):
    return json.dumps({"thought": thought, "action": action, "args": args}, ensure_ascii=False)


NOTHING_TEST = "def test(run, kit):\n    assert kit.testing\n"

SHOP_HANDLER = r'''
import time
import httpx

def run(args, kit):
    data = args.get("data") or {}
    acc = data.get("acceptable") if isinstance(data, dict) else None
    if not acc:
        return {"reply": "어떤 상품을 찾으세요? data에 acceptable SKU 목록을 보내 주세요.", "state": "input_required"}
    h = {"authorization": "Bearer " + kit.token("shop")}
    inv = httpx.get(kit.market + "/api/inventory", headers=h, timeout=20).json()
    unit = next((u for u in inv["units"] if u["sku"] in acc and u["grade"] != "expired"), None)
    if unit is None:
        r = httpx.post(kit.market + "/api/supply", json={"sku": acc[0], "qty": 1}, headers=h, timeout=20)
        if r.status_code != 200:
            return {"reply": "재고를 못 구했어요: " + r.text[:200], "state": "rejected"}
        unit = r.json()["units"][0]
    price = max(1000, int((unit.get("list_price") or 1000) * 0.9))
    if isinstance(data.get("budget"), int):
        price = min(price, data["budget"])
    co = {"id": "cs-" + args["task_id"][:12], "shop": kit.name, "shop_url": kit.shop_url, "item": unit["sku"],
          "price": price, "task": args["task_id"], "currency": "credits", "iat": int(time.time()),
          "unit": unit["unit"], "cert": unit["cert"]}
    return {"reply": f"{price} 크레딧에 드릴게요.", "data": {"checkout_jwt": kit.sign_jwt(co, "shop"), "price": price},
            "state": "completed"}
'''
SHOP_HANDLER_TEST = '''
def test(run, kit):
    out = run({"text": "안녕하세요", "data": None, "task_id": "t-test", "context_id": "c", "history": []}, kit)
    assert out["state"] == "input_required", out
'''

CARD_TOOL = '''
import json
def run(args, kit):
    open("card.json", "w").write(json.dumps({"name": kit.name, "description": args["description"],
        "skills": [{"id": "buy", "name": "Buy", "description": "send data {acceptable, budget}", "tags": ["shop"]}]}))
    return "written"
'''
CARD_TEST = "def test(run, kit):\n    assert callable(run)\n"

BUYER_DEAL = r'''
import base64, hashlib, json, uuid
import httpx

def b64u(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()

def run(args, kit):
    m, h = kit.market, {"authorization": "Bearer " + kit.token("buyer")}
    o = httpx.post(m + "/api/orders", headers=h, timeout=20).json()
    if "order" not in o:
        return {"no_order": o}
    shop = next(s for s in httpx.get(m + "/api/shops", headers=h, timeout=20).json()["shops"] if s["name"] == args["shop"])
    card = httpx.get(shop["url"] + "/.well-known/agent-card.json", timeout=20).json()
    rpc = card["supportedInterfaces"][0]["url"]
    msg = {"jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": {"message": {
        "messageId": uuid.uuid4().hex, "role": "ROLE_USER",
        "parts": [{"text": "이 주문에 맞는 걸 주세요"}, {"data": {"acceptable": o["acceptable"], "budget": o["budget"]}}]}}}
    r = httpx.post(rpc, json=msg, headers={"A2A-Version": "1.0"}, timeout=150).json()
    task = r["result"]["task"]
    jwt = task["artifacts"][0]["parts"][0]["data"]["checkout_jwt"]
    price = json.loads(base64.urlsafe_b64decode(jwt.split(".")[1] + "=="))["price"]
    digest = hashlib.sha256(jwt.encode()).digest()
    ch = o["chain"]
    each, half = price * ch["market_fee_bps"] // 10000, ch["network_fee_credits"] // 2
    tx = kit.pay(shop["pubkey"], (price - each - half) * ch["credit_wei"], digest.hex())
    fee_tx = kit.pay(ch["market_address"], 2 * each * ch["credit_wei"], digest.hex()) if each else None
    opens = {k: hashlib.sha256(kit.canon(v["mandate"])).hexdigest() for k, v in o["mandates"].items()}
    cm = {"vct": "mandate.checkout.1", "order": o["order"], "open_mandate": opens["checkout"], "checkout_jwt": jwt,
          "checkout_hash": b64u(digest)}
    pm = {"vct": "mandate.payment.1", "order": o["order"], "open_mandate": opens["payment"], "payee": shop["url"],
          "amount": price, "currency": "credits", "checkout_hash": b64u(digest), "tx_hash": tx, "fee_tx_hash": fee_tx}
    body = {"order": o["order"], "checkout_jwt": jwt, "checkout_mandate": {"mandate": cm, "sig": kit.sign(cm, "buyer")},
            "payment_mandate": {"mandate": pm, "sig": kit.sign(pm, "buyer")}}
    s = httpx.post(m + "/api/settle", json=body, timeout=60)
    return {"status": s.status_code, "settled": s.json().get("settled"), "checks": s.json().get("checks")}
'''
BUYER_TEST = '''
def test(run, kit):
    assert kit.token("buyer") and kit.address("buyer").startswith("0x")
    assert kit.pay("0x" + "1" * 40, 1000, "00") == "0x" + "0" * 64     # tests send nothing
'''

PEEK_KEY = '''
def run(args, kit):
    return open("buyer_wallet.key").read()
'''
PEEK_NET = '''
import urllib.request
def run(args, kit):
    return urllib.request.urlopen("http://example.com", timeout=5).status
'''

SCRIPTS = {
    "shop-script": [
        move("forge_tool", {"name": "sell", "description": "answers buyers over A2A: stock and a signed checkout",
                            "permission": {"network": ["127.0.0.1"], "identity": ["shop"], "wallet": ["sign_jwt"],
                                           "handles": ["a2a"], "timeout": 60},
                            "logic": SHOP_HANDLER, "test": SHOP_HANDLER_TEST}),
        move("forge_tool", {"name": "write_card", "description": "writes card.json; args {description}",
                            "permission": {"write": ["card.json"]}, "logic": CARD_TOOL, "test": CARD_TEST}),
        move("write_card", {"description": "주문의 acceptable SKU와 예산을 data로 보내면 바로 결제 가능한 checkout을 드립니다."}),
        move("forge_tool", {"name": "peek_key", "description": "tries to read a key", "permission": {},
                            "logic": PEEK_KEY, "test": "def test(run, kit):\n    run({}, kit)\n"}),
        move("forge_tool", {"name": "peek_net", "description": "tries an undeclared host", "permission": {"network": ["127.0.0.1"]},
                            "logic": PEEK_NET, "test": "def test(run, kit):\n    run({}, kit)\n"}),
        move("sleep", {"seconds": 3600, "why": "the handler answers buyers"}),
    ],
    "buyer-script": [
        move("forge_tool", {"name": "deal", "description": "one whole deal with a named shop; args {shop}",
                            "permission": {"network": ["127.0.0.1"], "identity": ["buyer"], "wallet": ["sign", "pay"],
                                           "pay_limit": 100000, "timeout": 170},
                            "logic": BUYER_DEAL, "test": BUYER_TEST}),
        move("deal", {"shop": "forge-shop"}),
        move("write_note", {"text": "deal tool works with forge-shop"}),
        move("sleep", {"seconds": 3600, "why": "done"}),
    ],
}
SEEN = {}          # model -> the observations it got, in order


class LLM(BaseHTTPRequestHandler):
    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers["content-length"])))
        script = SCRIPTS[req["model"]]
        msgs = req["messages"]
        n = sum(1 for m in msgs if m["role"] == "assistant")
        if n:
            SEEN.setdefault(req["model"], []).append(msgs[-1]["content"])
        else:
            SEEN.setdefault(req["model"] + ":context", []).append(msgs[1]["content"])
        content = script[n] if n < len(script) else move("sleep", {"seconds": 3600, "why": "script over"})
        body = json.dumps({"choices": [{"message": {"role": "assistant", "content": content}}]}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def wait_for(fn, seconds, every=0.5):
    end = time.time() + seconds
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(every)
    return None


def kit_proc(d, name, model, role_flag, tmp):
    env = dict(os.environ, AGENT_LLM_BASE=f"http://127.0.0.1:{LLM_PORT}/v1", AGENT_LLM_MODEL=model, AGENT_LLM_KEY="x",
               MAROO_RPC_URL=f"http://127.0.0.1:{RPC_PORT}")
    out = open(tmp / f"{name}.out", "w")
    return subprocess.Popen([sys.executable, str(HERE / "publish/scripts/agent_kit.py"), "run", "--dir", str(d),
                             "--market", BASE, "--name", name, role_flag], stdout=out, stderr=subprocess.STDOUT, env=env)


def main():
    tmp = Path(tempfile.mkdtemp(prefix="r4forge-"))
    chain = FakeChain(RPC_PORT)
    llm = ThreadingHTTPServer(("127.0.0.1", LLM_PORT), LLM)
    threading.Thread(target=llm.serve_forever, daemon=True).start()
    env = dict(os.environ, MARKET_ADMIN_TOKEN=ADMIN, MARKET_ORDER_GAP="0", MARKET_SUPPLY_EVERY="0")
    for k in ("MARKET_PUBLIC", "GITHUB_CLIENT_ID", "GITHUB_CLIENT_SECRET", "MARKET_BASE_URL"):
        env.pop(k, None)
    mlog = open(tmp / "market.log", "w")
    market = subprocess.Popen([sys.executable, str(HERE / "market/server.py"), "--port", str(PORT), "--db", str(tmp / "m.db")],
                              stdout=mlog, stderr=subprocess.STDOUT, env=env)
    procs = [market]
    c = httpx.Client(timeout=30)
    try:
        wait_for(lambda: c.get(f"{BASE}/api/demand").status_code == 200 if _up(c) else None, 60)
        r = c.post(f"{BASE}/api/admin/round", json={"round": 4}, headers={"x-admin-token": ADMIN})
        check("the market is in Round 4", r.status_code == 200 and r.json().get("round") == 4, r.text[:100])

        root = c.get(f"{BASE}/.well-known/agent-card.json").json()
        ids = {e["id"] for e in root.get("entities", [])}
        check("the market serves a directory card of its entities",
              {"orders", "clearing", "wholesale", "forum", "board", "bank"} <= ids, sorted(ids))
        w = c.get(f"{BASE}/entities/wholesale/.well-known/agent-card.json").json()
        check("an entity card names each skill's HTTP call",
              all(s.get("http", {}).get("path", "").startswith("/api/") for s in w["skills"]), [s["id"] for s in w["skills"]])
        check("an unknown entity is 404", c.get(f"{BASE}/entities/nope/.well-known/agent-card.json").status_code == 404)

        sd, bd = tmp / "shop", tmp / "buyer"
        for d in (sd, bd):
            d.mkdir()
            (d / "agent.json").write_text(json.dumps({"persona": "test", "goal": "test", "run_minutes": 5}))
        buyer_key = maroo.Wallet.load(bd / "buyer_wallet.key")
        chain.fund(buyer_key.address, 10 ** 22)

        procs.append(kit_proc(sd, "forge-shop", "shop-script", "--shop-only", tmp))
        shop_ready = wait_for(lambda: (sd / "tools/sell/forge.json").exists() and (sd / "card.json").exists(), 90)
        check("the shop agent forged its A2A handler and wrote its card", shop_ready)
        log = lambda d: (d / "logs/agent-kit.log").read_text() if (d / "logs/agent-kit.log").exists() else ""
        wait_for(lambda: "peek_net" in log(sd) and "test failed" in log(sd).split("peek_net")[-1], 60)
        forged = [json.loads(x) for x in (sd / "logs/forge.jsonl").read_text().splitlines()]
        by = {f["tool"]: f for f in forged}
        check("a tool may not read a key file", by.get("peek_key", {}).get("passed") is False
              and "secrets" in (by["peek_key"].get("error") or ""), by.get("peek_key", {}).get("error"))
        check("a tool may not reach an undeclared host", by.get("peek_net", {}).get("passed") is False
              and "permission.network" in (by["peek_net"].get("error") or ""), by.get("peek_net", {}).get("error"))
        check("failed tools are not installed", not (sd / "tools/peek_key").exists() and not (sd / "tools/peek_net").exists())
        shops = c.get(f"{BASE}/api/shops").json()["shops"]
        me = next((s for s in shops if s["name"] == "forge-shop"), None)
        card = c.get(me["url"] + "/.well-known/agent-card.json").json() if me else {}
        check("the shop's card is the agent's card.json with the kit's interface",
              "acceptable" in card.get("description", "") and card.get("supportedInterfaces", [{}])[0].get("protocolBinding") == "JSONRPC",
              card.get("description"))

        before = chain.balances.get(buyer_key.address.lower(), chain.balances.get(buyer_key.address, 0))
        procs.append(kit_proc(bd, "forge-buyer", "buyer-script", "--buyer-only", tmp))
        deal = wait_for(lambda: next((o for o in SEEN.get("buyer-script", []) if '"settled"' in o), None), 150)
        check("the buyer agent's forged tool settled a deal with the shop's forged handler",
              deal and '"settled": true' in deal, (deal or "")[:300])
        led = c.get(f"{BASE}/api/ledger", params={"after": 0}).json()
        settles = [e for e in led if e["kind"] == "settle"]
        check("the deal is on the ledger with its Maroo transactions",
              settles and settles[-1]["data"].get("tx_hash", "").startswith("0x"), settles[-1]["data"] if settles else led[-3:])
        ags = {x["name"]: x for x in c.get(f"{BASE}/api/agents").json()["agents"]}
        check("every agent is listed with its roles and card, the buyer-only one too",
              ags.get("forge-buyer", {}).get("card") and ags["forge-buyer"]["roles"] == ["buyer"]
              and ags.get("forge-shop", {}).get("roles") == ["shop"], ags)
        bcard = c.get(ags["forge-buyer"]["card"]).json() if ags.get("forge-buyer", {}).get("card") else {}
        check("a buyer-only agent serves a card with its role and wallet",
              bcard.get("roles") == ["buyer"] and bcard.get("wallets", {}).get("buyer") == buyer_key.address, bcard)
        ctx = (SEEN.get("buyer-script:context") or [""])[0]
        check("the agent starts with the directory card and no tools", "Order desk" in ctx and "(none yet" in ctx, ctx[:120])
        check("the buyer's notes persist", "deal tool works" in (bd / "notes.md").read_text(encoding="utf-8"))
        r = subprocess.run([sys.executable, str(HERE / "publish/scripts/agent_kit.py"), "tools", "--dir", str(bd),
                            "--market", BASE, "--retest"], capture_output=True, text=True, timeout=120,
                           env=dict(os.environ, MAROO_RPC_URL=f"http://127.0.0.1:{RPC_PORT}"))
        check("forged tools load again for the next run", "deal (v1" in r.stdout, r.stdout[-200:] + r.stderr[-200:])
        (bd / "tools/deal/logic.py").write_text("def run(args, kit):\n    return 'hand-written'\n")
        r = subprocess.run([sys.executable, str(HERE / "publish/scripts/agent_kit.py"), "tools", "--dir", str(bd),
                            "--market", BASE], capture_output=True, text=True, timeout=60)
        check("a tool edited by hand (not forged) does not load", "deal (v1" not in r.stdout, r.stdout[-200:])
    finally:
        for p in procs[::-1]:
            p.terminate()
            try:
                p.wait(10)
            except subprocess.TimeoutExpired:
                p.kill()
        llm.shutdown()
        chain.stop()
        mlog.close()
        failed = [r for r in RESULTS if not r[1]]
        print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed; logs in {tmp}")
        if not failed:
            shutil.rmtree(tmp, ignore_errors=True)
    sys.exit(len([r for r in RESULTS if not r[1]]))


def _up(c):
    try:
        c.get(f"{BASE}/api/demand")
        return True
    except httpx.HTTPError:
        return False


if __name__ == "__main__":
    main()
