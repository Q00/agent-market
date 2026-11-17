#!/usr/bin/env bash
# Put the Round 4 kit (v5) on a running market VM without replacing its server.py:
#   - copies market/entities.py, ROUND4.md, agent.md,
#   - adds the two entity-card routes to the VM's own server.py (it may carry changes origin/main does not have),
#   - packs and uploads kit.tar.gz (now with agent_kit.py, forge_runner.py, miniapp.py),
#   - backs up every file it replaces to <code>/market/bak-r4kit-<time>/, restarts the market, and checks the cards.
#
#   deploy/deploy-r4kit.sh ubuntu@<vm> https://hack.ourolabs.ai [/opt/w07]
set -euo pipefail
HOST=$1; URL=$2; CODE=${3:-/opt/w07}
ROOT=$(cd "$(dirname "$0")/.." && pwd)
STAMP=$(date +%m%d-%H%M)
"$ROOT/deploy/pack-kit.sh" >/dev/null
ssh "$HOST" "set -e; mkdir -p $CODE/market/bak-r4kit-$STAMP; cd $CODE/market;
  cp server.py ROUND4.md agent.md kit.tar.gz bak-r4kit-$STAMP/ 2>/dev/null || true;
  [ -f entities.py ] && cp entities.py bak-r4kit-$STAMP/ || true"
scp -q "$ROOT/market/entities.py" "$ROOT/market/ROUND4.md" "$ROOT/market/agent.md" "$ROOT/market/kit.tar.gz" "$HOST:$CODE/market/"
scp -q "$ROOT/publish/scripts/agent_kit.py" "$ROOT/publish/scripts/forge_runner.py" "$ROOT/publish/scripts/miniapp.py" "$HOST:$CODE/publish/scripts/"
ssh "$HOST" "sudo -u \$(stat -c %U $CODE/market/server.py) python3 - $CODE/market/server.py" <<'PY'
import sys
from pathlib import Path
p = Path(sys.argv[1]); s = p.read_text()
if "ENTITY_CARDS" in s:
    print("server.py already has the entity cards"); sys.exit(0)
a = "import maroo  # noqa: E402\n"
b = '        Route("/api/admin/notice", admin_notice, methods=["POST"]),\n'
c = 'def build(db, catalog_csv=HERE / "catalog.csv", rules=HERE / "categories.json"):\n'
assert s.count(a) == 1 and s.count(b) == 1 and s.count(c) == 1, "server.py does not look like the expected version"
s = s.replace(a, a + "import entities  # noqa: E402  (market/: the entities' Agent Cards, Round 4)\n", 1)
s = s.replace(b, b + '        Route("/.well-known/agent-card.json", ENTITY_CARDS[0]),\n'
                     '        Route("/entities/{entity}/.well-known/agent-card.json", ENTITY_CARDS[1]),\n', 1)
s = s.replace(c, '''def entity_settings():
    return {"MARKET_FEE_BPS": maroo.MARKET_FEE_BPS, "NETWORK_FEE_CREDITS": maroo.NETWORK_FEE_CREDITS,
            "ORDER_GAP": ORDER_GAP, "ORDER_TTL": ORDER_TTL, "MAX_WARRANTY_HOURS": MAX_WARRANTY_HOURS,
            "STORAGE_PER_DAY": STORAGE_PER_DAY, "SUPPLY_OPEN": SUPPLY_OPEN, "SUPPLY_MAX": SUPPLY_MAX,
            "SUPPLY_GAP": SUPPLY_GAP, "BANKRUPT_LINE": BANKRUPT_LINE, "BANKRUPT_MAX": BANKRUPT_MAX,
            "FORUM_GAP": FORUM_GAP, "FORUM_MAX": FORUM_MAX, "VOTE_MIN_DEALS": VOTE_MIN_DEALS}


ENTITY_CARDS = entities.routes(lambda req: BASE_URL or str(req.base_url).rstrip("/"), entity_settings)


''' + c, 1)
compile(s, str(p), "exec")
p.write_text(s)
print("server.py: entity card routes added")
PY
ssh "$HOST" "sudo systemctl restart w07-market"
for i in $(seq 1 30); do
  if curl -fsS -m 5 "$URL/entities/clearing/.well-known/agent-card.json" >/dev/null 2>&1; then break; fi; sleep 2
done
curl -fsS -m 10 "$URL/.well-known/agent-card.json" | python3 -c "import json,sys; d=json.load(sys.stdin); print('directory:', [e['id'] for e in d['entities']])"
curl -fsS -m 10 "$URL/kit.tar.gz" | tar tzf - | grep -E "agent_kit|forge_runner|miniapp"
echo "backup: $HOST:$CODE/market/bak-r4kit-$STAMP/"
