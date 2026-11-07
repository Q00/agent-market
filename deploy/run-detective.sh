#!/bin/bash
# Runs on the demo VM: sends the ReAct detective in, then grades its report against the planted answer key.
#   run-detective.sh [minutes]
set -e
APP=/opt/w07; OUT=/var/lib/w07/detective; MINUTES=${1:-12}
mkdir -p $OUT; F=$OUT/report-$(date +%m%d-%H%M).json
sudo sh -c "set -a; . /etc/w07/llm.env; . /etc/w07/detective.env; set +a; DETECTIVE_MCP_URL=http://127.0.0.1:8787/mcp/\$MCP_PATH_TOKEN; export DETECTIVE_MCP_URL; cd $OUT; exec sudo -E -u ubuntu PYTHONPATH=$APP/house $APP/venv/bin/python $APP/house/detective.py --out $F --minutes $MINUTES"
sudo python3 $APP/deploy/scenario-score.py "$F"
echo "report: $F"
