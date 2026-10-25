"""Course claim check (28d-44): in an ATM range, sell one tick UNDER the ceiling (be the first seller, more rounds a
month) instead of waiting at the ceiling. Same data and fill rules as tools/atm_ticks_backtest.py (conservative).

    python tools/atm_undercut_test.py [pages=8]
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from bot import atm  # noqa: E402
from bot.sources import otcm  # noqa: E402
from atm_backtest import daily  # noqa: E402
from atm_ticks_backtest import BUDGET, FEE, FLOOR_GONE, MAX_DAYS  # noqa: E402


def simulate(bars, under):
    """Conservative fills; the sell waits `under` ticks below the ceiling. Returns (pnl list, open pnl, days)."""
    out, d = [], atm.WINDOW
    while d < len(bars):
        rng = atm.find_range(bars[:d])
        b = bars[d]
        if not rng or b["l"] > rng["floor"] + 1e-9 or not b["v"]:
            d += 1
            continue
        avg_v = sum(x["v"] for x in bars[d - atm.WINDOW:d]) / atm.WINDOW
        p = atm.plan_ticks(rng, FEE, min(BUDGET, avg_v * 0.02 * rng["floor"]))
        if not p:
            d += 1
            continue
        f, c, shares = p["entry"], p["target"], p["shares"]
        for _ in range(under):
            c = round(c - atm.tick(c), 6)
        if c <= f or shares * (c - f) - 2 * FEE <= 0:
            d += 1
            continue
        exit_px, j, hits, below, closed = None, d, 0, 0, True
        while exit_px is None:
            j += 1
            if j >= len(bars):
                exit_px, j, closed = bars[-1]["c"], len(bars) - 1, False
                break
            x = bars[j]
            if x["h"] >= c - 1e-9:
                hits += 1
                if hits >= 2 or x["h"] > c + 1e-9:
                    exit_px = c
                    continue
            below = below + 1 if x["c"] < f - 1e-9 else 0
            if below >= FLOOR_GONE or j - d >= MAX_DAYS:
                exit_px = x["c"]
        out.append((round(shares * (exit_px - f) - 2 * FEE, 2), closed, j - d))
        d = j + 1
    return out


def main(pages=8):
    rows = otcm.volume_leaders(pages=pages)
    syms = sorted({(r.get("symbol") or "").upper() for r in rows if 0 < (r.get("price") or 0) <= 0.01} - {""})
    print(f"{len(syms)} tickers", flush=True)
    res = {0: [], 1: []}
    for s in syms:
        try:
            bars = daily(s)
        except Exception:
            continue
        for k in res:
            res[k] += simulate(bars, k)
        time.sleep(0.3)
    for k, t in res.items():
        closed = [x for x in t if x[1]]
        wins = [x for x in closed if x[0] > 0]
        print(f"sell {'AT the ceiling' if k == 0 else f'{k} tick under it'}: {len(t)} rounds, closed {len(closed)}, "
              f"wins {len(wins)}, P&L closed ${sum(x[0] for x in closed):,.0f}, with open ${sum(x[0] for x in t):,.0f}, "
              f"avg days {sum(x[2] for x in closed) / max(1, len(closed)):.1f}")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 8)
