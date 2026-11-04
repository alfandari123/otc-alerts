"""Expert Market risk: how many days until a stock loses its public quote for stale reports.

SEC Rule 15c2-11 (amended 2021): brokers may keep quoting a stock only while its latest published report covers a
period that ended at most 180 days ago. When that runs out, OTC Markets moves the stock to the Expert Market
(grey, retail can't buy). For companies that report to the SEC we read their filing list (data.sec.gov, free,
official) and count: period end of the latest 10-K/10-Q + 180 days. Companies that report only to OTC Markets
(alternative reporting) can't be checked from the cloud, so they get no estimate.

info(): cached per day; lines(): Hebrew alert lines; watch(): new warnings for watchlist stocks.
"""
from datetime import date, timedelta

from . import config
from .sources import sec

PERIODIC = {"10-K", "10-Q", "10-KT", "10-QT", "10-K/A", "10-Q/A", "20-F", "40-F"}
LATE = {"NT 10-K", "NT 10-Q", "NT 10-K/A", "NT 10-Q/A"}
GRACE = 180
STAGES = (60, 30, 14, 7, 0)   # watchlist warning when the days left first drop to one of these


def _calc(cik, today):
    r = sec._get(f"https://data.sec.gov/submissions/CIK{cik:010d}.json").json()["filings"]["recent"]
    rows = list(zip(r["form"], r.get("reportDate", []), r["filingDate"]))
    periods = [rep for f, rep, _ in rows if f in PERIODIC and rep]
    if not periods:
        return None
    last = max(periods)
    deadline = date.fromisoformat(last) + timedelta(days=GRACE)
    filed_since = [fd for f, _, fd in rows if f in PERIODIC]
    late = [(f, fd) for f, _, fd in rows if f in LATE and fd > max(filed_since)]
    return {"period": last, "deadline": deadline.isoformat(), "days": (deadline - today).days,
            "late": late[0] if late else None}


def info(sym, st, today):
    """{"period", "deadline", "days", "late"} or None (not an SEC filer / no data). Cached for the day."""
    cache = st["demo"] = {s: v for s, v in st.get("demo", {}).items() if v[0] == str(today)}
    hit = cache.get(sym)
    if hit and hit[0] == str(today):
        return hit[1]
    try:
        cik = sec.cik_for(sym)
        res = _calc(cik, today) if cik else None
    except Exception:
        return None
    cache[sym] = [str(today), res]
    return res


def _d(iso):
    return ".".join(reversed(iso.split("-")))


def lines(d, always=False):
    """Hebrew lines; by default only when the risk is close (always=True for the watchlist report)."""
    if not d or (not always and d["days"] > config.DEMOTION_SHOW_DAYS and not d["late"]):
        return []
    out = []
    if d["days"] < 0:
        out.append(f"🚫 הדוח האחרון מכסה תקופה שהסתיימה ב-{_d(d['period'])} – עברו יותר מ-{GRACE} יום. "
                   "אם לא הוגש דוח חדש, המניה בסכנה ממשית לרדת ל-Expert Market (אי אפשר לקנות)")
    else:
        icon = "⚠️" if d["days"] <= config.DEMOTION_SHOW_DAYS else "🗓️"
        out.append(f"{icon} עוד {d['days']} ימים עד סכנת Expert Market ({_d(d['deadline'])}), "
                   f"אם החברה לא תגיש דוח חדש. הדוח האחרון: עד {_d(d['period'])}")
    if d["late"]:
        out.append(f"⚠️ החברה הודיעה ל-SEC שהדוח יתאחר ({d['late'][0]}, {_d(d['late'][1])})")
    return out


def watch(st, today):
    """[(ticker, info)] for watchlist stocks that just reached a new warning stage (or filed a late notice)."""
    seen = st.setdefault("demo_alert", {})
    st["demo_alert"] = seen = {s: v for s, v in seen.items() if s in st["watch"]}
    out = []
    for sym in st["watch"]:
        d = info(sym, st, today)
        if not d:
            continue
        stage = next((s for s in reversed(STAGES) if d["days"] <= s), None)
        key = f"{d['period']}|{stage}|{d['late'][1] if d['late'] else ''}"
        prev = seen.get(sym)
        if prev != key:
            seen[sym] = key
            # first look at a stock: stay quiet unless it is already close / late
            if (stage is not None or d["late"]) and (prev is not None or d["days"] <= config.DEMOTION_SHOW_DAYS
                                                     or d["late"]):
                out.append((sym, d))
    return out
