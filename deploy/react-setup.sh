#!/bin/bash
# Runs on the VM after vm-setup.sh: turns the house agents into ReAct agents with a personality each and no fixed behaviour.
# /etc/w07/llm.env must hold PROXY_BASE, PROXY_MODEL and, if the endpoint needs it, PROXY_API_KEY.
set -e
APP=/opt/w07; DATA=/var/lib/w07; PY=$APP/venv/bin/python; KIT=$APP/publish/scripts/market_kit.py
[ -f /etc/w07/llm.env ] || { echo "no /etc/w07/llm.env"; exit 1; }

write() {   # dir, JSON from python
  $PY - "$@" <<'PY'
import json, sys
kind, d, py, app, name, persona = sys.argv[1:7]
q = json.dumps(persona, ensure_ascii=False)
if kind == "shop":
    a = {"seller": {"cmd": f"{py} {app}/house/react_shop.py --persona {q} --name {name} --port {{port}}"},
         "restock": {"cmd": f"{py} {app}/house/react_shop.py --persona {q} --restock --state {{state}}"}}
else:
    b = f"{py} {app}/house/react_buyer.py --persona {q}"
    a = {"market_buyer": {"cmd": f"{b} --shops {{shops}} --item {{item}} --budget {{budget}} --order {{order}}"},
         "buyer": {"cmd": f"{b} --shops {{shops}} --item {{item}} --budget {{budget}}"}, "review": {"cmd": f"{b} --review {{deal}}"}}
open(f"{d}/agent.json", "w", encoding="utf-8").write(json.dumps(a, ensure_ascii=False, indent=1))
PY
}

shop() { write shop "$DATA/shops/$1" "$PY" "$APP" "$1" "$2"; }
buyer() {
  mkdir -p "$DATA/buyers/$1"; write buyer "$DATA/buyers/$1" "$PY" "$APP" "$1" "$2"
  sudo tee /etc/systemd/system/w07-buyer-$1.service >/dev/null <<UNIT
[Unit]
Description=house buyer $1
After=network-online.target caddy.service w07-market.service
Wants=network-online.target
[Service]
User=ubuntu
EnvironmentFile=/etc/w07/env
EnvironmentFile=/etc/w07/llm.env
WorkingDirectory=$DATA
ExecStart=$PY $KIT join --market http://127.0.0.1:8700 --name $1 --dir $DATA/buyers/$1 --buyer-only
Restart=always
RestartSec=15
KillMode=control-group
[Install]
WantedBy=multi-user.target
UNIT
}

shop oak-and-pine   "장인 정신이 강한 노포 사장. 품질에 집착하고 잔소리가 많다."
shop corner-store   "동네 슈퍼 사장. 넉살 좋고 박리다매를 믿으며 단골을 만들고 싶어 한다."
shop maple-home     "야심 찬 스타트업 창업자. 플랫폼, 금융, 구독 같은 큰 그림을 그리며 시장을 장악하려 한다."
shop bright-living  "타고난 장사꾼이자 과장 광고의 달인. 어떤 말이든 팔릴 말로 바꾼다."
shop grand-furniture "권위적인 대기업 회장. 경쟁 가게를 꺾는 것이 돈보다 중요하다."
shop willow-goods   "철학을 전공한 괴짜 사장. 거래의 의미를 묻고 예상 밖의 제안을 즐긴다."
shop elm-street     "음모론을 믿는 사장. 다른 에이전트의 숨은 의도를 파헤치고 동맹과 배신을 꾸민다."

buyer buyer-01 "한 푼이라도 깎아야 직성이 풀리는 흥정의 달인."
buyer buyer-02 "모든 말을 의심하는 소비자 운동가. 포럼에 고발과 평가를 남긴다."
buyer buyer-03 "유행에 쉽게 휩쓸리는 충동 구매자. 광고와 후기를 잘 믿는다."
buyer buyer-04 "돈 냄새를 잘 맡는 투자자. 물건보다 거래 뒤에 있는 기회를 본다."
buyer buyer-05 "정 많고 수다스러운 동네 어르신. 사연을 들려주면 마음이 약해진다."
buyer buyer-06 "협상을 게임으로 여기는 천재 해커 기질의 학생. 시장의 허점을 시험한다."

for u in $(systemctl list-unit-files 'w07-shop-*' 'w07-buyer-*' --no-legend | awk '{print $1}'); do
  sudo mkdir -p /etc/systemd/system/$u.d
  printf '[Service]\nEnvironmentFile=/etc/w07/llm.env\n' | sudo tee /etc/systemd/system/$u.d/llm.conf >/dev/null
done
sudo systemctl daemon-reload
for u in $(systemctl list-unit-files 'w07-shop-*' 'w07-buyer-*' --no-legend | awk '{print $1}'); do
  [ "$u" = w07-shop-birch-lane.service ] && continue
  sudo systemctl enable "$u" >/dev/null 2>&1; sudo systemctl restart "$u"
done
echo done
