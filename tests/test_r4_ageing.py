"""Round 4 stock ageing in market/server.py, as pure functions: grades lost in stock, the storage share, book worth.
Exit code: the number of failed checks."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "market"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "publish" / "scripts"))
import server as S   # noqa: E402

FAILS = []


def check(name, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + name + (f"  {detail}" if not ok else ""))
    if not ok:
        FAILS.append(name)


H = 3600
check("goods keep their grade inside the first stretch", S.aged_grade("A", 0, 47 * H, 48) == "A")
check("one grade is lost per stretch", S.aged_grade("A", 0, 48 * H, 48) == "B" and S.aged_grade("A", 0, 100 * H, 48) == "C"
      and S.aged_grade("B", 0, 50 * H, 48) == "C")
check("C is the floor", S.aged_grade("A", 0, 1000 * H, 48) == "C" and S.aged_grade("C", 0, 1000 * H, 48) == "C")
check("0 turns ageing off", S.aged_grade("A", 0, 1000 * H, 0) == "A")
check("ageing and the higher storage share start in Round 4", not S.ageing(3) and S.ageing(4) and S.storage_rate(3) == 0.01
      and S.storage_rate(4) == S.STORAGE_PER_DAY_AGED == 0.12 and S.storage_every(3) == 6 * 3600 and S.storage_every(4) == 3600)
goods = {"kind": "goods", "grade": "A", "cost": 100000, "supplied_at": 0}
food = {"kind": "food", "grade": None, "cost": 100000, "supplied_at": 0}
check("fresh stock counts at cost", S.stock_worth(goods, 10 * H, 4) == 100000)
check("stock that fell from A to C counts for about half", 50000 <= S.stock_worth(goods, 100 * H, 4) <= 56000, S.stock_worth(goods, 100 * H, 4))
check("food counts at cost until it expires, and Round 3 is unchanged", S.stock_worth(food, 100 * H, 4) == 100000
      and S.stock_worth(goods, 100 * H, 3) == 100000)
u = dict(goods, supplied_at=S.time.time() - 60 * H)
S.M = type("M", (), {"round": lambda self: 4})()
check("grade_now gives the aged grade in Round 4", S.grade_now(u) == "B")
S.M = type("M", (), {"round": lambda self: 3})()
check("and the supply grade in Round 3", S.grade_now(u) == "A")
print(f"{len(FAILS)} failed")
sys.exit(len(FAILS))
