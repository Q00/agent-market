#!/bin/bash
# Runs on the VM: runs the Market Detective MCP server from a pinned commit, as an account that cannot read the market's secrets.
#   detective-setup.sh <repo url> <commit>
set -e
REPO=$1; SHA=$2; [ -n "$SHA" ] || { echo "usage: $0 <repo url> <commit>"; exit 1; }
HOME_D=/home/detective; SRC=$HOME_D/src; APP=$SRC/integrations/market-detective-mcp
command -v node >/dev/null || { sudo apt-get update -qq; sudo apt-get install -y -qq nodejs npm git; }
id -u detective >/dev/null 2>&1 || sudo useradd -m -s /usr/sbin/nologin detective
sudo chmod 700 $HOME_D
sudo -u detective sh -c "[ -d $SRC/.git ] || git clone -q $REPO $SRC; cd $SRC && git fetch -q origin $SHA && git checkout -q --detach $SHA"
[ "$(sudo -u detective git -C $SRC rev-parse HEAD)" = "$SHA" ] || { echo "not the pinned commit"; exit 1; }
sudo -u detective sh -c "cd $APP && npm ci --ignore-scripts --no-audit --no-fund --silent"
sudo sh -c "[ -f /etc/w07/detective.env ] || printf 'MCP_PATH_TOKEN=%s\n' \$(openssl rand -hex 24) > /etc/w07/detective.env; chmod 600 /etc/w07/detective.env"
sudo tee /etc/systemd/system/w07-detective.service >/dev/null <<UNIT
[Unit]
Description=Market Detective MCP (third-party code, sandboxed)
After=network-online.target w07-market.service
[Service]
User=detective
EnvironmentFile=/etc/w07/detective.env
Environment=PORT=8787 MARKET_BASE=http://127.0.0.1:8700 DATA_MODE=live LIVE_LEDGER_ENTRIES=6000 MARKET_GAP_MS=50
WorkingDirectory=$APP
ExecStart=$(command -v node) server.js
Restart=on-failure
RestartSec=5
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectKernelTunables=yes
ProtectControlGroups=yes
ReadWritePaths=$APP/data
InaccessiblePaths=/var/lib/w07 /etc/w07 /etc/caddy /home/ubuntu
[Install]
WantedBy=multi-user.target
UNIT
sudo -u detective mkdir -p $APP/data
sudo systemctl daemon-reload; sudo systemctl enable w07-detective >/dev/null 2>&1; sudo systemctl restart w07-detective
sleep 4; systemctl is-active w07-detective
sudo -u detective test -r /etc/w07/env && echo "WARNING: detective can read /etc/w07/env" || echo "isolated: /etc/w07/env unreadable"
sudo -u detective test -r /var/lib/w07/buyers/buyer-01/buyer_wallet.key && echo "WARNING: detective can read a wallet" || echo "isolated: wallets unreadable"
