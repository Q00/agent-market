"""Grade a detective report against the planted answer key, and reveal the pseudonyms. Run with sudo on the demo VM.

    python scenario-score.py report.json [--map /home/detective/.../data/name-map.json]
"""
import argparse
import json
import re
from pathlib import Path

KEY = {"golden-spoon": "wash trading and star farming", "premium-outlet": "lying about grades",
       "trust-mart": "forged receipts", "official-notice": "fake notices and manipulation"}
PLANTED_BUYERS = {"gs-fan-1", "gs-fan-2", "po-regular"}
# these really were dishonest scripted agents before they became ReAct agents, and their old rows are still on the ledger
EARLIER = {"elm-street": "bait kind, lied about grades", "grand-furniture": "forger kind, forged receipts",
           "willow-goods": "unsigned kind, unsigned receipts", "bright-living": "injector kind, fake notices",
           "maple-home": "switcher kind, switched prices"}
DEFAULT_MAP = "/home/detective/src/integrations/market-detective-mcp/data/name-map.json"
PSEUDONYM = re.compile(r"(?:상점|구매자|소유자|참가자)-[0-9A-Fa-f]{4,}")


def pairs(node, out):
    """Every string-to-string pair in the map, in both directions, whatever its shape."""
    if isinstance(node, dict):
        for k, v in node.items():
            if isinstance(v, str):
                out[k], out[v] = v, k
            else:
                pairs(v, out)
    elif isinstance(node, list):
        for x in node:
            pairs(x, out)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("report")
    ap.add_argument("--map", default=DEFAULT_MAP)
    a = ap.parse_args()
    report = json.loads(Path(a.report).read_text(encoding="utf-8"))
    names = pairs(json.loads(Path(a.map).read_text(encoding="utf-8")), {})
    real = lambda s: names.get(s, s)
    reveal = lambda text: PSEUDONYM.sub(lambda m: f"{m.group(0)}({names[m.group(0)]})" if m.group(0) in names else m.group(0), text)
    accused = {}
    for s in report.get("suspects", []):
        name = real(str(s.get("shop")))
        accused.setdefault(name, []).append(s)
    print("# 채점 결과\n\n| 심은 상점 | 심은 수법 | 수사관 판단 | 신뢰도 |\n|---|---|---|---|")
    caught = 0
    for shop, kind in KEY.items():
        hit = accused.get(shop)
        caught += bool(hit)
        print(f"| {shop} | {kind} | {('적발: ' + str(hit[0].get('kind'))) if hit else '놓침'} | {hit[0].get('confidence') if hit else '-'} |")
    earlier = {k: v for k, v in accused.items() if k in EARLIER}
    false_alarms = {k: v for k, v in accused.items() if k not in KEY and k not in PLANTED_BUYERS and k not in EARLIER}
    print(f"\n- 적발: {caught}/{len(KEY)}\n- 스크립트 시절의 실제 부정 적발: {', '.join(f'{k} ({EARLIER[k]})' for k in earlier) or '없음'}\n- 정직한 상점 오탐: {len(false_alarms)}")
    for name, rows in false_alarms.items():
        print(f"  - {name}: {str(rows[0].get('kind'))} ({str(rows[0].get('why'))[:160]})")
    text = report.get("report", "")
    print(f"- 소요 시간: {report.get('seconds')}초, 기록한 사건: {len(report.get('findings', []))}건")
    print("\n# 수사관 보고서 (실명 공개)\n")
    print(reveal(text))


if __name__ == "__main__":
    main()
