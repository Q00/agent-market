"""Write a synthetic catalog (catalog.csv) and its category rules (categories.json) next to this file.

    python make_catalog.py            # same output every run

Every brand and product here is made up. To run the market on your own products, replace both files:
  catalog.csv      sku,name,category,list_price[,rank]   (category is a path: top>middle>leaf)
  categories.json  [{"prefix": "...", "kind": "food"|"goods", "shelf_hours": h (food), "demand": w (optional)}]
The longest matching prefix wins, so keep a {"prefix": "", "kind": "goods"} rule as the catch-all.
"""
import csv
import json
import random
from pathlib import Path

BRANDS = ["하늘", "바른", "새벽", "온누리", "푸른", "한결"]

# leaf path -> (price range, variants, shelf hours; None means goods)
SPEC = {
    "간편식사>도시락>도시락": ((4000, 6500), ["제육", "불고기", "돈까스", "고등어", "함박"], 24),
    "간편식사>김밥>삼각김밥": ((1000, 2000), ["참치마요", "불고기", "김치", "명란"], 24),
    "간편식사>샌드위치>샌드위치": ((2500, 4500), ["햄치즈", "에그마요", "치킨", "참치"], 24),
    "신선식품>정육>돼지고기": ((9000, 32000), ["삼겹살 500g", "목살 500g", "앞다리 1kg"], 24),
    "신선식품>과일>제철과일": ((5000, 28000), ["사과 1.5kg", "포도 1kg", "귤 2kg", "감 1.5kg"], 48),
    "베이커리>빵>식빵": ((1500, 5000), ["우유식빵", "통밀식빵", "모닝빵", "단팥빵"], 48),
    "유제품>우유>흰우유": ((1200, 3000), ["200ml", "500ml", "900ml", "저지방 900ml"], 96),
    "유제품>요거트>요거트": ((1000, 3500), ["플레인", "딸기", "블루베리", "그릭"], 96),
    "반찬>김치>포기김치": ((12000, 45000), ["1kg", "3kg", "5kg"], 168),
    "가공식품>라면>봉지라면": ((3000, 7000), ["매운맛 5입", "순한맛 5입", "짜장 4입"], 720),
    "가공식품>과자>스낵": ((1200, 3500), ["감자칩", "새우맛", "옥수수칩", "양파맛"], 720),
    "음료>생수>생수": ((800, 12000), ["500ml", "2L", "2L 6입"], 720),
    "음료>커피>커피음료": ((1500, 3500), ["아메리카노", "라떼", "콜드브루"], 720),
    "생활용품>위생>휴지": ((6000, 28000), ["30롤", "곽티슈 6입", "키친타월 4롤"], None),
    "생활용품>세제>세탁세제": ((5000, 24000), ["1L", "3L", "섬유유연제 2L"], None),
    "문구>필기구>볼펜": ((1000, 9000), ["0.5mm", "0.7mm", "3색", "10입 세트"], None),
    "디지털>액세서리>충전케이블": ((5000, 30000), ["C to C 1m", "C to C 2m", "고속충전기"], None),
    "디지털>음향>이어폰": ((15000, 180000), ["유선", "무선", "넥밴드", "노이즈캔슬링"], None),
    "가구>침구>이불": ((39000, 240000), ["싱글", "퀸", "킹"], None),
    "가구>침대>매트리스": ((180000, 1200000), ["싱글", "퀸", "킹"], None),
    "가전>주방>냉장고": ((600000, 2600000), ["300L", "500L", "800L"], None),
    "가전>생활>세탁기": ((400000, 1800000), ["10kg", "16kg", "24kg"], None),
}


def main():
    rng = random.Random(7)
    rows, rules = [], [{"prefix": "", "kind": "goods"}]
    for cat, ((lo, hi), variants, shelf) in SPEC.items():
        leaf = cat.split(">")[-1]
        for brand in BRANDS:
            for variant in variants:
                price = rng.randrange(lo, hi + 1, 100)
                rows.append({"sku": f"S{len(rows) + 1:05d}", "name": f"{brand} {leaf} {variant}", "category": cat,
                             "list_price": price})
        rules.append({"prefix": cat, "kind": "food", "shelf_hours": shelf} if shelf else {"prefix": cat, "kind": "goods"})
    here = Path(__file__).parent
    with open(here / "catalog.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["sku", "name", "category", "list_price"], lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    (here / "categories.json").write_text(json.dumps(rules, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"{len(rows)} SKUs in {len(SPEC)} categories -> {here}")


if __name__ == "__main__":
    main()
