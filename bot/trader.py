"""Paper-trading agent (virtual money only).

Buys an OTC stock after strong positive news when the price starts to move with volume, only if the
possible gain is at least 3x the possible loss (after the spread). Stays away from active diluters and sells
at once when dilution shows up. Once the gain equals the risk, the stop moves to the entry price (no loss),
then trails behind the high.

Prices are Yahoo 1-minute bars (last trades, no bid/ask). To stay honest every buy is filled one tick ABOVE
the last trade and every sell one tick BELOW it, and a stop that is gapped through fills at the bar's open.
"""
import math
import time
from datetime import datetime

from . import config, dilution, market, net
from .util import ET, esc, et_now, fmt_price, log

BARS_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{}?interval=1m&range=1d"

DIL_LABELS = {"הנפקת מניות (דילול)", "הנפקת Reg A (דילול)", "תשקיף הנפקה (דילול)", "הגדלת מניות מורשות",
              "גיוס / דילול", "התחייבות פיננסית / הלוואה", "ספליט הפוך", "ספליט הפוך (נוספה סיומת D לטיקר)"}
END_OF_DILUTION = "סוף הדילול"

_bars = {}   # per-cycle cache: ticker -> (bars, previous close)


# ---------- helpers ----------

def tick(p):
    return 0.0001 if p < 1 else 0.01


def _grid(x, p, up):
    t = tick(p)
    n = x / t
    n = math.ceil(n - 1e-9) if up else math.floor(n + 1e-9)
    return round(max(n, 1) * t, 4 if t < 0.01 else 2)


def init(st):
    pt = st.get("pt")
    if not pt:
        pt = st["pt"] = {"start": config.TRADE_START_CASH, "cash": config.TRADE_START_CASH, "pos": {},
                         "cands": {}, "closed": [], "dil": {}, "skips": {}, "day_rep": "", "week_rep": ""}
    return pt


def get_bars(sym):
    """Today's 1-minute bars [{t,o,h,l,c,v}] and the previous close; ([], None) when unavailable."""
    if sym in _bars:
        return _bars[sym]
    out, prev = [], None
    try:
        r = net.get(BARS_URL.format(sym), timeout=15).json()["chart"]["result"][0]
        prev = r["meta"].get("chartPreviousClose")
        q = r["indicators"]["quote"][0]
        today = et_now().date()
        for i, t in enumerate(r.get("timestamp") or []):
            o, h, l, c, v = (q[k][i] for k in ("open", "high", "low", "close", "volume"))
            if None in (o, h, l, c) or datetime.fromtimestamp(t, ET).date() != today:
                continue
            out.append({"t": t, "o": round(o, 6), "h": round(h, 6), "l": round(l, 6), "c": round(c, 6),
                        "v": v or 0})
    except Exception as e:
        log(f"trader: bars failed ({net.short_err(e)})")
    _bars[sym] = (out, prev)
    return _bars[sym]


def equity(pt):
    return pt["cash"] + sum(p["shares"] * p.get("last", p["entry"]) for p in pt["pos"].values())


def _session(now):
    """'pre' / 'open' / 'post' / None (weekend)."""
    if now.weekday() >= 5:
        return None
    m = now.hour * 60 + now.minute
    return "pre" if m < 9 * 60 + 30 else ("open" if m < 16 * 60 else "post")


def _mins(now):
    return now.hour * 60 + now.minute


# ---------- hooks called from main.py ----------

def on_news(st, sym, score, cat, it, price):
    """A strong positive news item: the stock becomes a candidate for a buy."""
    if not config.TRADE_ON or not sym:
        return
    pt = init(st)
    if sym in pt["pos"] or sym in pt["cands"]:
        return
    tier = (it.get("tier") or "").lower()
    if "expert" in tier or "grey" in tier:
        return
    pt["cands"][sym] = {"ts": time.time(), "score": score, "cat": cat, "price": price, "ref": None,
                        "deadline": None, "why": "", "title": (it.get("title") or "")[:120],
                        "url": it.get("url", "")}
    log("trader: new candidate")


def on_item(st, it, rule):
    """Any new item: remember tickers with fresh dilution signs (offerings, convertible notes, reverse split...)."""
    if not config.TRADE_ON or not it.get("syms"):
        return
    labels = set(rule["labels"])
    if labels & DIL_LABELS and END_OF_DILUTION not in labels:
        pt = init(st)
        for s in it["syms"]:
            pt["dil"][s] = time.time()


# ---------- buying ----------

def _active_diluter(st, sym):
    """Hebrew reason when the company is diluting now, else ''."""
    pt = st["pt"]
    if time.time() - pt["dil"].get(sym, 0) < config.TRADE_DIL_FLAG_DAYS * 86400:
        return "דיווח טרי על הנפקה / הלוואה להמרה / ספליט הפוך"
    d = dilution.summary(sym, st, str(et_now().date()))
    if d:
        if (d.get("chg3") or 0) >= config.DILUTION_WARN_3M:
            return f"מדלל פעיל: מספר המניות עלה ב-{d['chg3'] * 100:.0f}% ב-3 חודשים"
        if (d.get("chg6") or 0) >= config.DILUTION_WARN_6M:
            return f"מדלל פעיל: מספר המניות עלה ב-{d['chg6'] * 100:.0f}% בחצי שנה"
        if d.get("auth") and d["tso"] / d["auth"] >= config.TRADE_AUTH_USED_MAX:
            return f"ניצלה {d['tso'] / d['auth'] * 100:.0f}% מהמניות המורשות – צפויה הגדלה ודילול"
    return ""


def _plan(c, bars, prev):
    """Entry plan dict, or (None, reason in Hebrew) when the setup is not good (yet)."""
    last = bars[-1]["c"]
    t = tick(last)
    if not config.MIN_PRICE <= last <= config.MAX_PRICE:
        return None, "המחיר מחוץ לטווח"
    if c["ref"] is None:   # price before the news: last close, or the last minute before the news came out
        before = [b for b in bars if b["t"] < c["ts"]]
        c["ref"] = before[-1]["c"] if before else (prev or bars[0]["o"])
    ref = c["ref"]
    if last < ref + t:
        return None, "המחיר עוד לא עולה"
    if last > ref * (1 + config.TRADE_MAX_CHASE):
        return None, f"המניה כבר עלתה {(last / ref - 1) * 100:.0f}% – מחכה לתיקון"
    recent = bars[-config.TRADE_CONFIRM_BARS:]
    if last < recent[0]["c"]:
        return None, "המחיר יורד בדקות האחרונות"
    high = max(b["h"] for b in bars)
    if high >= last * (1 + config.TRADE_DUMP_FROM_HIGH):
        return None, f"ירדה {(1 - last / high) * 100:.0f}% מהשיא של היום – לחץ מכירה"
    entry = _grid(last + t, last, True)
    swing = min(b["l"] for b in bars[-15:])
    stop = _grid(max(ref, swing - t), last, False)
    stop = max(stop, _grid(entry * (1 - config.TRADE_MAX_RISK), last, True))
    if stop > entry - 2 * t:
        stop = _grid(entry - 2 * t, last, False)
    stop_fill = max(stop - t, t)
    risk = entry - stop_fill
    if risk / entry > config.TRADE_MAX_RISK + 1e-9:
        return None, f"הסיכון גדול מדי ({risk / entry * 100:.0f}%) – צעד מחיר גדול מדי במחיר הזה"
    target = _grid(entry + config.TRADE_RR * risk, last, True)
    if target / entry - 1 > config.TRADE_MAX_TARGET:
        return None, f"כדי לקבל 1:3 צריך עלייה של {(target / entry - 1) * 100:.0f}% – לא ריאלי"
    return {"entry": entry, "stop": stop, "target": target, "risk": risk, "ref": ref}, ""


def _buy(run, sym, c):
    st, pt = run.st, run.st["pt"]
    now = et_now()
    if not config.TRADE_ENTRY_FROM <= _mins(now) <= config.TRADE_ENTRY_UNTIL:
        return False
    bars, prev = get_bars(sym)
    if len(bars) < config.TRADE_CONFIRM_BARS:
        c["why"] = "אין מספיק מסחר היום"
        return False
    why = _active_diluter(st, sym)
    if why:
        c["why"], c["drop"] = why, True
        return False
    plan, why = _plan(c, bars, prev)
    if not plan:
        c["why"] = why
        return False
    avg = market.avg_volume(sym, now.date(), st["avgvol"])
    vol = sum(b["v"] for b in bars)
    if avg and vol < config.TRADE_VOL_CONFIRM_X * avg:
        c["why"] = f"אין מספיק ווליום ({vol / avg:.1f} מהממוצע)"
        return False
    if len(pt["pos"]) >= config.TRADE_MAX_POSITIONS:
        c["why"] = "כבר יש מקסימום מניות פתוחות"
        return False
    budget = min(equity(pt) * config.TRADE_POSITION_PCT, pt["cash"])
    budget = min(budget, vol * config.TRADE_MAX_VOL_SHARE * plan["entry"])  # we must be able to get out
    shares = int(budget / plan["entry"])
    if shares * plan["entry"] < config.TRADE_MIN_DOLLARS:
        c["why"] = "אין מספיק מסחר במניה בשביל עסקה בגודל הזה"
        return False
    cost = shares * plan["entry"]
    pt["cash"] -= cost
    d = dilution.summary(sym, st, str(now.date()))
    pt["pos"][sym] = {"shares": shares, "entry": plan["entry"], "stop": plan["stop"], "stop0": plan["stop"],
                      "target": plan["target"], "R": plan["risk"], "high": plan["entry"], "last": bars[-1]["c"],
                      "opened": time.time(), "last_t": bars[-1]["t"], "cat": c["cat"], "score": c["score"],
                      "tso0": d["tso"] if d else None, "tso_chk": time.time(), "be": False}
    gain = (plan["target"] / plan["entry"] - 1) * 100
    loss = plan["risk"] / plan["entry"] * 100
    run.notify(f"🛒 <b>קנייה וירטואלית: {sym}</b>\n"
               f"{esc(c['cat'])} · ציון {c['score']}/10\n"
               f"{shares:,} מניות × ${fmt_price(plan['entry'])} = ${cost:,.0f} "
               f"({cost / equity(pt) * 100:.0f}% מהתיק)\n"
               f"🎯 יעד: ${fmt_price(plan['target'])} (+{gain:.0f}%)\n"
               f"🛑 עצירה: ${fmt_price(plan['stop'])} (−{loss:.0f}%)\n"
               f"יחס סיכוי-סיכון: 1:{(plan['target'] - plan['entry']) / plan['risk']:.1f}\n"
               f"מחיר לפני החדשה: ${fmt_price(plan['ref'])}\n"
               f"כשהרווח יגיע ל-+{loss:.0f}% העצירה תעלה למחיר הקנייה (בלי הפסד).")
    log("trader: bought")
    return True


# ---------- selling ----------

def _close(run, sym, fill, reason):
    pt = run.st["pt"]
    p = pt["pos"].pop(sym)
    fill = max(fill, tick(fill))
    pnl = p["shares"] * (fill - p["entry"])
    pt["cash"] += p["shares"] * fill
    r_mult = (fill - p["entry"]) / p["R"] if p["R"] else 0
    pt["closed"].append({"sym": sym, "entry": p["entry"], "exit": fill, "shares": p["shares"], "pnl": round(pnl, 2),
                         "r": round(r_mult, 2), "opened": p["opened"], "closed": time.time(), "why": reason})
    pt["closed"] = pt["closed"][-300:]
    pct = (fill / p["entry"] - 1) * 100
    icon = "✅" if pnl > 0.005 else ("⚪" if pnl > -0.005 else "🔻")
    run.notify(f"{icon} <b>מכירה וירטואלית: {sym}</b>\n{esc(reason)}\n"
               f"קנייה ${fmt_price(p['entry'])} ← מכירה ${fmt_price(fill)} ({pct:+.0f}%)\n"
               f"רווח/הפסד: <b>{'+' if pnl >= 0 else '−'}${abs(pnl):,.2f}</b> ({r_mult:+.1f}R)\n"
               f"שווי התיק: ${equity(pt):,.0f}")
    log("trader: sold")


def _manage(run, sym, p):
    st = run.st
    now = et_now()
    bars, _ = get_bars(sym)
    new = [b for b in bars if b["t"] > p["last_t"]]
    for b in new:
        t = tick(b["c"])
        if b["l"] <= p["stop"]:     # checked first: we do not know if the low or the high came first
            fill = min(p["stop"], b["o"]) - t
            why = "העצירה הוזזה למחיר הקנייה ונפגעה – יציאה בלי הפסד" if p["be"] and p["stop"] <= p["entry"] + t \
                else ("עצירה נגררת – נועל רווח" if p["stop"] > p["entry"] else "הגיע לנקודת העצירה")
            if b["o"] < p["stop"]:
                why += " (המחיר קפץ מתחת לעצירה)"
            return _close(run, sym, fill, why)
        if b["h"] >= p["target"] + t:
            return _close(run, sym, p["target"], "🎯 הגיע ליעד הרווח")
        p["high"] = max(p["high"], b["h"])
        p["last"], p["last_t"] = b["c"], b["t"]
        if p["high"] >= p["entry"] + p["R"] and p["stop"] < p["entry"] + t:
            p["stop"], p["be"] = _grid(p["entry"] + t, p["entry"], True), True   # sell fill = entry price
        if p["high"] >= p["entry"] + config.TRADE_TRAIL_AFTER_R * p["R"]:
            p["stop"] = max(p["stop"], _grid(p["high"] - p["R"], p["high"], False))
    if not bars:
        return
    last = bars[-1]["c"]
    sell = last - tick(last)
    # dilution showed up after we bought
    if st["pt"]["dil"].get(sym, 0) > p["opened"]:
        return _close(run, sym, sell, "🔴 זוהה דיווח על דילול / הנפקה – יציאה מיידית")
    if time.time() - p.get("tso_chk", 0) > config.TRADE_TSO_CHECK_MIN * 60:
        p["tso_chk"] = time.time()
        st.get("dil", {}).pop(sym, None)            # force a fresh read
        d = dilution.summary(sym, st, str(now.date()))
        if d and p.get("tso0") and d["tso"] > p["tso0"] * (1 + config.DILUTION_ALERT_MIN):
            return _close(run, sym, sell, f"🔴 נוספו מניות ({dilution.fmt(p['tso0'])} → {dilution.fmt(d['tso'])}) "
                                          "– מדלל פעיל, יציאה מיידית")
    # heavy selling: big volume while the price drops (often a diluter dumping shares)
    win = config.TRADE_DUMP_BARS
    if len(bars) >= 3 * win:
        recent, before = bars[-win:], bars[:-win]
        avg_v = sum(b["v"] for b in before) / len(before) * win
        drop = 1 - last / recent[0]["o"]
        if avg_v and sum(b["v"] for b in recent) >= config.TRADE_DUMP_VOL_X * avg_v and drop >= config.TRADE_DUMP_DROP:
            return _close(run, sym, sell, f"🔴 מכירה כבדה: ירידה של {drop * 100:.0f}% בווליום חריג – כנראה מדלל מוכר")
    # end of day: never keep a stock overnight unless it is clearly working
    if _mins(now) >= config.TRADE_EOD_MIN and last < p["entry"] + config.TRADE_OVERNIGHT_MIN_R * p["R"]:
        return _close(run, sym, sell, "סוף יום: המניה לא התקדמה מספיק – לא משאיר אותה ללילה")
    if time.time() - p["opened"] > config.TRADE_MAX_DAYS * 86400 and _mins(now) >= config.TRADE_EOD_MIN:
        return _close(run, sym, sell, f"מוחזקת יותר מ-{config.TRADE_MAX_DAYS} ימים – יציאה")


# ---------- reports ----------

def _trades_since(pt, ts):
    return [x for x in pt["closed"] if x["closed"] >= ts]


def _stats(trades):
    if not trades:
        return "אין עסקאות שנסגרו."
    wins = [x for x in trades if x["pnl"] > 0.005]
    pnl = sum(x["pnl"] for x in trades)
    avg_r = sum(x["r"] for x in trades) / len(trades)
    return (f"עסקאות שנסגרו: {len(trades)} · מוצלחות: {len(wins)} ({len(wins) / len(trades) * 100:.0f}%)\n"
            f"רווח/הפסד: {'+' if pnl >= 0 else '−'}${abs(pnl):,.2f} · ממוצע לעסקה: {avg_r:+.2f}R")


def portfolio_text(st):
    pt = init(st)
    eq = equity(pt)
    lines = ["💼 <b>התיק הווירטואלי</b>",
             f"שווי: ${eq:,.0f} ({(eq / pt['start'] - 1) * 100:+.1f}% מההתחלה, ${pt['start']:,.0f})",
             f"מזומן: ${pt['cash']:,.0f}"]
    if pt["pos"]:
        lines.append("\n<b>מניות פתוחות:</b>")
        for s, p in pt["pos"].items():
            ch = (p.get("last", p["entry"]) / p["entry"] - 1) * 100
            lines.append(f"• <b>{s}</b> {p['shares']:,} × ${fmt_price(p['entry'])} · עכשיו ${fmt_price(p.get('last'))} "
                         f"({ch:+.0f}%) · 🎯 ${fmt_price(p['target'])} · 🛑 ${fmt_price(p['stop'])}"
                         + (" (בלי הפסד)" if p["stop"] >= p["entry"] else ""))
    else:
        lines.append("אין מניות פתוחות.")
    if pt["cands"]:
        lines.append("\n<b>במעקב לקנייה:</b>")
        for s, c in pt["cands"].items():
            lines.append(f"• {s} – {esc(c['cat'])}" + (f" · {esc(c['why'])}" if c.get("why") else ""))
    lines += ["", "<b>מאז ההתחלה:</b>", _stats(pt["closed"])]
    return "\n".join(lines)


def trades_text(st):
    pt = init(st)
    if not pt["closed"]:
        return "עוד אין עסקאות שנסגרו."
    lines = ["📒 <b>10 העסקאות האחרונות</b>"]
    for x in pt["closed"][-10:][::-1]:
        when = datetime.fromtimestamp(x["closed"], ET).strftime("%d/%m")
        lines.append(f"{when} <b>{x['sym']}</b> ${fmt_price(x['entry'])}→${fmt_price(x['exit'])} "
                     f"{'+' if x['pnl'] >= 0 else '−'}${abs(x['pnl']):,.2f} ({x['r']:+.1f}R) · {esc(x['why'])}")
    return "\n".join(lines)


def _reports(run):
    pt, now = run.st["pt"], et_now()
    day = str(now.date())
    if _session(now) != "post" or pt["day_rep"] == day:
        return
    start_of_day = datetime(now.year, now.month, now.day, tzinfo=ET).timestamp()
    today = _trades_since(pt, start_of_day)
    skips = pt["skips"].get(day, [])
    if today or pt["pos"] or skips:
        lines = [f"📊 <b>סיכום יום – מסחר וירטואלי</b>", _stats(today),
                 f"שווי התיק: ${equity(pt):,.0f} ({(equity(pt) / pt['start'] - 1) * 100:+.1f}% מההתחלה)"]
        if pt["pos"]:
            lines.append("נשארו פתוחות ללילה: " + ", ".join(pt["pos"]))
        if skips:
            lines.append("\n<b>חדשות שלא נקנו:</b>")
            lines += [f"• {s} – {esc(w)}" for s, w in skips[:8]]
        if run.notify("\n".join(lines)):
            pt["day_rep"] = day
    else:
        pt["day_rep"] = day
    if now.weekday() == 4 and pt["week_rep"] != day:
        week = _trades_since(pt, start_of_day - 4 * 86400)
        if run.notify("🗓️ <b>סיכום שבוע – מסחר וירטואלי</b>\n" + _stats(week) + "\n\n<b>מאז ההתחלה:</b>\n"
                      + _stats(pt["closed"])):
            pt["week_rep"] = day
    pt["skips"] = {d: v for d, v in pt["skips"].items() if d == day}


# ---------- one cycle ----------

def cycle(run):
    if not config.TRADE_ON:
        return
    st = run.st
    pt = init(st)
    _bars.clear()
    now = et_now()
    pt["dil"] = {s: t for s, t in pt["dil"].items() if time.time() - t < 30 * 86400}
    if _session(now) == "open":
        for sym in list(pt["pos"]):
            try:
                _manage(run, sym, pt["pos"][sym])
            except Exception as e:
                log(f"trader: manage failed ({net.short_err(e)})")
        day = str(now.date())
        for sym in list(pt["cands"]):
            c = pt["cands"][sym]
            if c["deadline"] is None:
                open_ts = datetime(now.year, now.month, now.day, 9, 30, tzinfo=ET).timestamp()
                c["deadline"] = max(c["ts"], open_ts) + config.TRADE_WINDOW_MIN * 60
            try:
                bought = sym not in pt["pos"] and _buy(run, sym, c)
            except Exception as e:
                log(f"trader: buy check failed ({net.short_err(e)})")
                bought = False
            if bought or sym in pt["pos"]:
                pt["cands"].pop(sym, None)
            elif c.get("drop") or time.time() > c["deadline"]:
                pt["skips"].setdefault(day, []).append([sym, c.get("why") or "לא נמצאה נקודת כניסה טובה"])
                pt["cands"].pop(sym, None)
    else:
        # news that came after the close waits for the next session; expired / old ones are dropped
        for sym, c in list(pt["cands"].items()):
            if c["deadline"] and time.time() > c["deadline"]:
                pt["skips"].setdefault(str(now.date()), []).append(
                    [sym, c.get("why") or "לא נמצאה נקודת כניסה טובה"])
                pt["cands"].pop(sym, None)
            elif time.time() - c["ts"] > 3 * 86400:
                pt["cands"].pop(sym, None)
    _reports(run)
