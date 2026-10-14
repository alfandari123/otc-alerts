"""Paper-trading agent (virtual money only).

Buys an OTC stock after strong positive news when the price starts to move with volume, only if the
possible gain is at least 3x the possible loss - after the spread AND the broker fees. Stays away from active
diluters. Once the gain equals the risk, the stop moves to the entry price (no loss), then trails the high.
At the target it sells 2/3 and keeps 1/3 as a "bonus" when there is no dilution danger and the move is strong.
Heavy selling is investigated (dilution? bad news? a seller sitting on one price?) before deciding to get out.
A stock that does not move is held while there is no danger; bad news or dilution -> out at once.

Prices are Yahoo 1-minute bars (last trades, no bid/ask). To stay honest every buy is filled one tick ABOVE
the last trade and every sell one tick BELOW it, and a stop that is gapped through fills at the bar's open.
"""
import math
import time
from datetime import datetime

from . import config, dilution, insiders, market, net, schwab
from .util import ET, esc, et_now, fmt_price, log

BARS_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{}?interval=1m&range=1d"

DIL_LABELS = {"הנפקת מניות (דילול)", "הנפקת Reg A (דילול)", "תשקיף הנפקה (דילול)", "הגדלת מניות מורשות",
              "גיוס / דילול", "התחייבות פיננסית / הלוואה", "ספליט הפוך", "ספליט הפוך (נוספה סיומת D לטיקר)"}
END_OF_DILUTION = "סוף הדילול"

_bars = {}   # per-cycle cache: ticker -> (bars, previous close)
_quotes = {}  # per-cycle cache: ticker -> Schwab bid/ask (None when not connected)


# ---------- helpers ----------

def tick(p):
    return 0.0001 if p < 1 else 0.01


def _grid(x, p, up):
    t = tick(p)
    n = x / t
    n = math.ceil(n - 1e-9) if up else math.floor(n + 1e-9)
    return round(max(n, 1) * t, 4 if t < 0.01 else 2)


def live():
    return config.TRADE_MODE == "live"


def init(st):
    """The agent's book: "pt" = virtual money, "lt" = real money at Schwab (separate, so the test stays clean)."""
    pt = st.setdefault("lt" if live() else "pt", {})
    cash = config.TRADE_LIVE_MAX_CAPITAL if live() else config.TRADE_START_CASH
    for k, v in (("start", cash), ("cash", cash), ("pos", {}), ("cands", {}),
                 ("closed", []), ("skips", {}), ("day_rep", ""), ("week_rep", ""), ("dil", {}), ("neg", {}),
                 ("eq_hist", []), ("fees", 0.0), ("notes", {}), ("paused", False), ("orders", {}), ("rm", {})):
        pt.setdefault(k, v)
    for p in pt["pos"].values():   # positions opened by the first version
        p.setdefault("basis", p["shares"] * p["entry"])
        p.setdefault("shares0", p["shares"])
        p.setdefault("runner", False)
        p.setdefault("ref", p["stop0"])
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


def quote(sym):
    """Real bid/ask from Schwab when connected (also improves the virtual test), else None."""
    if sym in _quotes:
        return _quotes[sym]
    q = None
    s = schwab.status()
    if s and s.get("connected"):
        try:
            q = schwab.quotes([sym]).get(sym)
        except Exception as e:
            log(f"trader: quote failed ({net.short_err(e)})")
    if q and not (q.get("bid") and q.get("ask")):
        q = None
    _quotes[sym] = q
    return q


_books = {}   # per-cycle cache: ticker -> Level 2 snapshot (None when not available)


def book(sym):
    """Level 2 from Schwab's stream (bids/asks with market makers), or None."""
    if sym in _books:
        return _books[sym]
    res = None
    s = schwab.status()
    if s and s.get("connected"):
        try:
            res = schwab.snapshot([sym], seconds=config.TRADE_L2_SECONDS).get(sym)
            if res and not (res["bids"] or res["asks"]):
                res = None
        except Exception as e:
            log(f"trader: level 2 failed ({net.short_err(e)})")
    _books[sym] = res
    return res


def stop_exit(bk, stop, shares):
    """Where selling at the stop would really fill: walk the buyers at or below the stop until our shares are
    absorbed. Returns the average price, or None when the visible buyers are not enough."""
    left, got = shares, 0.0
    for x in sorted((x for x in bk["bids"] if x["price"] and x["price"] <= stop + 1e-9), key=lambda x: -x["price"]):
        take = min(left, x["size"])
        got += take * x["price"]
        left -= take
        if left <= 0:
            return got / shares
    return None


def bid_hole(bk, at_or_below):
    """The first gap in the buyers' side at or below a price: (last solid level, next buyer) or None.
    Example (BDPT): buyers at 0.0041 and 0.0040, the next one only at 0.0023."""
    levels = sorted({x["price"] for x in bk["bids"] if x["price"] and x["price"] <= at_or_below + 1e-9}, reverse=True)
    for a, b in zip(levels, levels[1:]):
        if 1 - b / a >= config.TRADE_L2_HOLE:
            return a, b
    return None


def hole_text(h):
    return f"אחרי ${fmt_price(h[0])} הקונה הבא רק ב-${fmt_price(h[1])} (−{(1 - h[1] / h[0]) * 100:.0f}%)"


def book_wall(bk, low, high, our_shares):
    """Hebrew description of a big seller sitting between `low` and `high`, or ''."""
    if not bk or not bk["asks"]:
        return ""
    sizes = sorted(x["size"] for x in bk["asks"])
    median = sizes[len(sizes) // 2] or 1
    for x in bk["asks"]:
        if low <= (x["price"] or 0) < high and x["size"] >= max(config.TRADE_L2_WALL_X * median,
                                                                 config.TRADE_L2_WALL_VS_US * our_shares):
            mm = ", ".join(m["id"] for m in x["mm"] if m.get("id"))
            return f"${fmt_price(x['price'])}: {x['size']:,.0f} מניות" + (f" ({mm})" if mm else "")
    return ""


def sell_price(sym, last):
    """Price we can sell at right now: the real bid, or one tick under the last trade."""
    q = quote(sym)
    return q["bid"] if q else last - tick(last)


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


def _days(ts):
    return (time.time() - ts) / 86400


def price_class(p):
    """Trader slang for sub-penny price levels."""
    if p < 0.001:
        return "טריפס"     # $0.000X
    if p < 0.01:
        return "דאבס"      # $0.00XX
    return "פני"


# ---------- hooks called from main.py ----------

def on_news(st, sym, score, cat, it, price):
    """A strong positive news item: the stock becomes a candidate for a buy."""
    if not config.TRADE_ON or not sym:
        return
    pt = init(st)
    if sym in pt["pos"]:
        pt["pos"][sym]["news"] = [time.time(), cat]     # fresh good news for a stock we hold
        return
    if sym in pt["cands"]:
        return
    tier = (it.get("tier") or "").lower()
    if "expert" in tier or "grey" in tier:
        return
    pt["cands"][sym] = {"ts": time.time(), "score": score, "cat": cat, "price": price, "ref": None,
                        "deadline": None, "why": "", "title": (it.get("title") or "")[:120],
                        "url": it.get("url", "")}
    log("trader: new candidate")


def on_deal(st, syms, facts):
    """A merger / share exchange paid with RESTRICTED shares to the new owners (Rule 144): that is not market
    dilution, so it does not block a buy the way a convertible note or an offering does."""
    if not config.TRADE_ON:
        return
    pt = init(st)
    for s in syms:
        if time.time() - pt["dil"].get(s, 0) < 900:      # the flag this same filing just set (Item 3.02)
            pt["dil"].pop(s, None)
        pt["rm"][s] = {"ts": time.time(), "after": facts.get("after"), "auth": facts.get("auth")}


def on_item(st, it, rule):
    """Any new item: remember fresh dilution signs and bad news per ticker."""
    if not config.TRADE_ON or not it.get("syms"):
        return
    labels = set(rule["labels"])
    pt = init(st)
    if labels & DIL_LABELS and END_OF_DILUTION not in labels:
        for s in it["syms"]:
            pt["dil"][s] = time.time()
    elif rule["pol"] < 0:
        for s in it["syms"]:
            pt["neg"][s] = [time.time(), rule["label"]]


# ---------- danger checks ----------

def _danger(st, sym, since=None):
    """Hebrew reason when the stock is dangerous (dilution / bad news), else ''."""
    pt = init(st)
    since = since or time.time() - config.TRADE_DIL_FLAG_DAYS * 86400
    if pt["dil"].get(sym, 0) > since:
        return "דיווח טרי על הנפקה / הלוואה להמרה / ספליט הפוך"
    neg = pt["neg"].get(sym)
    if neg and neg[0] > since:
        return f"חדשה שלילית: {neg[1]}"
    why = insiders.danger(insiders.get(st, sym))
    if why:
        return why
    rm = pt["rm"].get(sym)
    if rm and _days(rm["ts"]) < 180:
        # share count jumped because of the merger (restricted shares) - judge the room left, not the jump
        if rm.get("after") and rm.get("auth") and rm["after"] >= rm["auth"] * config.TRADE_AUTH_USED_MAX:
            return "אחרי המיזוג כמעט כל המניות המורשות בשימוש – צפויה הגדלה ודילול"
        return ""
    d = dilution.summary(sym, st, str(et_now().date()))
    if d:
        if (d.get("chg3") or 0) >= config.DILUTION_WARN_3M:
            return f"מדלל פעיל: מספר המניות עלה ב-{d['chg3'] * 100:.0f}% ב-3 חודשים"
        if (d.get("chg6") or 0) >= config.DILUTION_WARN_6M:
            return f"מדלל פעיל: מספר המניות עלה ב-{d['chg6'] * 100:.0f}% בחצי שנה"
        if d.get("auth") and d["tso"] / d["auth"] >= config.TRADE_AUTH_USED_MAX:
            return f"ניצלה {d['tso'] / d['auth'] * 100:.0f}% מהמניות המורשות – צפויה הגדלה ודילול"
    return ""


def _seller_wall(bars):
    """A big seller sitting on one price: many touches of the same high with heavy volume, no break.
    Returns the price level or None."""
    win = bars[-config.TRADE_WALL_BARS:]
    if len(win) < config.TRADE_WALL_BARS or len(bars) < 2 * config.TRADE_WALL_BARS:
        return None
    level = max(b["h"] for b in win)
    t = tick(level)
    touches = sum(1 for b in win if b["h"] >= level - t / 2)
    before = bars[:-len(win)]
    normal = sum(b["v"] for b in before) / len(before) * len(win)
    heavy = normal and sum(b["v"] for b in win) >= config.TRADE_WALL_VOL_X * normal
    if touches >= config.TRADE_WALL_TOUCHES and heavy and win[-1]["c"] < level:
        return level
    return None


def _momentum(bars):
    """True when buyers are still in control: volume still strong and the price near the day's high."""
    if len(bars) < 20:
        return False
    recent = bars[-10:]
    avg_bar = sum(b["v"] for b in bars) / len(bars)
    high = max(b["h"] for b in bars)
    return sum(b["v"] for b in recent) / 10 >= avg_bar and recent[-1]["c"] >= high * 0.9


# ---------- buying ----------

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
    wall = _seller_wall(bars)
    if wall:
        return None, f"מוכר גדול יושב על ${fmt_price(wall)} – המחיר לא עובר אותו"
    entry = _grid(last + t, last, True)
    q = c.get("q")
    if q:    # real bid/ask from Schwab: buy at the ask, and skip when buyers and sellers are too far apart
        spread = (q["ask"] - q["bid"]) / q["ask"]
        if spread > config.TRADE_MAX_SPREAD:
            return None, f"הפער בין הקונים למוכרים גדול מדי ({spread * 100:.0f}%)"
        entry = _grid(q["ask"], q["ask"], True)
    swing = min(b["l"] for b in bars[-15:])
    stop = _grid(max(ref, swing - t), last, False)
    stop = max(stop, _grid(entry * (1 - config.TRADE_MAX_RISK), last, True))
    if stop > entry - 2 * t:
        stop = _grid(entry - 2 * t, last, False)
    stop_fill = max(stop - t, t)
    risk = entry - stop_fill
    if risk / entry > config.TRADE_MAX_RISK + 1e-9:
        return None, f"הסיכון גדול מדי ({risk / entry * 100:.0f}%) – צעד מחיר גדול מדי במחיר הזה"
    return {"entry": entry, "stop": stop, "risk": risk, "ref": ref}, ""


def _size_pct(score, price):
    """How much of the portfolio this setup deserves: stronger news = bigger; trips (0.000X) = smaller."""
    pct = config.TRADE_SIZE_BY_SCORE.get(min(score, 10), config.TRADE_POSITION_PCT)
    if price < 0.001:
        pct *= config.TRADE_TRIPS_FACTOR
    return min(pct, config.TRADE_MAX_POSITION_PCT)


def _target(entry, risk, shares, p):
    """Lowest target that still pays 3x the risk after fees: gain - 2 fees >= 3 x (loss + 2 fees)."""
    fee = config.TRADE_FEE
    need = config.TRADE_RR * risk + (2 * fee * (1 + config.TRADE_RR)) / shares
    return _grid(entry + need, p, True)


def _buy(run, sym, c):
    st, pt = run.st, init(run.st)
    now = et_now()
    if not config.TRADE_ENTRY_FROM <= _mins(now) <= config.TRADE_ENTRY_UNTIL:
        return False
    bars, prev = get_bars(sym)
    if len(bars) < config.TRADE_CONFIRM_BARS:
        c["why"] = "אין מספיק מסחר היום"
        return False
    why = _danger(st, sym)
    if why:
        c["why"], c["drop"] = why, True
        return False
    if pt["paused"]:
        c["why"] = "הסוכן בהשהיה (/go כדי להמשיך)"
        return False
    c["q"] = quote(sym)
    plan, why = _plan(c, bars, prev)
    c.pop("q", None)
    if not plan:
        c["why"] = why
        return False
    avg = market.avg_volume(sym, now.date(), st["avgvol"])
    vol = sum(b["v"] for b in bars)
    if avg and vol < config.TRADE_VOL_CONFIRM_X * avg:
        c["why"] = f"אין מספיק ווליום ({vol / avg:.1f} מהממוצע)"
        return False
    if len(pt["pos"]) >= config.TRADE_MAX_POSITIONS and not _rotate(run, c):
        c["why"] = "כבר יש מקסימום מניות פתוחות"
        return False
    entry, risk = plan["entry"], plan["risk"]
    pct = _size_pct(c["score"], entry)
    budget = min(equity(pt) * pct, pt["cash"] - config.TRADE_FEE)
    budget = min(budget, vol * config.TRADE_MAX_VOL_SHARE * entry)   # we must be able to get out
    shares = int(budget / entry)
    bk = book(sym)
    if bk and bk["bids"]:     # Level 2: buy no more than the buyers standing near the bid can take back from us
        depth = sum(x["size"] for x in bk["bids"][:config.TRADE_L2_LEVELS])
        shares = min(shares, int(depth * config.TRADE_L2_BID_SHARE))
        # a hole under the stop: the stop would really fill much lower -> buy only what the buyers near the
        # stop can absorb (or nothing)
        visible_low = min((x["price"] for x in bk["bids"] if x["price"]), default=None)
        if visible_low is not None and visible_low <= plan["stop"]:
            ex = stop_exit(bk, plan["stop"], shares)
            if ex is None or entry - ex > risk * config.TRADE_L2_RISK_X:
                near = sum(x["size"] for x in bk["bids"]
                           if x["price"] and plan["stop"] * (1 - config.TRADE_L2_NEAR) <= x["price"] <= plan["stop"])
                shares = min(shares, int(near))
                if shares * entry < config.TRADE_MIN_DOLLARS:
                    h = bid_hole(bk, plan["stop"])
                    c["why"] = ("Level 2: חור בצד הקונים מתחת לעצירה – " + hole_text(h)) if h else \
                        "Level 2: אין מספיק קונים ליד נקודת העצירה"
                    return False
    if shares * entry < config.TRADE_MIN_DOLLARS:
        c["why"] = "אין מספיק מסחר / קונים במניה בשביל עסקה כדאית"
        return False
    target = _target(entry, risk, shares, entry)
    if target / entry - 1 > config.TRADE_MAX_TARGET:
        c["why"] = f"כדי לקבל 1:3 אחרי עמלות צריך עלייה של {(target / entry - 1) * 100:.0f}% – לא ריאלי"
        return False
    wall = book_wall(bk, entry, target, shares)
    if wall:
        c["why"] = f"Level 2: קיר מוכרים לפני היעד – {wall}"
        return False
    info = {"stop": plan["stop"], "target": target, "R": risk, "ref": plan["ref"], "cat": c["cat"],
            "score": c["score"], "title": c.get("title", ""), "url": c.get("url", ""),
            "last": bars[-1]["c"], "last_t": bars[-1]["t"]}
    if live():
        return _live_buy(run, sym, shares, entry, info)
    _open(run, sym, shares, entry, info)
    return True


def _open(run, sym, shares, entry, info):
    """A buy was filled (virtual: at once; real: when Schwab reports the fill)."""
    st, pt = run.st, init(run.st)
    cost = shares * entry
    pt["cash"] -= cost + config.TRADE_FEE
    pt["fees"] += config.TRADE_FEE
    d = dilution.summary(sym, st, str(et_now().date()))
    risk, target = info["R"], info["target"]
    pt["pos"][sym] = {"shares": shares, "shares0": shares, "entry": entry, "stop": info["stop"],
                      "stop0": info["stop"], "target": target, "R": risk, "high": entry, "last": info["last"],
                      "opened": time.time(), "last_t": info["last_t"], "cat": info["cat"], "score": info["score"],
                      "ref": info["ref"], "basis": cost + config.TRADE_FEE, "tso0": d["tso"] if d else None,
                      "tso_chk": time.time(), "be": False, "runner": False, "title": info["title"],
                      "url": info["url"]}
    c = info
    plan = {"stop": info["stop"], "ref": info["ref"]}
    gain = (target / entry - 1) * 100
    loss = risk / entry * 100
    net_gain = shares * (target - entry) - 2 * config.TRADE_FEE
    net_loss = shares * risk + 2 * config.TRADE_FEE
    run.notify(f"🛒 <b>קנייה {_word()}: {sym}</b> ({price_class(entry)})\n"
               f"{esc(c['cat'])} · ציון {c['score']}/10\n"
               f"{shares:,} מניות × ${fmt_price(entry)} = ${cost:,.0f} ({cost / equity(pt) * 100:.0f}% מהתיק)\n"
               f"🎯 יעד: ${fmt_price(target)} (+{gain:.0f}%) · 🛑 עצירה: ${fmt_price(plan['stop'])} (−{loss:.0f}%)\n"
               f"אחרי עמלות: רווח אפשרי ${net_gain:,.0f} מול הפסד אפשרי ${net_loss:,.0f} "
               f"(1:{net_gain / net_loss:.1f})\n"
               f"מחיר לפני החדשה: ${fmt_price(plan['ref'])}\n"
               + "".join(x + "\n" for x in insiders.lines(st.get("ins", {}).get(sym, [0, None])[1]))
               +
               f"ברווח של +{loss:.0f}% העצירה עולה למחיר הקנייה (בלי הפסד). ביעד: מוכר 2/3, ו-1/3 נשאר כבונוס "
               "אם אין סכנת דילול.")
    log("trader: bought")
    return True


def _rotate(run, c):
    """All slots are full: free one by selling a stock that has been standing still, for much stronger news."""
    pt = init(run.st)
    if c["score"] < config.TRADE_ROTATE_SCORE:
        return False
    still = [(p["opened"], s) for s, p in pt["pos"].items()
             if _days(p["opened"]) >= config.TRADE_STALE_DAYS and abs(p.get("last", p["entry"]) - p["entry"]) < 0.5 * p["R"]]
    if not still:
        return False
    _, sym = min(still)
    p = pt["pos"][sym]
    _sell(run, sym, p["shares"], sell_price(sym, p["last"]),
          f"מפנה מקום: המניה עומדת {_days(p['opened']):.0f} ימים, ויש הזדמנות חזקה יותר")
    return True


# ---------- selling ----------

def _word():
    return "אמיתית" if live() else "וירטואלית"


def _sell(run, sym, shares, fill, reason):
    """Sell `shares` (all or part) at about `fill`. Returns True when the position is closed now.
    Real money: a limit order is sent and the sale is booked when Schwab reports the fill."""
    if live():
        _live_sell(run, sym, shares, fill, reason)
        return False
    return _book(run, sym, shares, fill, reason)


def _book(run, sym, shares, fill, reason, fee=True):
    pt = init(run.st)
    p = pt["pos"][sym]
    shares = min(shares, p["shares"])
    fill = max(fill, tick(fill))
    basis = p["basis"] * shares / p["shares"]
    cost = config.TRADE_FEE if fee else 0.0
    proceeds = shares * fill - cost
    pnl = proceeds - basis
    pt["cash"] += proceeds
    pt["fees"] += cost
    p["basis"] -= basis
    p["shares"] -= shares
    closed = p["shares"] <= 0
    r_mult = (fill - p["entry"]) / p["R"] if p["R"] else 0
    pt["closed"].append({"sym": sym, "entry": p["entry"], "exit": fill, "shares": shares, "pnl": round(pnl, 2),
                         "r": round(r_mult, 2), "opened": p["opened"], "closed": time.time(), "why": reason,
                         "part": not closed})
    pt["closed"] = pt["closed"][-300:]
    if closed:
        pt["pos"].pop(sym)
    pct = (fill / p["entry"] - 1) * 100
    icon = "✅" if pnl > 0.005 else ("⚪" if pnl > -0.005 else "🔻")
    part = "" if closed else f" (חלקית – {shares:,} מתוך {shares + p['shares']:,} מניות)"
    run.notify(f"{icon} <b>מכירה {_word()}: {sym}</b>{part}\n{esc(reason)}\n"
               f"קנייה ${fmt_price(p['entry'])} ← מכירה ${fmt_price(fill)} ({pct:+.0f}%)\n"
               f"רווח/הפסד אחרי עמלות: <b>{'+' if pnl >= 0 else '−'}${abs(pnl):,.2f}</b> ({r_mult:+.1f}R)\n"
               + ("" if closed else f"נשארו {p['shares']:,} מניות" + (" כבונוס" if p["runner"] else "")
                  + f" · 🛑 עצירה ${fmt_price(p['stop'])}" + (" (ברווח)" if p["stop"] > p["entry"] else "") + "\n")
               + f"שווי התיק: ${equity(pt):,.0f}")
    log("trader: sold" + ("" if closed else " (part)"))
    return closed


# ---------- real money (Schwab): orders are sent, fills are booked when Schwab reports them ----------

def _live_buy(run, sym, shares, entry, info):
    st, pt = run.st, init(run.st)
    try:
        acct = schwab.account(st)
    except Exception as e:
        log(f"trader: account failed ({net.short_err(e)})")
        return False
    shares = min(shares, int((acct["cash"] - config.TRADE_FEE) / entry))
    if shares * entry < config.TRADE_MIN_DOLLARS:
        _note(run, sym, "cash", f"⚠️ <b>{sym}</b>: אין מספיק מזומן בחשבון Schwab לקנייה.")
        return False
    try:
        oid = schwab.place(st, sym, "BUY", shares, entry)
    except Exception as e:
        _note(run, sym, "rej", f"⚠️ <b>{sym}</b>: Schwab דחו את פקודת הקנייה ({esc(net.short_err(e))}).")
        return False
    reserve = shares * entry + config.TRADE_FEE
    pt["cash"] -= reserve     # held until the order ends, so two buys cannot use the same money
    pt["orders"][oid] = {"sym": sym, "qty": shares, "price": entry, "ts": time.time(), "reserve": reserve,
                         "info": info}
    run.notify(f"📤 <b>נשלחה פקודת קנייה: {sym}</b>\n{shares:,} מניות במחיר עד ${fmt_price(entry)} "
               f"(${shares * entry:,.0f}). אם לא תתבצע תוך {config.TRADE_LIVE_ORDER_MIN} דקות – תבוטל.")
    log("trader: buy order sent")
    return True


def _live_sell(run, sym, shares, price, reason):
    st, pt = run.st, init(run.st)
    p = pt["pos"][sym]
    if p.get("selling"):
        return
    shares = min(shares, p["shares"])
    try:
        held = schwab.account(st)["pos"].get(sym, 0)
        shares = min(shares, held)    # never sell shares that are not there (he trades the same account)
        if shares <= 0:
            pt["pos"].pop(sym)
            run.notify(f"⚠️ <b>{sym}</b>: המניות כבר לא בחשבון (נמכרו ידנית?) – הסוכן מפסיק לנהל אותה.")
            return
        q = quote(sym)
        if q:
            price = q["bid"]          # real money: sell to the buyers that are there now
        price = max(_grid(price, price, False), tick(price))
        oid = schwab.place(st, sym, "SELL", shares, price)
    except Exception as e:
        log(f"trader: sell order failed ({net.short_err(e)})")
        return
    p["selling"] = {"oid": oid, "qty": shares, "price": price, "ts": time.time(), "why": reason, "booked": 0}
    run.notify(f"📤 <b>נשלחה פקודת מכירה: {sym}</b>\n{esc(reason)}\n{shares:,} מניות במחיר ${fmt_price(price)}")
    log("trader: sell order sent")


def _sync_orders(run):
    """Follow our open orders at Schwab: book fills, cancel stale buys, re-price stale sells."""
    st, pt = run.st, init(run.st)
    try:
        for oid, o in list(pt["orders"].items()):
            s = schwab.order(st, oid)
            done = s["status"] in ("FILLED", "CANCELED", "REJECTED", "EXPIRED", "REPLACED")
            if not done:
                if time.time() - o["ts"] > config.TRADE_LIVE_ORDER_MIN * 60 and not o.get("cancel"):
                    schwab.cancel(st, oid)
                    o["cancel"] = True
                continue
            pt["orders"].pop(oid)
            pt["cash"] += o["reserve"]
            if s["filled"] > 0:
                _open(run, o["sym"], s["filled"], s["avg"] or o["price"], o["info"])
            else:
                run.notify(f"⚪ <b>{o['sym']}</b>: פקודת הקנייה לא בוצעה (אין מוכרים במחיר) ובוטלה.")
        for sym, p in list(pt["pos"].items()):
            sel = p.get("selling")
            if not sel:
                continue
            s = schwab.order(st, sel["oid"])
            new = s["filled"] - sel["booked"]
            if new > 0:
                first = sel["booked"] == 0
                sel["booked"] = s["filled"]
                closed = _book(run, sym, new, s["avg"] or sel["price"], sel["why"], fee=first)
                if closed:
                    continue
            if s["status"] in ("FILLED", "CANCELED", "REJECTED", "EXPIRED", "REPLACED"):
                p.pop("selling", None)     # what is left is managed again next minute (new price)
            elif time.time() - sel["ts"] > config.TRADE_LIVE_SELL_RETRY_MIN * 60 and not sel.get("cancel"):
                schwab.cancel(st, sel["oid"])
                sel["cancel"] = True
    except schwab.NotConnected:
        log("trader: Schwab not connected")


def _note(run, sym, key, text):
    """One information message per stock per day (no spam)."""
    notes = init(run.st)["notes"]
    k = f"{sym}:{key}:{et_now().date()}"
    if k not in notes:
        notes[k] = time.time()
        run.notify(text)


def _at_target(run, sym, p, bars):
    """Target reached: sell 2/3 and keep 1/3 as a bonus when there is no dilution danger and buyers are strong."""
    danger = _danger(run.st, sym)
    if danger or not _momentum(bars):
        why = f"🎯 הגיע ליעד – מוכר הכול ({esc(danger) if danger else 'המומנטום נחלש'})"
        return _sell(run, sym, p["shares"], p["target"], why)
    part = int(p["shares"] * config.TRADE_TAKE_PART)
    p["runner"] = True
    p["stop"] = max(p["stop"], _grid(p["entry"] + config.TRADE_RUNNER_LOCK_R * p["R"], p["entry"], False))
    _sell(run, sym, part, p["target"], "🎯 הגיע ליעד – מוכר 2/3, אין סכנת דילול והקונים חזקים: 1/3 נשאר כבונוס")
    return False


def _heavy_selling(run, sym, p, bars, sell):
    """Big volume while the price drops: find out why before deciding. Returns True when we sold."""
    win = config.TRADE_DUMP_BARS
    if len(bars) < 3 * win:
        return False
    recent, before = bars[-win:], bars[:-win]
    normal = sum(b["v"] for b in before) / len(before) * win
    last = bars[-1]["c"]
    drop = 1 - last / recent[0]["o"]
    if not (normal and sum(b["v"] for b in recent) >= config.TRADE_DUMP_VOL_X * normal
            and drop >= config.TRADE_DUMP_DROP):
        return False
    reasons = []
    danger = _danger(run.st, sym, since=p["opened"] - config.TRADE_DIL_FLAG_DAYS * 86400)
    if danger:
        reasons.append(danger)
    wall = _seller_wall(bars)
    if wall:
        reasons.append(f"מוכר גדול יושב על ${fmt_price(wall)} (כנראה מדלל)")
    wall2 = book_wall(book(sym), last, last * 1.5, p["shares"])
    if wall2:
        reasons.append(f"Level 2: קיר מוכרים – {wall2}")
    if last < p["ref"]:
        reasons.append("המחיר ירד מתחת למחיר שלפני החדשה – החדשה 'נמחקה'")
    if reasons:
        _sell(run, sym, p["shares"], sell, "🔴 מכירה כבדה – נורה אדומה: " + "; ".join(reasons))
        return True
    _note(run, sym, "dump", f"ℹ️ <b>{sym}</b>: מכירה כבדה (ירידה של {drop * 100:.0f}%), אבל אין דילול, "
                            f"אין חדשה רעה ואין מוכר שיושב על מחיר – נראה כמו מימוש רווחים. "
                            f"נשאר בפנים, העצירה ב-${fmt_price(p['stop'])}.")
    return False


def _manage(run, sym, p):
    st = run.st
    now = et_now()
    if p.get("selling"):      # a real sell order is working; _sync_orders follows it
        return
    bars, _ = get_bars(sym)
    new = [b for b in bars if b["t"] > p["last_t"]]
    for b in new:
        t = tick(b["c"])
        if b["l"] <= p["stop"]:     # checked first: we do not know if the low or the high came first
            fill = min(p["stop"], b["o"]) - t
            if p["runner"]:
                why = "בונוס: העצירה הנגררת נפגעה – הרווח נשמר"
            elif p["be"] and p["stop"] <= p["entry"] + t:
                why = "העצירה הוזזה למחיר הקנייה ונפגעה – יציאה בלי הפסד"
            else:
                why = "עצירה נגררת – נועל רווח" if p["stop"] > p["entry"] else "הגיע לנקודת העצירה"
            if b["o"] < p["stop"]:
                why += " (המחיר קפץ מתחת לעצירה)"
            _sell(run, sym, p["shares"], fill, why)
            return
        if not p["runner"] and b["h"] >= p["target"] + t:
            p["last"], p["last_t"] = b["c"], b["t"]
            if _at_target(run, sym, p, [x for x in bars if x["t"] <= b["t"]]):
                return
        p["high"] = max(p["high"], b["h"])
        p["last"], p["last_t"] = b["c"], b["t"]
        if p["high"] >= p["entry"] + p["R"] and p["stop"] < p["entry"] + t:
            p["stop"], p["be"] = _grid(p["entry"] + t, p["entry"], True), True   # sell fill = entry price
        trail = config.TRADE_RUNNER_TRAIL_R if p["runner"] else 1.0
        if p["runner"] or p["high"] >= p["entry"] + config.TRADE_TRAIL_AFTER_R * p["R"]:
            p["stop"] = max(p["stop"], _grid(p["high"] - trail * p["R"], p["high"], False))
    if not bars:
        return
    last = bars[-1]["c"]
    sell = sell_price(sym, last)
    # dilution or bad news after we bought -> out at once
    if init(st)["dil"].get(sym, 0) > p["opened"]:
        _sell(run, sym, p["shares"], sell, "🔴 זוהה דיווח על דילול / הנפקה – יציאה מיידית")
        return
    neg = init(st)["neg"].get(sym)
    if neg and neg[0] > p["opened"]:
        _sell(run, sym, p["shares"], sell, f"🔴 חדשה שלילית ({neg[1]}) – יציאה מיידית")
        return
    if time.time() - p.get("tso_chk", 0) > config.TRADE_TSO_CHECK_MIN * 60:
        p["tso_chk"] = time.time()
        st.get("dil", {}).pop(sym, None)            # force a fresh read
        d = dilution.summary(sym, st, str(now.date()))
        if d and p.get("tso0") and d["tso"] > p["tso0"] * (1 + config.DILUTION_ALERT_MIN):
            _sell(run, sym, p["shares"], sell, f"🔴 נוספו מניות ({dilution.fmt(p['tso0'])} → "
                                               f"{dilution.fmt(d['tso'])}) – מדלל פעיל, יציאה מיידית")
            return
    # Level 2: a hole in the buyers right under our stop -> when in profit, lift the stop onto the last solid level
    bk = book(sym)
    if bk and bk["bids"] and p["stop"] >= p["entry"]:
        h = bid_hole(bk, last)
        if h and h[1] < p["stop"] < h[0] < last:      # our stop sits inside the hole
            p["stop"] = h[0]
            _note(run, sym, "hole", f"🕳️ <b>{sym}</b>: Level 2 – {hole_text(h)}. העצירה הועלתה ל-${fmt_price(h[0])}, "
                                    "כדי לצאת לפני שהקונים נגמרים.")
    elif bk and bk["bids"]:
        h = bid_hole(bk, p["stop"])
        if h and h[0] >= p["stop"] * (1 - config.TRADE_L2_NEAR):
            _note(run, sym, "hole", f"⚠️ <b>{sym}</b>: Level 2 – {hole_text(h)}. אם העצירה תיפגע, המכירה עלולה "
                                    "להתבצע נמוך מהמתוכנן.")
    if _heavy_selling(run, sym, p, bars, sell):
        return
    # end of day: make sure we are not too big for the buyers that are there
    if _mins(now) >= config.TRADE_EOD_MIN:
        vol = sum(b["v"] for b in bars)
        cap = int(vol * config.TRADE_EXIT_LIQ_SHARE)
        if vol and p["shares"] > cap > 0 and p["shares"] - cap >= p["shares"] * 0.2:
            if _sell(run, sym, p["shares"] - cap, sell, "המסחר במניה התייבש – מקטין כדי שיהיה למי למכור"):
                return
    # a stock that stands still is normal: hold while there is no danger, up to the maximum
    if _days(p["opened"]) > config.TRADE_MAX_DAYS and _mins(now) >= config.TRADE_EOD_MIN:
        _sell(run, sym, p["shares"], sell, f"מוחזקת יותר מ-{config.TRADE_MAX_DAYS} ימים בלי תזוזה – משחרר את הכסף")


# ---------- reports ----------

def _trades_since(pt, ts):
    return [x for x in pt["closed"] if x["closed"] >= ts]


def _stats(trades):
    if not trades:
        return "אין מכירות."
    wins = [x for x in trades if x["pnl"] > 0.005]
    pnl = sum(x["pnl"] for x in trades)
    avg_r = sum(x["r"] for x in trades) / len(trades)
    return (f"מכירות: {len(trades)} · ברווח: {len(wins)} ({len(wins) / len(trades) * 100:.0f}%)\n"
            f"רווח/הפסד אחרי עמלות: {'+' if pnl >= 0 else '−'}${abs(pnl):,.2f} · ממוצע: {avg_r:+.2f}R")


def portfolio_text(st):
    pt = init(st)
    eq = equity(pt)
    lines = ["💼 <b>התיק הווירטואלי</b>",
             f"שווי: ${eq:,.0f} ({(eq / pt['start'] - 1) * 100:+.1f}% מההתחלה, ${pt['start']:,.0f})",
             f"מזומן: ${pt['cash']:,.0f} · עמלות ששולמו: ${pt['fees']:,.2f}"]
    if pt["pos"]:
        lines.append("\n<b>מניות פתוחות:</b>")
        for s, p in pt["pos"].items():
            ch = (p.get("last", p["entry"]) / p["entry"] - 1) * 100
            lines.append(f"• <b>{s}</b> {p['shares']:,} × ${fmt_price(p['entry'])} · עכשיו ${fmt_price(p.get('last'))} "
                         f"({ch:+.0f}%) · " + ("בונוס · " if p["runner"] else f"🎯 ${fmt_price(p['target'])} · ")
                         + f"🛑 ${fmt_price(p['stop'])}" + (" (בלי הפסד)" if p["stop"] >= p["entry"] else "")
                         + f" · {_days(p['opened']):.0f} ימים")
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
        return "עוד אין מכירות."
    lines = ["📒 <b>10 המכירות האחרונות</b>"]
    for x in pt["closed"][-10:][::-1]:
        when = datetime.fromtimestamp(x["closed"], ET).strftime("%d/%m")
        lines.append(f"{when} <b>{x['sym']}</b>{' (חלקית)' if x.get('part') else ''} ${fmt_price(x['entry'])}→"
                     f"${fmt_price(x['exit'])} {'+' if x['pnl'] >= 0 else '−'}${abs(x['pnl']):,.2f} "
                     f"({x['r']:+.1f}R) · {esc(x['why'])}")
    return "\n".join(lines)


def _reports(run):
    pt, now = init(run.st), et_now()
    day = str(now.date())
    if _session(now) != "post" or pt["day_rep"] == day:
        return
    start_of_day = datetime(now.year, now.month, now.day, tzinfo=ET).timestamp()
    today = _trades_since(pt, start_of_day)
    skips = pt["skips"].get(day, [])
    if today or pt["pos"] or skips:
        lines = ["📊 <b>סיכום יום – מסחר וירטואלי</b>", _stats(today),
                 f"שווי התיק: ${equity(pt):,.0f} ({(equity(pt) / pt['start'] - 1) * 100:+.1f}% מההתחלה)"]
        if pt["pos"]:
            lines.append("\n<b>מוחזקות:</b>")
            for s, p in pt["pos"].items():
                ch = (p.get("last", p["entry"]) / p["entry"] - 1) * 100
                lines.append(f"• {s} {ch:+.0f}% · {_days(p['opened']):.0f} ימים" + (" · בונוס" if p["runner"] else ""))
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
    _quotes.clear()
    _books.clear()
    now = et_now()
    if live():
        _sync_orders(run)
    day = str(now.date())
    pt["dil"] = {s: t for s, t in pt["dil"].items() if _days(t) < 30}
    pt["neg"] = {s: v for s, v in pt["neg"].items() if _days(v[0]) < 30}
    pt["rm"] = {s: v for s, v in pt["rm"].items() if _days(v["ts"]) < 180}
    pt["notes"] = {k: v for k, v in pt["notes"].items() if _days(v) < 2}
    if _session(now) == "open":
        for sym in list(pt["pos"]):
            try:
                _manage(run, sym, pt["pos"][sym])
            except Exception as e:
                log(f"trader: manage failed ({net.short_err(e)})")
        for sym in list(pt["cands"]):
            c = pt["cands"][sym]
            if c["deadline"] is None:
                open_ts = datetime(now.year, now.month, now.day, 9, 30, tzinfo=ET).timestamp()
                c["deadline"] = max(c["ts"], open_ts) + config.TRADE_WINDOW_MIN * 60
            try:
                bought = sym not in pt["pos"] and _buy(run, sym, c)
                if (c.get("why") or "").startswith("Level 2"):
                    c["why_l2"] = c["why"]          # the most telling reason stays in the daily summary
            except Exception as e:
                log(f"trader: buy check failed ({net.short_err(e)})")
                bought = False
            if bought or sym in pt["pos"]:
                pt["cands"].pop(sym, None)
            elif c.get("drop") or time.time() > c["deadline"]:
                pt["skips"].setdefault(day, []).append([sym, c.get("why_l2") or c.get("why") or "לא נמצאה נקודת כניסה טובה"])
                pt["cands"].pop(sym, None)
    else:
        # news that came after the close waits for the next session; expired / old ones are dropped
        for sym, c in list(pt["cands"].items()):
            if c["deadline"] and time.time() > c["deadline"]:
                pt["skips"].setdefault(day, []).append([sym, c.get("why_l2") or c.get("why") or "לא נמצאה נקודת כניסה טובה"])
                pt["cands"].pop(sym, None)
            elif _days(c["ts"]) > 3:
                pt["cands"].pop(sym, None)
    hist = pt["eq_hist"]
    if not hist or time.time() - hist[-1][0] >= config.TRADE_EQ_EVERY_MIN * 60:
        hist.append([int(time.time()), round(equity(pt), 2)])
        del hist[:-2000]
    _reports(run)


# ---------- data for the trading screen (dashboard) ----------

CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{}?interval=5m&range=5d"
_charts = {}   # ticker -> (time, bars); 5-minute bars of the last 5 days for the screen


def chart_bars(sym):
    hit = _charts.get(sym)
    if hit and time.time() - hit[0] < 300:
        return hit[1]
    out = []
    try:
        r = net.get(CHART_URL.format(sym), timeout=15).json()["chart"]["result"][0]
        q = r["indicators"]["quote"][0]
        for i, t in enumerate(r.get("timestamp") or []):
            row = [q[k][i] for k in ("open", "high", "low", "close")]
            if None not in row:
                out.append([t] + [round(x, 6) for x in row] + [q["volume"][i] or 0])
    except Exception as e:
        log(f"trader: chart failed ({net.short_err(e)})")
    _charts[sym] = (time.time(), out)
    return out


def dashboard(st):
    """Everything the trading screen shows (it is stored encrypted next to the bot's memory)."""
    pt = init(st)
    pos = []
    for s, p in pt["pos"].items():
        bars = chart_bars(s)
        pos.append({"sym": s, "shares": p["shares"], "shares0": p.get("shares0", p["shares"]), "entry": p["entry"],
                    "last": p.get("last"), "stop": p["stop"], "target": p["target"], "high": p["high"],
                    "opened": p["opened"], "cat": p["cat"], "score": p["score"], "runner": p["runner"],
                    "be": p["be"], "ref": p.get("ref"), "basis": round(p["basis"], 2), "title": p.get("title", ""),
                    "url": p.get("url", ""), "bars": bars})
    cands = [{"sym": s, "cat": c["cat"], "score": c["score"], "why": c.get("why", ""), "ts": c["ts"],
              "title": c.get("title", ""), "url": c.get("url", "")} for s, c in pt["cands"].items()]
    for row in pos + cands:      # insiders from the daily cache only (no extra SEC calls here)
        hit = st.get("ins", {}).get(row["sym"])
        row["ins"] = insiders.table(hit[1]) if hit else []
    orders = [{"sym": o["sym"], "qty": o["qty"], "price": o["price"], "ts": o["ts"], "side": "BUY"}
              for o in pt["orders"].values()]
    orders += [{"sym": s, "qty": p["selling"]["qty"], "price": p["selling"]["price"], "ts": p["selling"]["ts"],
                "side": "SELL"} for s, p in pt["pos"].items() if p.get("selling")]
    sw = schwab.status() if schwab.enabled() else None
    return {"v": 1, "mode": config.TRADE_MODE, "paused": pt["paused"], "orders": orders,
            "schwab": {"connected": bool(sw and sw.get("connected")), "days_left": sw and sw.get("days_left")},
            "updated": int(time.time()), "start": pt["start"], "cash": round(pt["cash"], 2),
            "equity": round(equity(pt), 2), "fees": round(pt["fees"], 2), "pos": pos, "cands": cands,
            "closed": pt["closed"][-150:], "skips": pt["skips"], "eq": pt["eq_hist"][-1500:],
            "fee": config.TRADE_FEE, "rr": config.TRADE_RR}
