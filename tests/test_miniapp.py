"""The mini app (agent_kit.py run --app): chat, a tool the page offers, and a MeshKit capability behind consent.

    .venv/bin/python tests/test_miniapp.py      (needs meshkit: pip install -e <meshkit>/meshkit-py, and its examples)

Starts a market, the meshkit-py example mini app (examples/miniapp_server.py, a DailyMart capability) and a buyer-only
agent kit with --app on a scripted model. This test plays the page: it offers a tool, answers the agent's call to it,
approves the MeshKit call, and chats. The exit code is the number of failed checks.
"""
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent.parent
MESHKIT = Path(os.environ.get("MESHKIT_PY", HERE.parent / "meshkit/meshkit-py"))
PORT, LLM_PORT, APP_PORT, MESH_PORT = 8760, 8763, 8764, 8765
BASE, APP = f"http://127.0.0.1:{PORT}", f"http://127.0.0.1:{APP_PORT}"
RESULTS = []


def check(name, ok, evidence=""):
    RESULTS.append((name, bool(ok)))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {str(evidence)[:300]}", flush=True)


def move(action, args):
    return json.dumps({"thought": "scripted", "action": action, "args": args}, ensure_ascii=False)


SEEN = []


class LLM(BaseHTTPRequestHandler):
    """Turn 1 notes what it sees, turn 2 shows a card through the page, turn 3 buys through MeshKit, then it sleeps;
    a chat message gets a reply."""

    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers["content-length"])))
        msgs = req["messages"]
        n = sum(1 for m in msgs if m["role"] == "assistant")
        last = msgs[-1]["content"]
        SEEN.append(last)
        waiting = re.findall(r'"id": "(m[0-9a-f]{6})", "from": "a person', last)
        if waiting:
            content = move("reply", {"to": waiting[-1], "text": "안녕하세요, 우유를 주문해 두었어요."})
        elif n == 0:
            content = move("write_note", {"text": "first look: " + str("mesh_grocery_purchase_essentials" in last)})
        elif n == 1:
            content = move("app_show_card", {"title": "우유", "body": "100원 예산으로 주문합니다"})
        elif n == 2:
            content = move("mesh_grocery_purchase_essentials", {"items": "milk", "budget_krw": "100"})
        elif n == 3:
            content = move("forge_tool", {"name": "peek_app", "description": "reads the app page", "permission": {"network": ["127.0.0.1"]},
                                          "logic": f"import urllib.request\ndef run(args, kit):\n    return urllib.request.urlopen('{APP}/', timeout=5).read()[:50].decode()\n",
                                          "test": "def test(run, kit):\n    run({}, kit)\n"})
        else:
            content = move("sleep", {"seconds": 3600, "why": "done"})
        body = json.dumps({"choices": [{"message": {"role": "assistant", "content": content}}]}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def wait_for(fn, seconds, every=0.3):
    end = time.time() + seconds
    while time.time() < end:
        try:
            v = fn()
        except Exception:
            v = None
        if v:
            return v
        time.sleep(every)
    return None


def main():
    sys.path.insert(0, str(MESHKIT))
    from meshkit import ed25519
    tmp = Path(tempfile.mkdtemp(prefix="miniapp-"))
    llm = ThreadingHTTPServer(("127.0.0.1", LLM_PORT), LLM)
    threading.Thread(target=llm.serve_forever, daemon=True).start()
    env = dict(os.environ)
    for k in ("MARKET_PUBLIC", "GITHUB_CLIENT_ID", "GITHUB_CLIENT_SECRET", "MARKET_BASE_URL"):
        env.pop(k, None)
    procs = [subprocess.Popen([sys.executable, str(HERE / "market/server.py"), "--port", str(PORT), "--db", str(tmp / "m.db")],
                              stdout=open(tmp / "market.log", "w"), stderr=subprocess.STDOUT, env=env),
             subprocess.Popen([sys.executable, str(MESHKIT / "examples/miniapp_server.py")], cwd=MESHKIT,
                              env=dict(env, PYTHONPATH=str(MESHKIT)), stdout=open(tmp / "mesh.log", "w"), stderr=subprocess.STDOUT)]
    c = httpx.Client(timeout=30)
    try:
        wait_for(lambda: c.get(f"{BASE}/api/demand").status_code == 200, 60)
        d = tmp / "agent"
        d.mkdir()
        (d / "agent.json").write_text(json.dumps({"persona": "test", "goal": "help the people in your app"}))
        receipt_pub = base64.b64encode(ed25519.public_key_raw(ed25519.private_key_from_seed(bytes(range(0xA0, 0xC0))))).decode()
        (d / "meshkit.json").write_text(json.dumps({
            "caller": {"app_id": "app.hermes-chat", "bundle_id": "ai.meshkit.sample.hermeschat",
                       "key_id": "hermes-request-ed25519", "seed_hex": bytes(range(1, 33)).hex()},
            "targets": [{"url": f"http://127.0.0.1:{MESH_PORT}", "app_id": "app.dailymart",
                         "bundle_id": "ai.meshkit.sample.dailymart", "receipt_key_id": "target-receipt-ed25519",
                         "receipt_pubkey": receipt_pub, "consent": True}]}))
        wait_for(lambda: httpx.post(f"http://127.0.0.1:{MESH_PORT}", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}), 30)
        kit_env = dict(env, AGENT_LLM_BASE=f"http://127.0.0.1:{LLM_PORT}/v1", AGENT_LLM_MODEL="app", AGENT_LLM_KEY="x")
        # the page offers its tool before the agent's first run: the kit starts the mind after the app is up
        procs.append(subprocess.Popen([sys.executable, str(HERE / "publish/scripts/agent_kit.py"), "run", "--dir", str(d),
                                       "--market", BASE, "--name", "app-agent", "--buyer-only", "--app", str(APP_PORT)],
                                      stdout=open(tmp / "kit.out", "w"), stderr=subprocess.STDOUT, env=kit_env))
        wait_for(lambda: c.get(f"{APP}/api/state").status_code == 200, 30, 0.1)
        tok = __import__("re").search(r"'x-page-token': '([^']+)'", c.get(f"{APP}/miniapp.js").text).group(1)
        check("only the page may answer a consent (no page token: 403)", c.post(f"{APP}/api/answer", json={"id": "x", "result": {"approved": True}}).status_code == 403)
        c.headers["x-page-token"] = tok
        c.post(f"{APP}/api/offer", json={"tools": [{"name": "show_card", "description": "show a card; args {title, body}"}]})
        st = c.get(f"{APP}/api/state").json()
        check("the MeshKit capability is a tool for the agent", "mesh_grocery_purchase_essentials" in st["app_tools"],
              list(st["app_tools"]))
        check("the page's tool is a tool for the agent", "app_show_card" in st["app_tools"])
        call = wait_for(lambda: next((x for x in c.get(f"{APP}/api/state").json()["calls"] if x["kind"] == "tool"), None), 60)
        check("the agent calls the page's tool", call and call["args"].get("title") == "우유", call)
        if call:
            c.post(f"{APP}/api/answer", json={"id": call["id"], "result": "shown"})
        consent = wait_for(lambda: next((x for x in c.get(f"{APP}/api/state").json()["calls"] if x["kind"] == "consent"), None), 60)
        check("a MeshKit call waits for a person's consent", consent and consent["name"] == "grocery.purchase_essentials", consent)
        if consent:
            c.post(f"{APP}/api/answer", json={"id": consent["id"], "result": {"approved": True}})
        obs = wait_for(lambda: next((s for s in SEEN if '"receipt"' in s), None), 60)
        check("the approved call returns a verified, target-signed receipt", obs and '"status": "completed"' in obs, (obs or "")[:300])
        forged = wait_for(lambda: [json.loads(x) for x in (d / "logs/forge.jsonl").read_text().splitlines()], 60)
        peek = next((f for f in forged or [] if f["tool"] == "peek_app"), {})
        check("a forged tool cannot reach the app's page", peek.get("passed") is False and "app's page" in (peek.get("error") or ""),
              peek.get("error"))
        r = c.post(f"{APP}/api/chat", json={"text": "안녕하세요", "user": "tester"}, timeout=150).json()
        check("a person's chat message gets the agent's reply", "우유" in (r.get("reply") or ""), r)
    finally:
        for p in procs[::-1]:
            p.terminate()
            try:
                p.wait(10)
            except subprocess.TimeoutExpired:
                p.kill()
        llm.shutdown()
        failed = [r for r in RESULTS if not r[1]]
        print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed; logs in {tmp}")
        if not failed:
            shutil.rmtree(tmp, ignore_errors=True)
    sys.exit(len([r for r in RESULTS if not r[1]]))


if __name__ == "__main__":
    main()
