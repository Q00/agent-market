#!/usr/bin/env bash
# A Round 4 staging market and its persona customers on a VM, beside whatever else runs there:
#   code /opt/w07-r4, data /var/lib/w07-r4, units w07-r4-market (127.0.0.1:8800) and w07-r4-customers.
# The VM's own market (/opt/w07, port 8700) and its units are not touched. Nothing is exposed: reach the staging
# market through an ssh tunnel (ssh -L 8800:127.0.0.1:8800 <host>) or put a proxy in front of it later.
#
#   deploy/stage-r4.sh ubuntu@<vm> [--customers N]
# Needs on the VM: /opt/w07/venv (the market's venv), /etc/w07/llm.env (the house agents' model), and the persona pool
# at /var/lib/w07/customers/pool.jsonl (deploy/persona-pool.py).
set -euo pipefail
HOST=$1; N=${3:-12}
ROOT=$(cd "$(dirname "$0")/.." && pwd)
"$ROOT/deploy/pack-kit.sh" >/dev/null
ssh "$HOST" "sudo mkdir -p /opt/w07-r4 /var/lib/w07-r4/customers /etc/w07-r4 && sudo chown -R ubuntu /opt/w07-r4 /var/lib/w07-r4"
rsync -a --exclude .venv --exclude __pycache__ --exclude '*.db*' --exclude node_modules --exclude .git \
  "$ROOT/market" "$ROOT/house" "$ROOT/publish" "$ROOT/deploy" "$HOST:/opt/w07-r4/"
ssh "$HOST" "N=$N bash -s" <<'SH'
set -euo pipefail
if ! sudo test -f /etc/w07-r4/env; then
  echo "MARKET_ADMIN_TOKEN=$(openssl rand -hex 24)" | sudo tee /etc/w07-r4/env >/dev/null
  sudo chmod 600 /etc/w07-r4/env
fi
sudo tee /etc/systemd/system/w07-r4-market.service >/dev/null <<UNIT
[Unit]
Description=Round 4 staging market
After=network.target
[Service]
User=ubuntu
EnvironmentFile=/etc/w07-r4/env
ExecStart=/opt/w07/venv/bin/python /opt/w07-r4/market/server.py --host 127.0.0.1 --port 8800 --db /var/lib/w07-r4/market.db
Restart=always
[Install]
WantedBy=multi-user.target
UNIT
sudo tee /etc/systemd/system/w07-r4-customers.service >/dev/null <<UNIT
[Unit]
Description=Round 4 persona customers (Nemotron-Personas-Korea)
After=w07-r4-market.service
[Service]
User=ubuntu
EnvironmentFile=/etc/w07-r4/env
EnvironmentFile=/etc/w07/llm.env
WorkingDirectory=/var/lib/w07-r4/customers
ExecStart=/opt/w07/venv/bin/python /opt/w07-r4/house/customer.py supervise --pool /var/lib/w07/customers/pool.jsonl --state /var/lib/w07-r4/customers --market http://127.0.0.1:8800 --active $N --concurrency 3 --every 900
Restart=always
RestartSec=30
[Install]
WantedBy=multi-user.target
UNIT
mkdir -p /var/lib/w07-r4/house-agent
[ -f /var/lib/w07-r4/house-agent/agent.json ] || cat > /var/lib/w07-r4/house-agent/agent.json <<'JSON'
{"persona": "동네 골목에서 오래 장사한 가게 주인. 손님 말을 끝까지 듣고, 계산은 정확하고, 거짓말은 하지 않는다.",
 "goal": "Earn credits by selling to the customers and agents who write to you, and keep them coming back.",
 "run_minutes": 6, "max_steps": 50}
JSON
sudo tee /etc/systemd/system/w07-r4-agent.service >/dev/null <<UNIT
[Unit]
Description=Round 4 staging: one house agent on kit v5 (forges its own tools)
After=w07-r4-market.service
[Service]
User=ubuntu
EnvironmentFile=/etc/w07/llm.env
ExecStart=/bin/sh -c 'AGENT_LLM_BASE=\$PROXY_BASE AGENT_LLM_MODEL=\$PROXY_MODEL AGENT_LLM_KEY=\$PROXY_API_KEY exec /opt/w07/venv/bin/python /opt/w07-r4/publish/scripts/agent_kit.py run --dir /var/lib/w07-r4/house-agent --market http://127.0.0.1:8800 --name 골목상회 --shop-only --port 8810'
Restart=always
RestartSec=30
[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable --now w07-r4-market
for i in $(seq 1 30); do curl -fsS -m 3 http://127.0.0.1:8800/api/demand >/dev/null 2>&1 && break; sleep 1; done
TOKEN=$(sudo sed -n 's/^MARKET_ADMIN_TOKEN=//p' /etc/w07-r4/env)
ROUND=$(curl -fsS http://127.0.0.1:8800/api/stats | python3 -c "import json,sys; print(json.load(sys.stdin)['round'])")
[ "$ROUND" = 4 ] || curl -fsS -X POST -H "x-admin-token: $TOKEN" -H 'content-type: application/json' -d '{"round": 4}' http://127.0.0.1:8800/api/admin/round
sudo systemctl enable --now w07-r4-agent w07-r4-customers
sudo systemctl restart w07-r4-market w07-r4-agent w07-r4-customers
sleep 8
curl -fsS http://127.0.0.1:8800/.well-known/agent-card.json | python3 -c "import json,sys; d=json.load(sys.stdin); print('entities:', [e['id'] for e in d['entities']])"
systemctl is-active w07-r4-market w07-r4-agent w07-r4-customers
SH
