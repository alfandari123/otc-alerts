"""Price and daily volume from Yahoo Finance (unofficial, free)."""
from datetime import datetime

from . import config, net
from .util import ET

URL = "https://query1.finance.yahoo.com/v8/finance/chart/{}?interval=1d&range=1mo"

_cache = {}
stats = {"ok": 0, "fail": 0, "err": ""}


def chart(sym):
    if sym in _cache:
        return _cache[sym]
    res = None
    try:
        j = net.get(URL.format(sym), timeout=15).json()
        r = (j.get("chart") or {}).get("result")
        if r:
            r = r[0]
            q = (r.get("indicators") or {}).get("quote") or [{}]
            res = {
                "price": r["meta"].get("regularMarketPrice"),
                "prev": r["meta"].get("chartPreviousClose"),
                "ts": r.get("timestamp") or [],
                "vol": q[0].get("volume") or [],
                "close": q[0].get("close") or [],
            }
        stats["ok"] += 1
    except net.HttpError as e:
        if e.status != 404:          # 404 = unknown symbol, not an outage
            stats["fail"] += 1
            stats["err"] = str(e)
        else:
            stats["ok"] += 1
    except (ValueError, KeyError, IndexError, TypeError):
        pass
    _cache[sym] = res
    return res


def price(syms):
    """First price found among the symbols (a new ticker may not be on Yahoo yet)."""
    for s in syms:
        c = chart(s)
        if c and c["price"]:
            return c["price"]
    return None


def band(p):
    if not p or p <= 0:
        return "unknown"
    return "above" if p > config.MAX_PRICE else "in"


def avg_volume(sym, today, cache):
    """Average daily volume of the last 20 sessions before today (cached for the day)."""
    hit = cache.get(sym)
    if hit and hit[0] == str(today):
        return hit[1]
    c = chart(sym)
    if not c or not c["ts"] or len(c["ts"]) != len(c["vol"]):
        return None
    hist = [v or 0 for t, v in zip(c["ts"], c["vol"]) if datetime.fromtimestamp(t, ET).date() != today][-20:]
    avg = sum(hist) / len(hist) if hist else None
    cache[sym] = [str(today), avg]
    return avg


def volume_spike(sym, today):
    c = chart(sym)
    if not c or not c["ts"] or len(c["ts"]) != len(c["vol"]):
        return None
    if datetime.fromtimestamp(c["ts"][-1], ET).date() != today:
        return None
    vols = [v or 0 for v in c["vol"]]
    closes = [x for x in c["close"][:-1] if x]
    vol_today = vols[-1]
    hist = vols[:-1][-20:]
    avg = sum(hist) / len(hist) if hist else 0
    p = c["price"] or (c["close"][-1] or 0)
    if not p or vol_today * p < config.VOLUME_MIN_DOLLARS:
        return None
    ratio = vol_today / avg if avg > 0 else None
    if ratio is not None and ratio < config.VOLUME_SPIKE_X:
        return None
    prev = closes[-1] if closes else c["prev"]
    chg = (p / prev - 1) * 100 if prev else None
    return {"vol": vol_today, "avg": avg, "ratio": ratio, "price": p, "chg": chg}


def liquidity(sym):
    """Trading activity over the last 20 sessions: {"days", "of", "dollars"} or None."""
    c = chart(sym)
    if not c:
        return None
    if not c["ts"]:                      # Yahoo knows the stock but has no trades this month
        return {"days": 0, "of": 20, "dollars": 0.0}
    if len(c["vol"]) != len(c["ts"]):
        return None
    rows = list(zip(c["vol"], c["close"]))[-20:]
    days = sum(1 for v, _ in rows if v)
    dollars = sum((v or 0) * (p or 0) for v, p in rows) / len(rows) if rows else 0
    return {"days": days, "of": len(rows), "dollars": dollars}


def liquidity_lines(*syms):
    """Hebrew line for alerts: can this stock actually be bought and sold?
    Several tickers may be given (after a ticker change Yahoo still knows only the old one)."""
    q = None
    for sym in syms:
        q = liquidity(sym) if sym else None
        if q and (q["days"] or sym == syms[-1]):
            break
    if not q:
        return []
    if q["days"] == 0:
        return ["\u26a0\ufe0f \u05de\u05e1\u05d7\u05e8: \u05d0\u05d9\u05df \u05e2\u05e1\u05e7\u05d0\u05d5\u05ea \u05d1\u05d7\u05d5\u05d3\u05e9 \u05d4\u05d0\u05d7\u05e8\u05d5\u05df \u2013 \u05db\u05de\u05e2\u05d8 \u05d0\u05d9 \u05d0\u05e4\u05e9\u05e8 \u05dc\u05e7\u05e0\u05d5\u05ea \u05d0\u05d5 \u05dc\u05de\u05db\u05d5\u05e8 (\u05d4\u05de\u05d7\u05d9\u05e8 \u05d1\u05d4\u05ea\u05e8\u05d0\u05d4 \u05d9\u05e9\u05df)"]
    warn = q["days"] < q["of"] / 2 or q["dollars"] < 1000
    return [("\u26a0\ufe0f " if warn else "\ud83d\udcc9 ")
            + f"\u05de\u05e1\u05d7\u05e8: {q['days']} \u05de\u05ea\u05d5\u05da {q['of']} \u05d9\u05de\u05d9\u05dd \u05d0\u05d7\u05e8\u05d5\u05e0\u05d9\u05dd, \u05de\u05d7\u05d6\u05d5\u05e8 \u05de\u05de\u05d5\u05e6\u05e2 ${q['dollars']:,.0f} \u05dc\u05d9\u05d5\u05dd"]
