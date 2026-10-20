"""Backtest of Reuven's "ATM stock" way (bot/atm.py plan_ticks) on one year of daily bars.

    python tools/atm_ticks_backtest.py            # most active OTC stocks today under $0.01
    python tools/atm_ticks_backtest.py AAA BBB

A buy order waits AT the floor and a sell order AT the ceiling (sub-penny traders queue on the bid/ask). There is
no price stop: the trade ends at the ceiling, or when the floor is gone for good (FLOOR_GONE closes in a row under
it), or after MAX_DAYS. Dilution cannot be seen in daily prices, so it is NOT checked here - this measures the
price pattern only. Two fill assumptions are reported:
  optimistic   - the order fills whenever the price trades at our level;
  conservative - the sell needs the price to trade at the ceiling on 2 different days (or above it), because at
                 sub-penny prices there is a long queue of sellers on the same tick.
"""
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot import atm  # noqa: E402
from bot.sources import otcm  # noqa: E402
from atm_backtest import daily  # noqa: E402

FEE = 6.95
BUDGET = 500.0
MAX_DAYS = 60
FLOOR_GONE = 10


def simulate(sym, bars, conservative):
    trades, d = [], atm.WINDOW
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
        exit_px, why, j, hits, below = None, "", d, 0, 0
        while exit_px is None:
            j += 1
            if j >= len(bars):
                exit_px, why, j = bars[-1]["c"], "still open", len(bars) - 1
                break
            x = bars[j]
            if x["h"] >= c - 1e-9:
                hits += 1
                if not conservative or hits >= 2 or x["h"] > c + 1e-9:
                    exit_px, why = c, "ceiling"
                    continue
            below = below + 1 if x["c"] < f - 1e-9 else 0
            if below >= FLOOR_GONE:
                exit_px, why = x["c"], "floor gone"
            elif j - d >= MAX_DAYS:
                exit_px, why = x["c"], "time"
        pnl = shares * (exit_px - f) - 2 * FEE
        trades.append({"sym": sym, "in": datetime.fromtimestamp(b["t"], timezone.utc).date().isoformat(),
                       "out": datetime.fromtimestamp(bars[j]["t"], timezone.utc).date().isoformat(), "floor": f,
                       "ceiling": c, "exit": exit_px, "shares": shares, "cost": round(shares * f, 2),
                       "pnl": round(pnl, 2), "why": why, "days": j - d})
        d = j + 1
    return trades


def report(name, trades):
    closed = [t for t in trades if t["why"] != "still open"]
    wins = [t for t in closed if t["pnl"] > 0]
    print(f"\n== {name}: {len(trades)} trades, closed {len(closed)}, wins {len(wins)}, "
          f"P&L closed ${sum(t['pnl'] for t in closed):,.2f}, with open ${sum(t['pnl'] for t in trades):,.2f}")
    for t in trades:
        print(f"  {t['sym']:6} {t['in']}->{t['out']} {t['days']:>2}d  floor {t['floor']} ceiling {t['ceiling']} "
              f"exit {t['exit']}  cost ${t['cost']:,.0f}  {t['pnl']:+8.2f}  {t['why']}")


def main():
    syms = [s.upper() for s in sys.argv[1:]]
    if not syms:
        rows = otcm.volume_leaders(pages=8)
        syms = sorted({(r.get("symbol") or "").upper() for r in rows if 0 < (r.get("price") or 0) <= 0.01} - {""})
    print(f"{len(syms)} tickers")
    opt, cons, now = [], [], []
    for s in syms:
        try:
            bars = daily(s)
        except Exception as e:
            print(f"{s}: no data ({type(e).__name__})")
            continue
        opt += simulate(s, bars, False)
        cons += simulate(s, bars, True)
        r = atm.find_range(bars)
        if r:
            now.append((s, r, bars[-1]["c"]))
        time.sleep(0.3)
    report("optimistic fills", opt)
    report("conservative fills", cons)
    print("\nin a range today:")
    for s, r, last in now:
        print(f"  {s:6} floor {r['floor']} ceiling {r['ceiling']} last {last} touches {r['touches_f']}/"
              f"{r['touches_c']} swings {r['swings']} ${r['dollars']:,.0f}/day")


if __name__ == "__main__":
    main()
