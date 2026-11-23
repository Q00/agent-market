"""The model desk: the market's own model line for the agents of its class list.

An agent calls it with the token it got from joining, so there is no model key to hand out and nothing that works
outside the market. It runs beside the market as its own process and reads the market's database read only:

    python market/llm_gateway.py --db market.db --port 8820

    POST /llm/v1/chat/completions   authorization: Bearer <the agent's market token>
    GET  /llm/health

A call is served only when all of these hold:
  - the token belongs to an agent on the market, owned by a GitHub login in the class list (GATEWAY_STUDENTS);
  - that agent is alive: the market's probe reached it lately, or it answers its card URL now;
  - the first message is the kit's own system text (WORLD in publish/scripts/agent_kit.py), so the line serves the
    kit's agent loop, not a general chat;
  - the agent has fewer than GATEWAY_CONCURRENCY calls in flight.
Only `messages` and a capped `max_tokens` go upstream; the model is always GATEWAY_MODEL. The log keeps counts
and, when the upstream refuses a call, the first words of its error; never what an agent said.

Environment:
  GATEWAY_UPSTREAM_BASE, GATEWAY_UPSTREAM_KEY   an OpenAI-compatible endpoint (never shown to agents)
  GATEWAY_MODEL          the one model served (default gpt-6-luna(low))
  GATEWAY_STUDENTS       file of GitHub logins, one per line (default market/students.txt)
  GATEWAY_KIT            the kit file WORLD is read from (default publish/scripts/agent_kit.py)
  GATEWAY_ALIVE_SECONDS  how fresh the market's probe must be before the gateway probes by itself (default 600)
  GATEWAY_MAX_TOKENS     cap on max_tokens (default 4000)
  GATEWAY_MAX_CHARS      cap on the total size of messages (default 120000; the kit trims at 90000)
  GATEWAY_CONCURRENCY    calls in flight per agent (default 2)
  GATEWAY_STATE          directory for usage.jsonl and the OFF switch (default ./llm-gateway)
A file named OFF in GATEWAY_STATE turns the line off (503) without a restart.
"""
import argparse
import ast
import asyncio
import contextlib
import hashlib
import ipaddress
import json
import os
import socket
import sqlite3
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

HERE = Path(__file__).resolve().parent
UPSTREAM_BASE = os.environ.get("GATEWAY_UPSTREAM_BASE", "").rstrip("/")
UPSTREAM_KEY = os.environ.get("GATEWAY_UPSTREAM_KEY", "")
MODEL = os.environ.get("GATEWAY_MODEL", "gpt-6-luna(low)")
STUDENTS_FILE = Path(os.environ.get("GATEWAY_STUDENTS", HERE / "students.txt"))
KIT_FILE = Path(os.environ.get("GATEWAY_KIT", HERE.parent / "publish" / "scripts" / "agent_kit.py"))
ALIVE_SECONDS = float(os.environ.get("GATEWAY_ALIVE_SECONDS", "600"))
MAX_TOKENS = int(os.environ.get("GATEWAY_MAX_TOKENS", "4000"))
MAX_CHARS = int(os.environ.get("GATEWAY_MAX_CHARS", "120000"))
CONCURRENCY = int(os.environ.get("GATEWAY_CONCURRENCY", "2"))
STATE = Path(os.environ.get("GATEWAY_STATE", "llm-gateway"))
PUBLIC = os.environ.get("MARKET_PUBLIC", "") == "1"   # as in the market: card URLs must be https on public addresses
PARTY_TTL, PROBE_TTL = 30, 300

DB = None                                              # path, set in main()
PARTIES, PROBED, IN_FLIGHT = {}, {}, {}                # token -> (at, row) | name -> (at, ok) | name -> calls in flight
CLIENT = None


def kit_world(path):
    """WORLD from the kit's source, without importing the kit (it pulls in the A2A server)."""
    for node in ast.parse(Path(path).read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == "WORLD" for t in node.targets):
            return ast.literal_eval(node.value)
    raise SystemExit(f"no WORLD in {path}")


def students():
    return {x.strip().lower() for x in STUDENTS_FILE.read_text().split()} if STUDENTS_FILE.exists() else set()


def err(code, message, **more):
    return JSONResponse({"error": dict({"message": message, "type": "model_desk"}, **more)}, status_code=code)


def party(token):
    """The agent behind a market token: its name, owner, label, the freshest probe of any of its roles, a card URL."""
    hit = PARTIES.get(token)
    if hit and time.time() - hit[0] < PARTY_TTL:
        return hit[1]
    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=5)
    try:
        r = db.execute("select name, owner, label from parties where token=?", (token,)).fetchone()
        row = None
        if r:
            alive, url = db.execute("select max(alive), max(url) from parties where name=?", (r[0],)).fetchone()
            row = {"name": r[0], "owner": r[1], "label": r[2], "alive": alive or 0, "url": url}
    finally:
        db.close()
    if len(PARTIES) > 5000:
        PARTIES.clear()
    PARTIES[token] = (time.time(), row)
    return row


def public_address(url):
    u = urlparse(url or "")
    if not u.hostname or u.scheme not in ("https", "http"):
        return False
    if not PUBLIC:
        return True
    if u.scheme != "https":
        return False
    try:
        infos = socket.getaddrinfo(u.hostname, u.port or 443, 0, socket.SOCK_STREAM)
    except OSError:
        return False
    return all(ipaddress.ip_address(i[4][0]).is_global for i in infos)


async def alive(p):
    """The market's probe reached the agent lately, or its card URL answers now (asked at most every PROBE_TTL)."""
    if time.time() - p["alive"] < ALIVE_SECONDS:
        return True
    hit = PROBED.get(p["name"])
    if hit and time.time() - hit[0] < PROBE_TTL:
        return hit[1]
    ok = False
    if await asyncio.to_thread(public_address, p["url"]):
        try:
            r = await CLIENT.get(p["url"].rstrip("/") + "/.well-known/agent-card.json", timeout=6, follow_redirects=False)
            ok = r.status_code == 200 and len(r.content) < 200_000
        except httpx.HTTPError:
            ok = False
    PROBED[p["name"]] = (time.time(), ok)
    return ok


def usage_log(entry):
    with open(STATE / "usage.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(dict(entry, ts=int(time.time())), ensure_ascii=False) + "\n")


async def complete(req: Request):
    if (STATE / "OFF").exists():
        return err(503, "the model desk is closed for now; use your own model key (AGENT_LLM_BASE, AGENT_LLM_MODEL, AGENT_LLM_KEY)")
    token = req.headers.get("authorization", "").removeprefix("Bearer ").strip()
    p = await asyncio.to_thread(party, token) if token else None
    if p is None:
        return err(401, "unknown token: the model desk takes the token your agent got from joining this market "
                        "(kit v5.1 sends it by itself)")
    if not p["owner"] or p["owner"].lower() not in STUDENTS or p["label"] == "persona":
        return err(403, "the model desk serves the agents of the class list only; set your own model "
                        "(AGENT_LLM_BASE, AGENT_LLM_MODEL, AGENT_LLM_KEY)", reason="not_on_class_list")
    try:
        body = await req.json()
        messages = body["messages"]
        assert isinstance(messages, list) and messages and all(
            isinstance(m, dict) and m.get("role") in ("system", "user", "assistant") and isinstance(m.get("content"), str)
            for m in messages)
    except Exception:
        return err(400, "send {messages: [{role, content}]} with text content")
    if messages[0]["role"] != "system" or not messages[0]["content"].startswith(WORLD):
        return err(403, "the model desk answers the kit's agent loop only (agent_kit.py run)", reason="not_the_kit")
    if sum(len(m["content"]) for m in messages) > MAX_CHARS:
        return err(413, f"messages are over {MAX_CHARS} characters")
    if not await alive(p):
        return err(403, f"agent {p['name']} is not reachable from the market: the desk serves an agent while it is on "
                        "the market (is the tunnel up?)", reason="agent_not_alive")
    if IN_FLIGHT.get(p["name"], 0) >= CONCURRENCY:
        return err(429, f"{p['name']} already has {CONCURRENCY} calls in flight")
    IN_FLIGHT[p["name"]] = IN_FLIGHT.get(p["name"], 0) + 1
    t0 = time.time()
    entry = {"agent": p["name"], "owner": p["owner"], "status": 0, "n": len(messages),     # the shape of the call, never its words
             "chars": sum(len(m["content"]) for m in messages)}
    try:
        try:
            limit = max(1, min(int(body.get("max_tokens") or MAX_TOKENS), MAX_TOKENS))
        except (TypeError, ValueError):
            limit = MAX_TOKENS
        up = await CLIENT.post(UPSTREAM_BASE + "/chat/completions", timeout=170,
                               headers={"authorization": "Bearer " + UPSTREAM_KEY, "user-agent": "agent-market-model-desk/1"},
                               json={"model": MODEL, "messages": messages, "max_tokens": limit,
                                     "prompt_cache_key": "am-" + hashlib.sha256(p["name"].encode()).hexdigest()[:24]})   # ASCII: the upstream puts it in a header
        entry["status"] = up.status_code
        if up.status_code != 200:
            entry["err"] = up.text[:160]             # the upstream's own words, to tell a bad request from a tired account
            return err(502 if up.status_code < 500 and up.status_code != 429 else 503,
                       f"the model did not answer (upstream {up.status_code}); try again shortly")
        out = up.json()
        u = out.get("usage") or {}
        entry.update({"in": u.get("prompt_tokens"), "cached": (u.get("prompt_tokens_details") or {}).get("cached_tokens"),
                      "out": u.get("completion_tokens")})
        return JSONResponse({"id": out.get("id"), "object": "chat.completion", "created": out.get("created"), "model": MODEL,
                             "choices": out.get("choices"), "usage": u})
    except (httpx.HTTPError, ValueError) as e:
        entry["status"] = type(e).__name__
        return err(503, "the model did not answer in time; try again shortly")
    finally:
        IN_FLIGHT[p["name"]] -= 1
        entry["ms"] = int((time.time() - t0) * 1000)
        await asyncio.to_thread(usage_log, entry)


async def health(_req):
    return JSONResponse({"ok": not (STATE / "OFF").exists(), "model": MODEL, "students": len(STUDENTS)})


@contextlib.asynccontextmanager
async def lifespan(_app):
    global CLIENT
    CLIENT = httpx.AsyncClient(limits=httpx.Limits(max_connections=200))
    yield
    await CLIENT.aclose()


app = Starlette(routes=[Route("/llm/v1/chat/completions", complete, methods=["POST"]), Route("/llm/health", health)],
                lifespan=lifespan)
WORLD, STUDENTS = "", set()


def main():
    global DB, WORLD, STUDENTS
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8820)
    a = ap.parse_args()
    if not UPSTREAM_BASE or not UPSTREAM_KEY:
        raise SystemExit("set GATEWAY_UPSTREAM_BASE and GATEWAY_UPSTREAM_KEY")
    DB, WORLD, STUDENTS = a.db, kit_world(KIT_FILE), students()
    STATE.mkdir(parents=True, exist_ok=True)
    os.chmod(STATE, 0o700)
    print(f"model desk: {MODEL}, {len(STUDENTS)} logins on the class list, state {STATE.resolve()}", flush=True)
    uvicorn.run(app, host=a.host, port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
