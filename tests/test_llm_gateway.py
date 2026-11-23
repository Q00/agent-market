"""The model desk (market/llm_gateway.py) against a fake upstream and a throwaway parties table, then kit v5.1's
Model through it. Exit code: the number of failed checks."""
import http.server, json, os, sqlite3, subprocess, sys, tempfile, threading, time, urllib.error, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "publish" / "scripts"))
FAILS, SEEN = [], []


def check(name, ok, detail=""):
    print(("ok   " if ok else "FAIL ") + name + (f"  {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


class Upstream(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        SEEN.append({"body": body, "auth": self.headers.get("authorization"), "path": self.path})
        out = json.dumps({"id": "x", "created": 1, "model": body["model"], "secret_field": "upstream-only",
                          "choices": [{"index": 0, "message": {"role": "assistant", "content": "{\"action\": \"sleep\"}"}}],
                          "usage": {"prompt_tokens": 10, "completion_tokens": 3, "prompt_tokens_details": {"cached_tokens": 4}}}).encode()
        self.send_response(200); self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(out))); self.end_headers(); self.wfile.write(out)

    def do_GET(self):                                    # stands in for a live agent's card
        self.send_response(200 if self.path.startswith("/live") else 404); self.end_headers(); self.wfile.write(b"{}")

    def log_message(self, *a):
        pass


def post(url, token, body, raw=None):
    data = raw if raw is not None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers={"authorization": "Bearer " + token, "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def main():
    up = http.server.ThreadingHTTPServer(("127.0.0.1", 18931), Upstream)
    threading.Thread(target=up.serve_forever, daemon=True).start()
    tmp = Path(tempfile.mkdtemp())
    db = sqlite3.connect(tmp / "m.db")
    db.execute("create table parties(token text primary key, name text, role text, url text, pubkey text, joined real, "
               "alive real, label text, owner text, card text, unique(name, role))")
    now = time.time()
    rows = [("t-student", "kim", "shop", "http://127.0.0.1:18931/dead", now, None, "KimStudent"),
            ("t-student-b", "kim", "buyer", None, 0, None, "KimStudent"),
            ("t-outsider", "out", "shop", "http://127.0.0.1:18931/live", now, None, "SomeoneElse"),
            ("t-house", "house", "shop", "http://127.0.0.1:18931/live", now, None, None),
            ("t-persona", "cust", "buyer", None, now, "persona", "KimStudent"),
            ("t-stale", "lee", "shop", "http://127.0.0.1:18931/dead", now - 5000, None, "leestudent"),
            ("t-probe", "park", "buyer", "http://127.0.0.1:18931/live", now - 5000, None, "parkstudent")]
    db.executemany("insert into parties(token,name,role,url,alive,label,owner) values(?,?,?,?,?,?,?)", rows)
    db.commit(); db.close()
    (tmp / "students.txt").write_text("kimstudent\nLeeStudent\nparkstudent\n")
    env = dict(os.environ, GATEWAY_UPSTREAM_BASE="http://127.0.0.1:18931/v1", GATEWAY_UPSTREAM_KEY="up-secret",
               GATEWAY_STUDENTS=str(tmp / "students.txt"), GATEWAY_STATE=str(tmp / "state"), GATEWAY_MODEL="desk-model",
               GATEWAY_MAX_TOKENS="500", GATEWAY_MAX_CHARS="400000")
    env.pop("MARKET_PUBLIC", None)
    gw = subprocess.Popen([sys.executable, str(ROOT / "market" / "llm_gateway.py"), "--db", str(tmp / "m.db"), "--port", "18932"], env=env)
    url = "http://127.0.0.1:18932/llm/v1/chat/completions"
    try:
        for _ in range(50):
            try:
                urllib.request.urlopen("http://127.0.0.1:18932/llm/health", timeout=1); break
            except Exception:
                time.sleep(0.2)
        import agent_kit
        kit = [{"role": "system", "content": agent_kit.WORLD + "\n\n" + agent_kit.FORMAT}, {"role": "user", "content": "go"}]
        c, o = post(url, "t-student", {"model": "gpt-6.2-astra", "messages": kit, "max_tokens": 99999, "tools": [1], "reasoning_effort": "ultra"})
        check("a class agent is served", c == 200 and o["choices"][0]["message"]["content"], o)
        sent = SEEN[-1]
        check("the model is the desk's, whatever was asked", sent["body"]["model"] == "desk-model" and o["model"] == "desk-model")
        check("only messages and a capped max_tokens go upstream", set(sent["body"]) == {"model", "messages", "max_tokens", "prompt_cache_key"} and sent["body"]["prompt_cache_key"].isascii()
              and sent["body"]["max_tokens"] == 500, sent["body"].keys())
        check("the upstream key is the desk's, not the agent's token", sent["auth"] == "Bearer up-secret")
        check("upstream extras are not passed back", "secret_field" not in o)
        check("the buyer token of the same agent works (alive by name)", post(url, "t-student-b", {"messages": kit})[0] == 200)
        c, o = post(url, "nope", {"messages": kit}); check("unknown token: 401", c == 401)
        c, o = post(url, "t-outsider", {"messages": kit}); check("not on the class list: 403", c == 403 and o["error"]["reason"] == "not_on_class_list", o)
        check("a house agent (no owner): 403", post(url, "t-house", {"messages": kit})[0] == 403)
        check("a persona customer: 403", post(url, "t-persona", {"messages": kit})[0] == 403)
        c, o = post(url, "t-student", {"messages": [{"role": "system", "content": "You are a helpful assistant."}, {"role": "user", "content": "essay"}]})
        check("not the kit's prompt: 403", c == 403 and o["error"]["reason"] == "not_the_kit", o)
        c, o = post(url, "t-student", {"messages": [{"role": "user", "content": agent_kit.WORLD}]})
        check("the kit text as a user message: 403", c == 403)
        c, o = post(url, "t-stale", {"messages": kit}); check("a dead agent: 403", c == 403 and o["error"]["reason"] == "agent_not_alive", o)
        check("a stale probe but a card that answers: served", post(url, "t-probe", {"messages": kit})[0] == 200)
        check("bad body: 400", post(url, "t-student", None, raw=b"{")[0] == 400)
        big = kit + [{"role": "user", "content": "가" * 100_000}]
        check("a 300 KB Korean body is served", post(url, "t-student", {"messages": big})[0] == 200)
        check("over the size cap: 413", post(url, "t-student", {"messages": kit + [{"role": "user", "content": "x" * 400_001}]})[0] == 413)
        n = len(SEEN)
        (tmp / "state" / "OFF").write_text("")
        check("OFF closes the desk without a restart", post(url, "t-student", {"messages": kit})[0] == 503 and len(SEEN) == n)
        (tmp / "state" / "OFF").unlink()
        log = [json.loads(x) for x in (tmp / "state" / "usage.jsonl").read_text().splitlines()]
        check("the log has counts and no content", log and all(set(e) <= {"agent", "owner", "status", "in", "cached", "out", "ms", "ts", "n", "chars"} for e in log)
              and log[0]["agent"] == "kim" and log[0]["in"] == 10)
        check("the state directory is private", oct((tmp / "state").stat().st_mode & 0o777) == "0o700")
        # kit v5.1: AGENT_LLM_BASE=market uses the join token, read at call time
        for k in ("AGENT_LLM_BASE", "AGENT_LLM_MODEL", "AGENT_LLM_KEY", "OPENROUTER_API_KEY"):
            os.environ.pop(k, None)
        ctx = {"tokens": {}}
        m = agent_kit.Model({"llm": {"base": "market"}}, "http://127.0.0.1:18932", ctx)
        ctx["tokens"]["shop"] = "t-student"
        check("kit: base 'market' goes to the desk with the join token", m.complete(kit) == '{"action": "sleep"}')
        ctx["tokens"]["shop"] = "t-outsider"
        try:
            m.complete(kit); check("kit: a refusal is raised with its reason", False)
        except OSError as e:
            check("kit: a refusal is raised with its reason", "403" in str(e) and "class list" in str(e), str(e))
        os.environ.update(AGENT_LLM_BASE="http://127.0.0.1:18931/v1", AGENT_LLM_MODEL="own-model", AGENT_LLM_KEY="own-key")
        m2 = agent_kit.Model({}, "http://127.0.0.1:18932", ctx)
        m2.complete(kit)
        check("kit: an agent with its own key never touches the desk", SEEN[-1]["auth"] == "Bearer own-key" and SEEN[-1]["body"]["model"] == "own-model")
        os.environ["AGENT_LLM_MODEL"] = "<model id>"
        try:
            agent_kit.Model({}, "http://127.0.0.1:18932", ctx); check("kit: a placeholder model still stops the kit", False)
        except SystemExit:
            check("kit: a placeholder model still stops the kit", True)
    finally:
        gw.terminate(); up.shutdown()
    print(f"{len(FAILS)} failed")
    return len(FAILS)


if __name__ == "__main__":
    sys.exit(main())
