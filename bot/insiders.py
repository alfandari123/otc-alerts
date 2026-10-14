"""Insiders: what the company's officers, directors and big holders bought or sold (SEC Form 4 / Form 144).

Only companies that report to the SEC file these; many sub-penny companies do not (then there is no data).
Codes in Form 4: P = bought on the market, S = sold on the market, C = conversion (often a convertible note
turned into shares), A = grant, M = option exercise, G = gift, F = shares withheld for tax.
"""
import re
import time
from datetime import date, timedelta

from . import net
from .sources import sec

SUBMISSIONS = "https://data.sec.gov/submissions/CIK{:010d}.json"
DOC = "https://www.sec.gov/Archives/edgar/data/{}/{}/{}"
MAX_FORM4 = 8
DAYS = 90

TX = re.compile(r"<nonDerivativeTransaction>(.*?)</nonDerivativeTransaction>", re.S)
CODE_HE = {"P": "קנייה בשוק", "S": "מכירה בשוק", "C": "המרה למניות", "A": "קיבל מניות (מענק)",
           "M": "מימוש אופציות", "G": "מתנה", "F": "מס", "D": "החזיר מניות לחברה", "J": "אחר"}


def _val(block, tag):
    m = re.search(rf"<{tag}>\s*(?:<value>)?\s*([^<]*?)\s*(?:</value>)?\s*</{tag}>", block, re.S)
    return m.group(1).strip() if m else ""


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def _role(doc):
    if _val(doc, "isOfficer") in ("1", "true"):
        return _val(doc, "officerTitle") or "מנהל"
    if _val(doc, "isDirector") in ("1", "true"):
        return "דירקטור"
    if _val(doc, "isTenPercentOwner") in ("1", "true"):
        return "בעל 10%+"
    return "בעל עניין"


def _form4(cik, acc, doc_name):
    raw_name = doc_name.split("/")[-1]      # "xslF345X05/form4.xml" is the rendered page; the data is next to it
    xml = sec._get(DOC.format(cik, acc.replace("-", ""), raw_name)).text
    owner = _val(xml, "rptOwnerName")
    role = _role(xml)
    out = []
    for t in TX.findall(xml):
        code = _val(t, "transactionCode")
        out.append({"date": _val(t, "transactionDate"), "owner": owner, "role": role, "code": code,
                    "shares": _num(_val(t, "transactionShares")), "price": _num(_val(t, "transactionPricePerShare")),
                    "ad": _val(t, "transactionAcquiredDisposedCode")})
    return out


def fetch(sym):
    """{"tx": [...], "f144": [dates]} for the last 90 days, or None when the company does not report to the SEC."""
    cik = sec.cik_for(sym)
    if not cik:
        return None
    r = sec._get(SUBMISSIONS.format(cik)).json()["filings"]["recent"]
    since = (date.today() - timedelta(days=DAYS)).isoformat()
    tx, f144, n = [], [], 0
    for i, form in enumerate(r["form"]):
        if r["filingDate"][i] < since:
            break                                  # newest first
        if form == "144":
            f144.append(r["filingDate"][i])
        elif form in ("4", "4/A") and n < MAX_FORM4:
            n += 1
            try:
                tx += _form4(cik, r["accessionNumber"][i], r["primaryDocument"][i])
            except Exception:
                continue
    return {"tx": tx, "f144": f144}


def get(st, sym):
    """Cached for the day in the bot's memory."""
    cache = st.setdefault("ins", {})
    today = str(date.today())
    hit = cache.get(sym)
    if hit and hit[0] == today:
        return hit[1]
    try:
        data = fetch(sym)
    except Exception:
        return None
    cache[sym] = [today, data]
    for s in [s for s, v in cache.items() if v[0] != today]:
        cache.pop(s)
    return data


def summary(data, days=30):
    """Dollar value bought / sold on the market in the last `days`, conversions and Form 144 notices."""
    if not data:
        return None
    since = (date.today() - timedelta(days=days)).isoformat()
    recent = [t for t in data["tx"] if t["date"] >= since]
    buy = sum(t["shares"] * t["price"] for t in recent if t["code"] == "P")
    sell = sum(t["shares"] * t["price"] for t in recent if t["code"] == "S")
    conv = sum(t["shares"] for t in recent if t["code"] == "C")
    return {"buy": buy, "sell": sell, "conv_shares": conv, "f144": [d for d in data["f144"] if d >= since],
            "n": len(recent)}


def danger(data):
    """Hebrew reason when insiders are getting out, else ''."""
    s = summary(data)
    if not s:
        return ""
    if s["f144"]:
        return f"בעל עניין הודיע על כוונה למכור מניות (Form 144, {s['f144'][0]})"
    if s["conv_shares"] > 0:
        return f"המרה של חוב/אופציות ל-{s['conv_shares']:,.0f} מניות ב-30 הימים האחרונים"
    if s["sell"] >= 5000 and s["sell"] > 2 * s["buy"]:
        return f"בעלי עניין מכרו ב-${s['sell']:,.0f} ב-30 הימים האחרונים"
    return ""


def lines(data):
    """Short Hebrew lines for messages."""
    s = summary(data)
    if not s or not (s["n"] or s["f144"]):
        return []
    out = []
    if s["buy"]:
        out.append(f"🟢 בעלי עניין קנו ב-${s['buy']:,.0f} (30 יום)")
    if s["sell"]:
        out.append(f"🔴 בעלי עניין מכרו ב-${s['sell']:,.0f} (30 יום)")
    if s["conv_shares"]:
        out.append(f"🔴 המרה ל-{s['conv_shares']:,.0f} מניות (30 יום)")
    if s["f144"]:
        out.append("🔴 הודעה על כוונה למכור (Form 144)")
    return out


def table(data, limit=12):
    """Rows for the trading screen."""
    if not data:
        return []
    rows = sorted(data["tx"], key=lambda t: t["date"], reverse=True)[:limit]
    return [{"date": t["date"], "owner": t["owner"], "role": t["role"], "code": t["code"],
             "what": CODE_HE.get(t["code"], t["code"]), "shares": t["shares"], "price": t["price"],
             "ad": t["ad"]} for t in rows] + [{"date": d, "owner": "", "role": "", "code": "144",
                                              "what": "כוונה למכור (144)", "shares": 0, "price": 0, "ad": "D"}
                                             for d in data["f144"][:3]]
