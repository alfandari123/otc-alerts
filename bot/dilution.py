"""How many shares a company has, and how fast that number grows (dilution).

Two sources:
- OTC Markets share data (transfer-agent feed, updated daily). Needs the stock's OTC Markets security id,
  which can only be looked up outside the cloud (GitHub's servers are blocked from that lookup), so ids are
  stored in the bot's memory (st["secid"]); tools/resolve_ids.py fills them in for the watchlist.
- SEC filings (cover-page share counts) for companies that report to the SEC - works everywhere.

summary(): dilution picture added to alerts and given to the AI.
watch(): changes in outstanding / authorized shares of watchlist stocks since the last check.
"""
import time
from datetime import date, timedelta

from . import config
from .sources import otcm, sec


def fmt(x):
    if x is None:
        return "?"
    if x >= 1e9:
        return f"{x / 1e9:.1f}".rstrip("0").rstrip(".") + "B"
    if x >= 1e6:
        return f"{x / 1e6:.1f}".rstrip("0").rstrip(".") + "M" if x < 1e7 else f"{x / 1e6:.0f}M"
    return f"{x:,.0f}"


def _json(path, **params):
    """OTC Markets sometimes answers with a 'maintenance' page for a moment: retry once."""
    try:
        return otcm._get(path, **params).json()
    except Exception:
        time.sleep(3)
        return otcm._get(path, **params).json()


def resolve_id(sym):
    """OTC Markets security id (works from a home computer, not from GitHub's servers)."""
    secs = _json(f"/otcapi/company/profile/full/{sym}").get("securities") or []
    sec_ = next((s for s in secs if (s.get("symbol") or "").upper() == sym), secs[0] if secs else None)
    return sec_.get("id") if sec_ else None


def _from_otc(sid):
    hist = _json("/gateway/share-data-api/shares", page=1, pageSize=1, secId=sid)
    dil = _json("/gateway/share-data-api/shares-dilution", page=1, pageSize=1, secId=sid)
    h, d = (hist[0] if hist else {}), (dil[0] if dil else {})
    tso = h.get("tsoShares") or d.get("currentTso")
    if not tso:
        return None
    return {"tso": tso, "auth": h.get("authShares"), "date": h.get("tsoDate") or d.get("currentTsoAsofDate"),
            "chg3": d.get("percentChange3Month"), "chg6": d.get("percentChange6Month"),
            "chg12": d.get("percentChange12Month"), "src": "OTC"}


def _from_sec(sym):
    cik = sec.cik_for(sym)
    if not cik:
        return None
    tso = sec.shares_series(cik, "dei", "EntityCommonStockSharesOutstanding")
    if not tso:
        return None
    last_date, last = tso[-1]
    end = date.fromisoformat(last_date)

    def change(months):
        target = (end - timedelta(days=months * 30.4)).isoformat()
        older = [v for d, v in tso if d <= target]
        # only when there is a data point reasonably close to the target date
        if older and [d for d, _ in tso if d <= target][-1] >= (end - timedelta(days=months * 30.4 + 75)).isoformat():
            return last / older[-1] - 1
        return None

    auth = sec.shares_series(cik, "us-gaap", "CommonStockSharesAuthorized")
    return {"tso": last, "auth": auth[-1][1] if auth else None, "date": last_date,
            "chg3": change(3), "chg6": change(6), "chg12": change(12), "src": "SEC"}


def summary(sym, st, today):
    """Dilution facts for a ticker (cached for the day), or None when there is no data.
    Temporary failures are not cached, so the next alert tries again."""
    cache = st.setdefault("dil", {})
    hit = cache.get(sym)
    if hit and hit[0] == today:
        return hit[1]
    try:
        sid = st.get("secid", {}).get(sym)
        res = _from_otc(sid) if sid else None
        if not res:
            res = _from_sec(sym)
    except Exception:
        return None
    cache[sym] = [today, res]
    return res


def _pct(x):
    return f"{abs(x) * 100:.0f}%"


def lines(d):
    """Hebrew lines for an alert."""
    if not d:
        return []
    when = ".".join(reversed(d["date"].split("-"))) if d.get("date") else ""
    src = "סוכן ההעברות" if d.get("src") == "OTC" else "דוח ל-SEC"
    out = [f"📊 מניות במחזור: {fmt(d['tso'])}" + (f" · מורשות: {fmt(d['auth'])}" if d.get("auth") else "")
           + (f" (לפי {src}, {when})" if when else "")]
    c3, c6, c12 = d.get("chg3"), d.get("chg6"), d.get("chg12")
    main_chg, label = next(((c, lab) for c, lab in ((c6, "בחצי שנה"), (c12, "בשנה"), (c3, "ב-3 חודשים"))
                            if c is not None), (None, ""))
    if main_chg is not None:
        if abs(main_chg) < 0.01 and abs(c3 or 0) < 0.01:
            out.append(f"✅ כמעט אין דילול {label}")
        elif main_chg <= -0.5:
            out.append(f"מספר המניות ירד ב-{_pct(main_chg)} {label} – כנראה ספליט הפוך")
        else:
            verb = "עלו" if main_chg > 0 else "ירדו"
            text = f"המניות במחזור {verb} ב-{_pct(main_chg)} {label}"
            if c3 is not None and label != "ב-3 חודשים":
                text += f" (ב-{_pct(c3)} ב-3 חודשים)"
            if c12 is not None and label != "בשנה":
                text += f", ב-{_pct(c12)} בשנה"
            warn = (c3 or 0) >= config.DILUTION_WARN_3M or (c6 or 0) >= config.DILUTION_WARN_6M
            out.append(("⚠️ דילול: " if warn else "דילול: ") + text)
    if d.get("auth") and d["tso"] / d["auth"] >= config.AUTH_USED_WARN:
        out.append(f"⚠️ ניצלה {d['tso'] / d['auth'] * 100:.0f}% מהמניות המורשות – ייתכן שתבקש להגדיל אותן")
    return out


def ai_text(d):
    """Share structure in English, for the AI."""
    if not d:
        return ""
    parts = [f"{d['tso']:,} shares outstanding (as of {d.get('date')})"]
    if d.get("auth"):
        parts.append(f"{d['auth']:,} authorized")
    for k, label in (("chg3", "3 months"), ("chg6", "6 months"), ("chg12", "12 months")):
        if d.get(k) is not None:
            parts.append(f"change over {label}: {d[k] * 100:+.0f}%")
    return "; ".join(parts)


def watch(st):
    """Changes in outstanding / authorized shares of watchlist stocks since the last check.
    Returns a list of (ticker, kind, old, new, date, source) - kind is "tso" or "auth"."""
    last = st.setdefault("tso", {})
    changes = []
    for sym in st["watch"]:
        try:
            sid = st.get("secid", {}).get(sym)
            now = _from_otc(sid) if sid else None
            if not now:
                now = _from_sec(sym)
        except Exception:
            continue
        if not now:
            continue
        prev = last.get(sym)
        if prev and prev.get("src") == now["src"]:
            for kind in ("tso", "auth"):
                old, new = prev.get(kind), now.get(kind)
                if old and new and abs(new - old) / old >= config.DILUTION_ALERT_MIN:
                    changes.append((sym, kind, old, new, now["date"], now["src"]))
        last[sym] = {"tso": now["tso"], "auth": now.get("auth"), "date": now["date"], "src": now["src"]}
    return changes
