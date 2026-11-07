"""방침 예산 안에서 소량 입고합니다 (키트의 restock 훅).   python restock.py --state .market"""
import argparse
import json
import random
import time
from pathlib import Path

import rules


def read(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


ap = argparse.ArgumentParser()
ap.add_argument("--state", required=True)
state_dir = Path(ap.parse_args().state)
inventory = read(state_dir / "inventory.json", {})
catalog = read(state_dir / "catalog.json", {})
window = inventory.get("restock") or {}
buy = []
if window.get("open"):
    ledger_file = state_dir / "restock_spent.json"
    spent = read(ledger_file, {})
    key = str(window.get("closes_at"))
    used = spent.get(key, 0)
    buy = rules.plan_restock(inventory, catalog, rules.load_policy("shop"), used, random.Random(int(time.time()) // 600))
    used += sum(catalog[b["sku"]]["list_price"] * 0.75 * b["qty"] for b in buy)
    ledger_file.write_text(json.dumps({key: used}), encoding="utf-8")
print(json.dumps({"buy": buy}), flush=True)
