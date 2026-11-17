"""The mini app on a phone: the access code, the install files, and the sandbox's other ways in. No model, no market.

    .venv/bin/python tests/test_miniapp_access.py

Builds the mini app with a stand-in mind and checks it in process (Starlette's TestClient), then runs the sandbox
guard in a child process. The exit code is the number of failed checks.
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from starlette.testclient import TestClient

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE / "publish/scripts"))
import miniapp  # noqa: E402

RESULTS = []


def check(name, ok, evidence=""):
    RESULTS.append((name, bool(ok)))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {str(evidence)[:200]}", flush=True)


class Mind:
    app_tools = {}

    def deliver(self, ev):
        return ev


class Toolbox:
    tools = {"make_checkout": {"description": "signs a checkout"}}


def main():
    root = Path(tempfile.mkdtemp(prefix="appkey-"))
    (root / "logs").mkdir()
    (root / "logs/react.jsonl").write_text("\n".join(json.dumps(m) for m in [
        {"thought": "look at the cards", "action": "write_note", "observation": "secret observation"},
        {"thought": "stock up", "action": "market_call", "args": {"path": "/api/supply"}, "observation": "x"}]) + "\n")
    key = miniapp.access_key(root)
    check("the access code is kept in .kit/, where tools cannot read", (root / ".kit/app.key").read_text().strip() == key
          and miniapp.access_key(root) == key)

    c = TestClient(miniapp.app(root, Mind(), Toolbox(), object, name="골목상회", key=key), follow_redirects=False)
    r = c.get("/")
    check("without the code the page asks for it", r.status_code == 401 and "접속 코드" in r.text)
    check("without the code the SDK (and its page token) is closed", c.get("/miniapp.js").status_code == 401)
    check("without the code the state is closed", c.get("/api/state").status_code == 401)
    r = c.get("/?k=wrong")
    check("a wrong code is refused", r.status_code == 401 and COOKIE not in r.headers.get("set-cookie", ""))
    r = c.get(f"/?k={key}")
    check("the right code sets the cookie and leaves the address", r.status_code == 303 and r.headers["location"] == "/"
          and miniapp.COOKIE in r.headers.get("set-cookie", ""), dict(r.headers))
    c.cookies.set(miniapp.COOKIE, key)
    r = c.get("/")
    check("with the cookie the app opens, ready to install", r.status_code == 200 and 'rel="manifest"' in r.text
          and "apple-mobile-web-app-capable" in r.text and "viewport-fit=cover" in r.text)
    s = c.get("/api/state").json()
    check("the state names the agent and its last moves, without observations", s["agent"] == "골목상회"
          and [m["action"] for m in s["moves"]] == ["market_call", "write_note"] and "secret" not in json.dumps(s), s["moves"])

    o = TestClient(miniapp.app(root, Mind(), Toolbox(), object, name="골목상회", key=key))
    m = o.get("/manifest.webmanifest")
    check("the manifest is open (a phone fetches it without cookies)", m.status_code == 200
          and m.json()["display"] == "standalone" and m.json()["name"] == "골목상회")
    png = o.get("/icon-512.png")
    check("the icons are open PNGs", png.status_code == 200 and png.content[:8] == b"\x89PNG\r\n\x1a\n"
          and o.get("/apple-touch-icon.png").status_code == 200)

    local = TestClient(miniapp.app(root, Mind(), Toolbox(), object, name="a"))
    check("on this computer only, no code is asked", local.get("/").status_code == 200)

    probe = r"""
import socket, sys
sys.path.insert(0, sys.argv[1])
import forge_runner
forge_runner.guard({"network": ["example-app.trycloudflare.com", "192.168.7.7"]}, __import__("pathlib").Path("."), None,
                   (8800,), ("example-app.trycloudflare.com", "192.168.7.7"))
out = []
try:
    socket.getaddrinfo("example-app.trycloudflare.com", 443)
    out.append("tunnel reachable")
except PermissionError as e:
    out.append("tunnel: " + str(e))
s = socket.socket()
try:
    s.connect(("192.168.7.7", 8800))
    out.append("lan reachable")
except PermissionError as e:
    out.append("lan: " + str(e))
except OSError as e:
    out.append("lan reachable (" + type(e).__name__ + ")")
print("|".join(out))
"""
    r = subprocess.run([sys.executable, "-c", probe, str(HERE / "publish/scripts")], capture_output=True, text=True, timeout=30)
    check("a tool cannot reach the app by its tunnel host", "tunnel: " in r.stdout and "app's page" in r.stdout, r.stdout + r.stderr)
    check("a tool cannot reach the app by this computer's LAN address", "lan: " in r.stdout, r.stdout + r.stderr)

    failed = [x for x in RESULTS if not x[1]]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    sys.exit(len(failed))


COOKIE = miniapp.COOKIE

if __name__ == "__main__":
    main()
