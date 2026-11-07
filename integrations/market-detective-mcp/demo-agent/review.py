"""실제 정산 결과로 리뷰를 작성합니다 (키트의 review 훅).   python review.py --deal deal.json"""
import argparse
import json
from pathlib import Path

import rules

ap = argparse.ArgumentParser()
ap.add_argument("--deal", required=True)
deal = json.loads(Path(ap.parse_args().deal).read_text(encoding="utf-8"))
print(json.dumps(rules.review_from_deal(deal), ensure_ascii=False), flush=True)
