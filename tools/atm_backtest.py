"""Backtest of the "ATM stock" method (bot/atm.py) on one year of daily bars.

    python tools/atm_backtest.py            # most active OTC stocks today, priced under $0.01
    python tools/atm_backtest.py AAA BBB    # chosen tickers

Every day the range is found from the 60 trading days BEFORE that day only (no peeking). Fills are conservative:
buy one tick above the floor when the day's low reaches it; if the same day also falls through the stop, the
trade counts as stopped out; a stop gapped through fills at the open minus a tick; the target counts only when
the day's high is a tick above it. $500 per trade, at most 2% of the average daily volume, $6.95 per order.
"""
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot import atm, net  # noqa: E402
from bot.sources import otcm  # noqa: E402

URL = "https://query1.finance.yahoo.com/v8/finance/chart/{}?interval=1d&range=1y"
FEE = 6.95
BUDGET = 500.0
MAX_DAYS = 40
RR = float(__import__("os").environ.get("ATM_RR", "3"))   # reward/risk needed after fees
COOLDOWN = 20        # trading days without re-entry after the floor broke


def daily(sym):
    r = net.get(URL.format(sym), timeout=20).json()["chart"]["result"][0]
    q = r["indicators"]["quote"][0]
    out = []
    for i, t in enumerate(r.get("timestamp") or []):
        o, h, l, c, v = (q[k][i] for k in ("open", "high", "low", "close", "volume"))
        if None in (o, h, l, c):
            continue
        out.append({"t": t, "o": round(o, 6), "h": round(h, 6), "l": round(l, 6), "c": round(c, 6), "v": v or 0})
    return out


def simulate(sym, bars):
    trades, d = [], atm.WINDOW
    while d + 1 < len(bars):
        rng = atm.find_range(bars[:d])
        sig = bars[d]
        # signal: the day tested the floor and HELD (closed at/above it, never broke the stop); buy next open
        if not rng:
            d += 1
            continue
        f, t = rng["floor"], atm.tick(rng["floor"])
        if not (sig["l"] <= f + t and sig["l"] > f - t and sig["c"] >= f):
            d += 1
            continue
        avg_v = sum(x["v"] for x in bars[d - atm.WINDOW:d]) / atm.WINDOW
        p = atm.plan(rng, FEE, min(BUDGET, avg_v * 0.02 * f), rr=RR)
        b = bars[d + 1]
        zone_top = f + (rng["ceiling"] - f) * 0.25
        if not p or b["o"] > zone_top or b["o"] <= p["stop"]:
            d += 1
            continue
        d += 1
        entry = atm._grid(b["o"] + t, True)                       # next open, one tick worse
        shares = int(min(BUDGET, avg_v * 0.02 * f) / entry)
        exit_px, why, j = None, "", d
        if b["l"] <= p["stop"]:                                   # same day through the stop: assume the worst
            exit_px, why = p["stop_fill"], "stop (same day)"
        while exit_px is None:
            j += 1
            if j >= len(bars):
                exit_px, why, j = bars[-1]["c"], "still open", len(bars) - 1
                break
            x = bars[j]
            if x["l"] <= p["stop"]:
                exit_px, why = max(min(p["stop"], x["o"]) - atm.tick(x["o"]), atm.tick(x["o"])), "stop"
            elif x["h"] >= p["target"] + atm.tick(p["target"]):
                exit_px, why = p["target"], "target"
            elif j - d >= MAX_DAYS:
                exit_px, why = x["c"], "time"
        pnl = shares * (exit_px - entry) - 2 * FEE
        trades.append({"sym": sym, "in": datetime.fromtimestamp(b["t"], timezone.utc).date().isoformat(),
                       "out": datetime.fromtimestamp(bars[j]["t"], timezone.utc).date().isoformat(),
                       "floor": rng["floor"], "ceiling": rng["ceiling"], "entry": entry, "exit": exit_px,
                       "shares": shares, "pnl": round(pnl, 2), "why": why, "days": j - d})
        d = j + 1 + (COOLDOWN if why.startswith("stop") else 0)   # a broken floor ends the pattern for a while
    return trades


def main():
    syms = [s.upper() for s in sys.argv[1:]]
    if not syms:
        rows = otcm.volume_leaders(pages=8)
        syms = sorted({(r.get("symbol") or "").upper() for r in rows if 0 < (r.get("price") or 0) <= 0.01} - {""})
    print(f"{len(syms)} tickers")
    all_trades, ranges_now = [], []
    for s in syms:
        try:
            bars = daily(s)
        except Exception as e:
            print(f"{s}: no data ({type(e).__name__})")
            continue
        tr = simulate(s, bars)
        all_trades += tr
        now = atm.find_range(bars)
        if now:
            ranges_now.append({"sym": s, **now, "last": bars[-1]["c"]})
        time.sleep(0.3)
    closed = [t for t in all_trades if t["why"] != "still open"]
    wins = [t for t in closed if t["pnl"] > 0]
    print(f"\ntrades: {len(all_trades)} (closed {len(closed)}), wins {len(wins)}, "
          f"P&L ${sum(t['pnl'] for t in all_trades):,.2f} (closed ${sum(t['pnl'] for t in closed):,.2f})")
    for t in all_trades:
        print(f"  {t['sym']:6} {t['in']}→{t['out']} {t['days']:>2}d  F {t['floor']} C {t['ceiling']}  "
              f"{t['entry']}→{t['exit']}  {t['pnl']:+8.2f}  {t['why']}")
    print("\nin a range today:")
    for r in ranges_now:
        print(f"  {r['sym']:6} floor {r['floor']} ceiling {r['ceiling']} last {r['last']} "
              f"touches {r['touches_f']}/{r['touches_c']} swings {r['swings']} ${r['dollars']:,.0f}/day")
    out = Path(__file__).resolve().parent.parent / "atm_backtest_result.json"
    out.write_text(json.dumps({"trades": all_trades, "ranges_now": ranges_now}, indent=1))


if __name__ == "__main__":
    main()
