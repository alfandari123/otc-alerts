"""Scan the market for "ATM stocks" (bot/atm.py): sub-penny stocks bouncing between the same floor and ceiling.

    python tools/atm_scan.py [pages]

For every stock under $0.01 among the most active OTC stocks today: the range now, how the pattern did on it in
the last year (conservative fills, tools/atm_ticks_backtest.py), where the price is vs the floor, how much money the
stock can take (2% of its average daily volume) and the warning signs OTC Markets shows (Caveat Emptor, tier).
Prints a table and writes atm_scan_result.json.
"""
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from bot import atm  # noqa: E402
from bot.sources import otcm  # noqa: E402
from atm_backtest import daily  # noqa: E402
from atm_ticks_backtest import FEE, simulate  # noqa: E402


def main():
    pages = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    rows = []
    for p in range(1, pages + 1):
        try:
            got = otcm._get("/otcapi/market-data/active/current", page=p, pageSize=50, sortOn="volume").json()
        except Exception:
            break
        recs = got.get("records") or []
        if not recs:
            break
        rows += recs
    info = {(r.get("symbol") or "").upper(): r for r in rows if 0 < (r.get("price") or 0) <= 0.01}
    print(f"{len(rows)} active stocks, {len(info)} under $0.01")
    found = []
    for sym, r in sorted(info.items()):
        try:
            bars = daily(sym)
        except Exception:
            continue
        rng = atm.find_range(bars)
        if not rng:
            continue
        trades = simulate(sym, bars, True)
        closed = [t for t in trades if t["why"] != "still open"]
        avg_v = sum(b["v"] for b in bars[-60:]) / 60
        max_dollars = avg_v * 0.02 * rng["floor"]
        last = bars[-1]["c"]
        round_gain = None
        p = atm.plan_ticks(rng, FEE, min(500, max_dollars))
        if p:
            round_gain = p["gain"]
        found.append({"sym": sym, "floor": rng["floor"], "ceiling": rng["ceiling"], "last": last,
                      "at_floor": last <= rng["floor"] + 1e-9, "touches": f"{rng['touches_f']}/{rng['touches_c']}",
                      "swings": rng["swings"], "rounds_won": sum(1 for t in closed if t["pnl"] > 0),
                      "rounds_lost": sum(1 for t in closed if t["pnl"] <= 0),
                      "pnl_1y": round(sum(t["pnl"] for t in trades), 2), "max_dollars": round(max_dollars),
                      "round_gain": round(round_gain, 2) if round_gain else None,
                      "caveat": bool(r.get("isCaveatEmptor")), "tier": r.get("tierName") or "",
                      "today_vol": r.get("shareVolume"), "today_dollars": r.get("dollarVolume")})
        time.sleep(0.25)
    found.sort(key=lambda x: (not x["at_floor"], -x["rounds_won"], x["rounds_lost"]))
    print(f"\n{len(found)} in a range now:")
    for x in found:
        print(f"  {x['sym']:6} floor {x['floor']:<7} ceiling {x['ceiling']:<7} last {x['last']:<8} "
              f"{'AT FLOOR' if x['at_floor'] else '        '} touches {x['touches']:>6} swings {x['swings']:>4} "
              f"| 1y rounds won {x['rounds_won']:>2} lost {x['rounds_lost']:>2} P&L ${x['pnl_1y']:>8,.2f} "
              f"| max ${x['max_dollars']:>6,} round ${x['round_gain'] or 0:>7,.2f} | {x['tier']}"
              f"{' | CAVEAT EMPTOR' if x['caveat'] else ''}")
    (ROOT / "atm_scan_result.json").write_text(json.dumps(found, indent=1))


if __name__ == "__main__":
    main()
