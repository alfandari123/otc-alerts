"""Course claim check (mil-76 "gappers"): a day with far-above-average volume, a big rise and a close near the high
opens higher the next morning -> buy near the close, sell at the open. One year of Yahoo daily bars of today's most
active OTC stocks (survivor-biased universe, like the ATM backtests).

    python tools/gap_test.py [pages=20] [max_price=0.20]
"""
import statistics as st
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from bot.sources import otcm  # noqa: E402
from atm_backtest import daily  # noqa: E402

FEE = 6.95
SIZE = 300.0     # dollars per trade (1–2% of a small account)


def events(bars):
    """(kind, gap, day move, volume x) for every big up day: kind 'strong' = closed near the high, 'weak' = faded."""
    out = []
    for d in range(21, len(bars) - 1):
        b, prev, nxt = bars[d], bars[d - 1], bars[d + 1]
        avg = sum(x["v"] for x in bars[d - 20:d]) / 20
        if not avg or not prev["c"] or b["v"] * b["c"] < 10_000:      # needs some money traded that day
            continue
        vx, move = b["v"] / avg, b["c"] / prev["c"] - 1
        if vx < 5 or move < 0.40:
            continue
        kind = "strong" if b["c"] >= 0.9 * b["h"] else "weak" if b["c"] <= 0.75 * b["h"] else ""
        if kind:
            out.append((kind, nxt["o"] / b["c"] - 1, move, vx))
    return out


def main(pages=20, max_price=0.20):
    rows = otcm.volume_leaders(pages=pages)
    syms = sorted({(r.get("symbol") or "").upper() for r in rows if 0 < (r.get("price") or 0) <= max_price} - {""})
    print(f"{len(syms)} tickers", flush=True)
    ev = []
    for s in syms:
        try:
            got = events(daily(s))
        except Exception:
            continue
        for e in got:
            print(f"{s:6} {e[0]:6} day {e[2]:+.0%} vol {e[3]:.0f}x -> open {e[1]:+.0%}", flush=True)
        ev += got
        time.sleep(0.3)
    print()
    for kind in ("strong", "weak"):
        g = [e[1] for e in ev if e[0] == kind]
        if not g:
            continue
        pnl = sum(SIZE * x - 2 * FEE for x in g)
        print(f"{kind:6} n={len(g):3}  next open median {st.median(g):+.0%}  mean {st.mean(g):+.0%}  "
              f"gap up {sum(x > 0 for x in g) / len(g):.0%}  gap >= +5% {sum(x >= 0.05 for x in g) / len(g):.0%}  "
              f"gap down {sum(x < 0 for x in g) / len(g):.0%}  | ${SIZE:.0f}/trade after fees: ${pnl:+,.0f}")
    print("strong = closed >= 90% of the day's high; weak = closed <= 75% of it; day >= +40% on >= 5x volume")


if __name__ == "__main__":
    a = sys.argv[1:]
    main(int(a[0]) if a else 20, float(a[1]) if len(a) > 1 else 0.20)
