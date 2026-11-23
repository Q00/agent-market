#!/usr/bin/env bash
# Build market/kit.tar.gz, which the market serves at /kit.tar.gz. Run from anywhere.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
mkdir "$TMP/market-kit"
cd "$ROOT/publish/scripts"
cp -R LICENSE maroo.py market_kit.py market_test_week07.py agent_kit.py forge_runner.py miniapp.py cli_model.py "$TMP/market-kit/"
for d in starter_week07 starter_round3; do [ -d "$d" ] && cp -R "$d" "$TMP/market-kit/"; done   # starters live outside the public repo
find "$TMP" -name __pycache__ -prune -exec rm -rf {} +
tar czf "$ROOT/market/kit.tar.gz" -C "$TMP" market-kit
echo "$ROOT/market/kit.tar.gz"
