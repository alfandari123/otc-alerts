"""Share-count data from OTC Markets (transfer-agent feed, updated daily) - for every OTC company,
including companies that do not report to the SEC.

summary(): one-line dilution picture added to alerts and given to the AI.
watch(): daily check of the watchlist - alerts when outstanding or authorized shares change.
"""
import time

from . import config
from .sources import otcm


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


def _sec_id(sym, st):
    """OTC Markets security id of a ticker (kept for good: it never changes)."""
    ids = st.setdefault("secid", {})
    if sym not in ids:
        secs = _json(f"/otcapi/company/profile/full/{sym}").get("securities") or []
        sec = next((s for s in secs if (s.get("symbol") or "").upper() == sym), secs[0] if secs else None)
        ids[sym] = sec.get("id") if sec else None
    return ids[sym]


def summary(sym, st, today):
    """Dilution facts for a ticker (cached for the day), or None when there is no data.
    Temporary failures are not cached, so the next alert tries again."""
    cache = st.setdefault("dil", {})
    hit = cache.get(sym)
    if hit and hit[0] == today:
        return hit[1]
    try:
        sid = _sec_id(sym, st)
        res = None
        if sid:
            hist = _json("/gateway/share-data-api/shares", page=1, pageSize=1, secId=sid)
            dil = _json("/gateway/share-data-api/shares-dilution", page=1, pageSize=1, secId=sid)
            h = hist[0] if hist else {}
            d = dil[0] if dil else {}
            res = {
                "tso": h.get("tsoShares") or d.get("currentTso"),
                "auth": h.get("authShares"),
                "date": h.get("tsoDate") or d.get("currentTsoAsofDate"),
                "chg3": d.get("percentChange3Month"),
                "chg6": d.get("percentChange6Month"),
                "chg12": d.get("percentChange12Month"),
            }
            if not res["tso"]:
                res = None
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
    out = [f"📊 מניות במחזור: {fmt(d['tso'])}" + (f" · מורשות: {fmt(d['auth'])}" if d.get("auth") else "")]
    c3, c6, c12 = d.get("chg3"), d.get("chg6"), d.get("chg12")
    if c6 is not None:
        if abs(c6) < 0.01 and abs(c3 or 0) < 0.01:
            out.append("✅ כמעט אין דילול בחצי השנה האחרונה")
        else:
            verb = "עלו" if c6 > 0 else "ירדו"
            text = f"המניות במחזור {verb} ב-{_pct(c6)} בחצי שנה"
            if c3 is not None:
                text += f" (ב-{_pct(c3)} ב-3 חודשים)"
            if c12 is not None:
                text += f", ב-{_pct(c12)} בשנה"
            warn = (c3 or 0) >= config.DILUTION_WARN_3M or c6 >= config.DILUTION_WARN_6M
            out.append(("⚠️ דילול: " if warn else "דילול: ") + text)
    if d.get("auth") and d["tso"] / d["auth"] >= config.AUTH_USED_WARN:
        out.append(f"⚠️ ניצלה {d['tso'] / d['auth'] * 100:.0f}% מהמניות המורשות – ייתכן שתבקש להגדיל אותן")
    return out


def ai_text(d):
    """Share structure in English, for the AI."""
    if not d:
        return ""
    parts = [f"{d['tso']:,} shares outstanding"]
    if d.get("auth"):
        parts.append(f"{d['auth']:,} authorized")
    for k, label in (("chg3", "3 months"), ("chg6", "6 months"), ("chg12", "12 months")):
        if d.get(k) is not None:
            parts.append(f"change over {label}: {d[k] * 100:+.0f}%")
    return "; ".join(parts)


def watch(st):
    """Changes in outstanding / authorized shares of watchlist stocks since the last check.
    Returns a list of (ticker, kind, old, new, date) - kind is "tso" or "auth"."""
    last = st.setdefault("tso", {})
    changes = []
    for sym in st["watch"]:
        try:
            sid = _sec_id(sym, st)
            if not sid:
                continue
            hist = _json("/gateway/share-data-api/shares", page=1, pageSize=1, secId=sid)
        except Exception:
            continue
        if not hist:
            continue
        h = hist[0]
        now = {"tso": h.get("tsoShares"), "auth": h.get("authShares"), "date": h.get("tsoDate")}
        prev = last.get(sym)
        if prev:
            for kind in ("tso", "auth"):
                old, new = prev.get(kind), now.get(kind)
                if old and new and abs(new - old) / old >= config.DILUTION_ALERT_MIN:
                    changes.append((sym, kind, old, new, now["date"]))
        last[sym] = now
    return changes
