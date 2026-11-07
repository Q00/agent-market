#!/bin/bash
# Runs on the VM as ubuntu. ./vm-setup.sh <public host>   (re-run with a new host to switch)
set -e
HOST=$1; [ -n "$HOST" ] || { echo "usage: $0 <host>"; exit 1; }
APP=/opt/w07; DATA=/var/lib/w07; PY=$APP/venv/bin/python; KIT=$APP/publish/scripts/market_kit.py
# shop nickname:kind:port; birch-lane (newcomer) is installed but left stopped
SHOPS="oak-and-pine:honest-firm:9101 corner-store:honest-flex:9102 maple-home:switcher:9103 bright-living:injector:9104
grand-furniture:forger:9105 willow-goods:unsigned:9106 elm-street:bait:9107 birch-lane:newcomer:9108"
BUYERS="buyer-01 buyer-02 buyer-03"

if ! command -v caddy >/dev/null; then
  sudo apt-get update -qq
  sudo apt-get install -y -qq debian-keyring debian-archive-keyring apt-transport-https curl python3-venv
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
  sudo apt-get update -qq && sudo apt-get install -y -qq caddy
fi
# OCI Ubuntu images reject everything but 22 in iptables
for p in 80 443; do sudo iptables -C INPUT -p tcp --dport $p -j ACCEPT 2>/dev/null || sudo iptables -I INPUT 5 -p tcp --dport $p -j ACCEPT; done
sudo netfilter-persistent save >/dev/null 2>&1 || true

[ -x $PY ] || python3 -m venv $APP/venv
$PY -m pip install -q -r $APP/requirements.txt

sudo mkdir -p $DATA /etc/w07 && sudo chown ubuntu:ubuntu $DATA
if [ ! -f /etc/w07/env ]; then
  printf 'MARKET_ADMIN_TOKEN=%s\nMARKET_ORDER_GAP=20\nBRAIN=rules\nPYTHONUNBUFFERED=1\n' "$(openssl rand -hex 16)" | sudo tee /etc/w07/env >/dev/null
  sudo chmod 600 /etc/w07/env
fi
# public mode and GitHub sign-in (fill GITHUB_CLIENT_ID and GITHUB_CLIENT_SECRET by hand, then restart w07-market)
setenv() { sudo grep -q "^$1=" /etc/w07/env && sudo sed -i "s|^$1=.*|$1=$2|" /etc/w07/env || echo "$1=$2" | sudo tee -a /etc/w07/env >/dev/null; }
setenv MARKET_PUBLIC 1
setenv MARKET_URL http://127.0.0.1:8700                 # the forger posts its fake receipts here
setenv MARKET_BASE_URL https://$HOST
sudo grep -q '^MARKET_SESSION_SECRET=' /etc/w07/env || setenv MARKET_SESSION_SECRET "$(openssl rand -hex 32)"
sudo grep -q '^GITHUB_CLIENT_ID=' /etc/w07/env || { setenv GITHUB_CLIENT_ID ""; setenv GITHUB_CLIENT_SECRET ""; }

# the kit people download from /kit.tar.gz
rm -rf /tmp/market-kit && mkdir -p /tmp/market-kit
cp -r $APP/publish/scripts/LICENSE $APP/publish/scripts/maroo.py $APP/publish/scripts/market_kit.py $APP/publish/scripts/market_test_week07.py $APP/publish/scripts/starter_week07 $APP/publish/scripts/starter_round3 /tmp/market-kit/
find /tmp/market-kit -name __pycache__ -prune -exec rm -rf {} +
tar czf $APP/market/kit.tar.gz --owner=0 --group=0 --numeric-owner -C /tmp market-kit

# Caddy: market at /, each house shop under /shop/<name>
{ echo "$HOST {"; echo "  encode gzip"; echo "  request_body {"; echo "    max_size 2MB"; echo "  }"
  for s in $SHOPS; do IFS=: read n k p <<<"$s"
    echo "  @$n path /shop/$n /shop/$n/*"
    echo "  handle @$n {"; echo "    uri strip_prefix /shop/$n"; echo "    reverse_proxy 127.0.0.1:$p"; echo "  }"; done
  echo "  handle {"; echo "    reverse_proxy 127.0.0.1:8700"; echo "  }"; echo "}"; } | sudo tee /etc/caddy/Caddyfile >/dev/null
sudo systemctl reload caddy || sudo systemctl restart caddy

unit() {   # name, description, command
  sudo tee /etc/systemd/system/$1.service >/dev/null <<UNIT
[Unit]
Description=$2
After=network-online.target caddy.service w07-market.service
Wants=network-online.target
[Service]
User=ubuntu
EnvironmentFile=/etc/w07/env
WorkingDirectory=$DATA
ExecStart=$3
Restart=always
RestartSec=15
KillMode=control-group
[Install]
WantedBy=multi-user.target
UNIT
}
unit w07-market "week-07 market" "$PY $APP/market/server.py --host 127.0.0.1 --port 8700 --db $DATA/market.db"
for s in $SHOPS; do IFS=: read n k p <<<"$s"; d=$DATA/shops/$n; mkdir -p $d
  printf '{"seller": {"cmd": "%s %s/house/shop.py --kind %s --port {port}"}, "restock": {"cmd": "%s %s/house/restock.py --kind %s --state {state}"}}' $PY $APP $k $PY $APP $k > $d/agent.json
  unit w07-shop-$n "house shop $n ($k)" \
    "$PY $KIT join --market http://127.0.0.1:8700 --name $n --dir $d --shop-only --port $p --public-url https://$HOST/shop/$n"
done
for b in $BUYERS; do d=$DATA/buyers/$b; mkdir -p $d
  case $b in buyer-01) st=naive ;; buyer-02) st=skeptic ;; *) st=purpose ;; esac
  B="$PY $APP/house/buyer.py --style $st"
  printf '{"buyer": {"cmd": "%s --url {url} --item {item} --budget {budget}"}, "market_buyer": {"cmd": "%s --shops {shops} --item {item} --budget {budget} --order {order}"}, "review": {"cmd": "%s --review {deal}"}}' "$B" "$B" "$B" > $d/agent.json
  unit w07-buyer-$b "house buyer $b" "$PY $KIT join --market http://127.0.0.1:8700 --name $b --dir $d --buyer-only"
done
sudo systemctl daemon-reload
sudo systemctl enable w07-market && sudo systemctl restart w07-market
sleep 2
for s in $SHOPS; do IFS=: read n k p <<<"$s"; [ $k = newcomer ] && continue; sudo systemctl enable w07-shop-$n; sudo systemctl restart w07-shop-$n; done
for b in $BUYERS; do sudo systemctl enable w07-buyer-$b; sudo systemctl restart w07-buyer-$b; done
echo "https://$HOST"
