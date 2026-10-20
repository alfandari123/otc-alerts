""""ATM stocks" (מניות כספומט) - the agent's second strategy, Reuven's way. Virtual money only for now.

- Once a day (outside market hours) scan the most active OTC stocks under $0.01 for a clean floor/ceiling range
  of 2-3 months (bot/atm.py) and send the list.
- During the session a buy order waits AT the floor and a sell order AT the ceiling. Sub-penny prices queue on the
  same tick, so an order counts as filled only after QUEUE_X times our size traded at that price (or the price went
  through it).
- No fixed % target: sell AT the ceiling. With fresh strong news, do not sell at the ceiling - let it run with a
  trailing stop.
- The floor breaks -> find out why: dilution (a dilution filing / share-count notice / bad news, or the floor
  breaking again day after day on heavy volume) -> sell at once. One seller dumping at market (a burst that closes
  back on/above the floor) -> hold, and buy more at the cheaper price once (no more than the first buy).
- Own budget: at most ATM_BUDGET_PCT of the portfolio, ATM_MAX_POS stocks, each <= 2% of the daily volume.
Positions live in pt["atm"]["pos"] (kind "atm"); cash, fees and closed trades are shared with the news strategy.
"""
import time
from datetime import datetime

from . import atm, config, net, trader
from .sources import otcm
from .util import ET, esc, et_now, fmt_price, log

DAILY_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{}?interval=1d&range=1y"


def _book(st):
    pt = trader.init(st)
    a = pt.setdefault("atm", {})
    for k, v in (("pos", {}), ("ranges", {}), ("scan_ts", 0), ("since", {}), ("cool", {})):
        a.setdefault(k, v)
    return pt, a


def invested(st):
    _, a = _book(st)
    return sum(p["shares"] * p.get("last", p["entry"]) for p in a["pos"].values())


# ---------- daily scan ----------

def _daily(sym):
    r = net.get(DAILY_URL.format(sym), timeout=20).json()["chart"]["result"][0]
    q = r["indicators"]["quote"][0]
    out = []
    for i, t in enumerate(r.get("timestamp") or []):
        o, h, l, c, v = (q[k][i] for k in ("open", "high", "low", "close", "volume"))
        if None not in (o, h, l, c):
            out.append({"t": t, "o": round(o, 6), "h": round(h, 6), "l": round(l, 6), "c": round(c, 6), "v": v or 0})
    return out


def scan(run, force=False):
    st = run.st
    pt, a = _book(st)
    now = et_now()
    m = now.hour * 60 + now.minute
    quiet = now.weekday() >= 5 or m < 9 * 60 or m >= 16 * 60 + 30
    if not force and (not quiet or time.time() - a["scan_ts"] < config.ATM_SCAN_EVERY_H * 3600):
        return
    a["scan_ts"] = time.time()
    rows = []
    for page in range(1, config.ATM_SCAN_PAGES + 1):
        try:
            got = otcm._get("/otcapi/market-data/active/current", page=page, pageSize=50, sortOn="volume").json()
        except Exception as e:
            log(f"atm: active list failed ({net.short_err(e)})")
            break
        recs = got.get("records") or []
        if not recs:
            break
        rows += recs
    info = {(r.get("symbol") or "").upper(): r for r in rows if 0 < (r.get("price") or 0) <= config.MAX_PRICE}
    for s in st.get("watch", []):
        info.setdefault(s, {})
    found = {}
    for sym, r in info.items():
        if r.get("isCaveatEmptor") or "expert" in (r.get("tierName") or "").lower():
            continue
        try:
            bars = _daily(sym)
        except Exception:
            continue
        rng = atm.find_range(bars)
        if not rng:
            continue
        avg_v = sum(b["v"] for b in bars[-atm.WINDOW:]) / atm.WINDOW
        found[sym] = {"floor": rng["floor"], "ceiling": rng["ceiling"], "last": bars[-1]["c"],
                      "max": round(avg_v * config.TRADE_MAX_VOL_SHARE * rng["floor"], 2), "swings": rng["swings"],
                      "touches": f"{rng['touches_f']}/{rng['touches_c']}", "tier": r.get("tierName") or ""}
    a["ranges"] = found
    log(f"atm: scan found {len(found)} ranges in {len(info)} stocks")
    if found:
        lines = ["🏧 <b>סריקת מניות כספומט</b> – בתעלה עכשיו:"]
        for s, x in sorted(found.items(), key=lambda kv: -kv[1]["max"]):
            lines.append(f"• <b>{s}</b> רצפה ${fmt_price(x['floor'])} ↔ תקרה ${fmt_price(x['ceiling'])} · "
                         f"עכשיו ${fmt_price(x['last'])} · עד ${x['max']:,.0f}")
        lines.append("הסוכן יקנה ברצפה וימכור בתקרה, אם אין סימני דילול.")
        run.notify("\n".join(lines))


# ---------- fills ----------

def _traded_at(bars, level, side):
    """Volume that traded at our price (buy: at/below it, sell: at/above it) and whether it went through it."""
    if side == "buy":
        vol = sum(b["v"] for b in bars if b["l"] <= level + 1e-9)
        through = any(b["l"] < level - 1e-9 for b in bars)
    else:
        vol = sum(b["v"] for b in bars if b["h"] >= level - 1e-9)
        through = any(b["h"] > level + 1e-9 for b in bars)
    return vol, through


def _filled(bars, level, shares, side):
    vol, through = _traded_at(bars, level, side)
    return through or vol >= config.ATM_QUEUE_X * shares


# ---------- trading ----------

def _budget(st, rng):
    pt, a = _book(st)
    room = trader.equity(pt) * config.ATM_BUDGET_PCT - invested(st)
    return max(0.0, min(config.ATM_MAX_TRADE, rng["max"], room, pt["cash"] - config.TRADE_FEE))


def _buy(run, sym, rng, price, shares, add=False):
    st = run.st
    pt, a = _book(st)
    cost = shares * price
    pt["cash"] -= cost + config.TRADE_FEE
    pt["fees"] += config.TRADE_FEE
    if add:
        p = a["pos"][sym]
        p["basis"] += cost + config.TRADE_FEE
        p["entry"] = round((p["entry"] * p["shares"] + cost) / (p["shares"] + shares), 6)
        p["shares"] += shares
        p["added"] = True
        run.notify(f"🏧 <b>כספומט – קנייה נוספת: {sym}</b>\n"
                   f"מוכר זרק מתחת לרצפה בלי סימני דילול – קונה עוד {shares:,} ב-${fmt_price(price)} (${cost:,.0f})\n"
                   f"ממוצע חדש ${fmt_price(p['entry'])} · מוכר בתקרה ${fmt_price(p['ceiling'])}")
        return
    a["pos"][sym] = {"kind": "atm", "shares": shares, "shares0": shares, "entry": price, "floor": rng["floor"],
                     "ceiling": rng["ceiling"], "last": price, "high": price, "basis": cost + config.TRADE_FEE,
                     "opened": time.time(), "break_days": [], "added": False, "trail": None}
    gain = shares * (rng["ceiling"] - price) - 2 * config.TRADE_FEE
    run.notify(f"🏧 <b>כספומט – קנייה וירטואלית: {sym}</b>\n"
               f"{shares:,} מניות × ${fmt_price(price)} = ${cost:,.0f} (ברצפה)\n"
               f"פקודת מכירה מחכה בתקרה ${fmt_price(rng['ceiling'])} → רווח אפשרי ${gain:,.0f} "
               f"(+{(rng['ceiling'] / price - 1) * 100:.0f}%) אחרי עמלות\n"
               "אין עצירה לפי מחיר: אם הרצפה נשברת – בודק אם זה דילול (יוצא מיד) או מוכר אחד (מחזיק/קונה עוד).")
    log("atm: bought")


def _sell(run, sym, price, why):
    st = run.st
    pt, a = _book(st)
    p = a["pos"].pop(sym)
    price = round(max(price, trader.tick(price)), 6)
    a["since"][sym] = time.time()
    proceeds = p["shares"] * price - config.TRADE_FEE
    pnl = proceeds - p["basis"]
    pt["cash"] += proceeds
    pt["fees"] += config.TRADE_FEE
    pt["closed"].append({"sym": sym, "entry": p["entry"], "exit": price, "shares": p["shares"], "pnl": round(pnl, 2),
                         "r": 0.0, "opened": p["opened"], "closed": time.time(), "why": "🏧 " + why, "part": False,
                         "kind": "atm"})
    pt["closed"] = pt["closed"][-300:]
    if "floor" in why or "דילול" in why:
        a["cool"][sym] = time.time()
    icon = "✅" if pnl > 0.005 else ("⚪" if pnl > -0.005 else "🔻")
    run.notify(f"{icon} <b>כספומט – מכירה וירטואלית: {sym}</b>\n{esc(why)}\n"
               f"קנייה ${fmt_price(p['entry'])} ← מכירה ${fmt_price(price)} ({(price / p['entry'] - 1) * 100:+.0f}%)\n"
               f"רווח/הפסד אחרי עמלות: <b>{'+' if pnl >= 0 else '−'}${abs(pnl):,.2f}</b> · "
               f"שווי התיק: ${trader.equity(pt):,.0f}")
    log("atm: sold")


def _dilution(st, sym, since):
    """Hebrew reason when the fall looks like dilution, else ''."""
    pt, _ = _book(st)
    if pt["dil"].get(sym, 0) > since:
        return pt["dilw"].get(sym) or "דיווח על דילול / הנפקה"
    neg = pt["neg"].get(sym)
    if neg and neg[0] > since:
        return f"חדשה שלילית: {neg[1]}"
    return trader._danger(st, sym, since=since)


def _manage(run, sym, p, now):
    st = run.st
    bars, _ = trader.get_bars(sym)
    if not bars:
        return
    mine = [b for b in bars if b["t"] >= p["opened"] - 60]      # trades since our buy (the order queue starts then)
    last = bars[-1]["c"]
    p["last"] = last
    p["high"] = max(p["high"], max(b["h"] for b in bars))
    t = trader.tick(last)
    # 1) dilution at any time -> out
    why = _dilution(st, sym, p["opened"])
    if why:
        return _sell(run, sym, last - t if last > t else last, f"זוהה דילול – יציאה מיידית: {why}")
    # 2) the ceiling (or news mode: trailing)
    news = p.get("news") or trader.init(st)["pos"].get(sym, {}).get("news")
    fresh_news = news and time.time() - news[0] < 3 * 86400
    if p["trail"]:
        p["trail"] = max(p["trail"], trader._grid(p["high"] * (1 - config.ATM_NEWS_TRAIL), p["high"], False))
        if last <= p["trail"]:
            return _sell(run, sym, last - t if last > t else last, "חדשה הריצה את המניה מעל התקרה – העצירה הנגררת נפגעה, הרווח נשמר")
    elif mine and _filled(mine, p["ceiling"], p["shares"], "sell"):
        if fresh_news:
            p["trail"] = trader._grid(max(p["ceiling"] - t, p["high"] * (1 - config.ATM_NEWS_TRAIL)), p["ceiling"], False)
            trader._note(run, sym, "atmnews", f"🏧 <b>{sym}</b> הגיעה לתקרה, אבל יש חדשה חיובית טרייה – לא מוכר בתקרה; "
                                              f"עצירה נגררת ב-${fmt_price(p['trail'])}")
        else:
            return _sell(run, sym, p["ceiling"], "🎯 הגיעה לתקרה – מכירה בהתנגדות")
    # 3) the floor broke: why?
    floor = p["floor"]
    day = str(now.date())
    if last < floor - 1e-9:
        before = bars[:-15]
        normal = sum(b["v"] for b in before) / len(before) * 15 if len(before) >= 15 else 0
        burst = bool(normal) and sum(b["v"] for b in bars[-15:]) >= 5 * normal
        if not p["added"] and burst and config.ATM_ADD_ON:
            add_px = trader._grid(last, last, False)
            shares = int(min(p["shares0"] * add_px, _budget(st, {"max": config.ATM_MAX_TRADE})) / add_px) if add_px else 0
            if shares * add_px >= config.TRADE_MIN_DOLLARS / 3:
                _buy(run, sym, None, add_px, shares, add=True)
        trader._note(run, sym, "atmbreak", f"🏧 <b>{sym}</b> ירדה מתחת לרצפה ${fmt_price(floor)} – אין סימני דילול "
                                            f"({'מוכר אחד זרק במחיר שוק' if burst else 'ירידה רגילה'}); מחזיק. "
                                            "אם זה יחזור יום אחרי יום בווליום כבד – אצא.")
    if now.hour * 60 + now.minute >= 15 * 60 + 55 and day not in p["break_days"]:
        # end of day: a close under the floor on heavy volume counts; two such days in a row = a diluter at work
        avg_v = sum(b["v"] for b in bars) / max(1, len(bars))
        if last < floor - 1e-9 and sum(b["v"] for b in bars) >= config.ATM_HEAVY_X * avg_v * len(bars) / 2:
            p["break_days"].append(day)
            p["break_days"] = p["break_days"][-5:]
            if len(p["break_days"]) >= 2:
                return _sell(run, sym, last - t if last > t else last,
                             "הרצפה נשברת שוב ושוב בווליום כבד – כנראה מדלל שמוכר; יציאה")
    if time.time() - p["opened"] > config.ATM_MAX_DAYS * 86400:
        return _sell(run, sym, last - t if last > t else last, f"מוחזקת יותר מ-{config.ATM_MAX_DAYS} יום בלי שהגיעה לתקרה")


def _entries(run, now):
    st = run.st
    pt, a = _book(st)
    if pt["paused"] or not (9 * 60 + 30 <= now.hour * 60 + now.minute <= 15 * 60 + 45):
        return
    for sym, rng in a["ranges"].items():
        if sym in a["pos"] or sym in pt["pos"] or len(a["pos"]) >= config.ATM_MAX_POS:
            continue
        if time.time() - a["cool"].get(sym, 0) < config.ATM_COOLDOWN_DAYS * 86400:
            continue
        bars, _ = trader.get_bars(sym)
        bars = [b for b in bars if b["t"] > a["since"].get(sym, 0)]
        if not bars or min(b["l"] for b in bars) > rng["floor"] + 1e-9:
            continue                                  # the price did not come down to the floor (since our order)
        if bars[-1]["c"] < rng["floor"] - 1e-9:
            continue                                  # it is under the floor right now: wait, do not catch a fall
        if trader._danger(st, sym):
            continue
        budget = _budget(st, rng)
        shares = int(budget / rng["floor"])
        if shares * rng["floor"] < config.ATM_MIN_TRADE:
            continue
        if shares * (rng["ceiling"] - rng["floor"]) - 2 * config.TRADE_FEE <= 0:
            continue
        if not _filled(bars, rng["floor"], shares, "buy"):
            continue
        _buy(run, sym, rng, rng["floor"], shares)


def cycle(run):
    if not (config.TRADE_ON and config.ATM_ON) or trader.live():
        return
    now = et_now()
    try:
        scan(run)
    except Exception as e:
        log(f"atm: scan failed ({net.short_err(e)})")
    if trader._session(now) != "open":
        return
    _, a = _book(run.st)
    for sym, p in list(a["pos"].items()):
        try:
            _manage(run, sym, p, now)
        except Exception as e:
            log(f"atm: manage failed ({net.short_err(e)})")
    try:
        _entries(run, now)
    except Exception as e:
        log(f"atm: entries failed ({net.short_err(e)})")


def dashboard_rows(st):
    _, a = _book(st)
    pos = [{"sym": s, "kind": "atm", "shares": p["shares"], "shares0": p["shares0"], "entry": p["entry"],
            "last": p.get("last"), "stop": p["floor"], "target": p["ceiling"], "high": p["high"],
            "opened": p["opened"], "cat": "🏧 כספומט", "score": 0, "runner": bool(p["trail"]), "be": False,
            "ref": p["floor"], "basis": round(p["basis"], 2), "title": "", "url": "", "bars": trader.chart_bars(s)}
           for s, p in a["pos"].items()]
    ranges = [{"sym": s, **x} for s, x in a["ranges"].items()]
    return pos, ranges
