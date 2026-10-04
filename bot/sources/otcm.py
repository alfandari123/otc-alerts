"""OTC Markets (the website's own API, unofficial).

News (every run): press releases of OTC companies from all wires (OTC Disclosure & News Service, Accesswire,
GlobeNewswire, Newsfile, PR Newswire...), each with its ticker and tier.
Events (every 30 minutes): Caveat Emptor, shell status, tier changes, quote eligibility, venue changes,
halts, paid promotions and SEC suspensions.
"""
import io
import re
import time
from datetime import datetime

from .. import net
from ..util import html_to_text

BASE = "https://backend.otcmarkets.com"
HEADERS = {"Origin": "https://www.otcmarkets.com", "Referer": "https://www.otcmarkets.com/",
           "Accept": "application/json"}
NEWS_PAGE_SIZE = 100
ACTIONS_EVERY_MIN = 30
MAX_AGE_DAYS = 4          # older events are ignored (the bot forgets seen items after 10 days)
problems = []             # feeds that failed in this run (shown in /status)

TIER_RANK = [("QX", 4), ("QB", 3), ("ID", 2), ("PC", 2), ("PL", 1), ("PS", 1), ("EM", 0), ("GM", 0), ("NT", 0)]


def tier_rank(code="", name=""):
    code, name = (code or "").upper(), (name or "").upper()
    for c, rank in TIER_RANK:
        if code == c:
            return rank
    for key, rank in (("OTCQX", 4), ("OTCQB", 3), ("OTCID", 2), ("PINK CURRENT", 2), ("PINK LIMITED", 1),
                      ("EXPERT", 0), ("GREY", 0)):
        if key in name:
            return rank
    return None


def _get(path, **params):
    r = net.get(BASE + path, params=params, headers=dict(HEADERS), timeout=25)
    if r.headers.get("Content-Type", "").startswith("text/html"):
        raise RuntimeError("OTC Markets maintenance")
    return r


def _records(path, **params):
    return _get(path, page=1, **params).json().get("records") or []


def fetch(st):
    problems.clear()
    items = []
    try:
        items += _news()
    except Exception as e:
        problems.append(f"news: {net.short_err(e)}")
    if time.time() - st.get("otc_actions_last", 0) >= ACTIONS_EVERY_MIN * 60:
        items += _actions()
        st["otc_actions_last"] = time.time()
    if problems and not items:
        raise RuntimeError("; ".join(problems))
    return items


# ---------- News ----------

def _ts(s):
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%f%z").timestamp()
    except (TypeError, ValueError):
        return time.time()


def _news():
    items = []
    cutoff = time.time() - MAX_AGE_DAYS * 86400
    for r in _records("/news-otcapi/news", pageSize=NEWS_PAGE_SIZE, sortOn="releaseDate", sortDir="DESC"):
        sym, title = (r.get("symbol") or "").upper(), r.get("title") or ""
        ts = _ts(r.get("releaseDate"))
        if not sym or not r.get("id") or ts < cutoff:
            continue
        tier = r.get("tier") or {}
        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:80] or "news"
        items.append({
            "id": f"otcn:{r['id']}",
            "src": "OTCM",
            "src_label": f"OTC Markets – {r.get('sourceDesc') or 'News'}",
            "news_id": r["id"],
            "type": r.get("typeDesc") or "",
            "tier": tier.get("description") or "",
            "tier_code": tier.get("groupId") or "",
            "company": "",
            "syms": [sym],
            "title": title,
            "text": title,
            "url": f"https://www.otcmarkets.com/stock/{sym}/news/{slug}?id={r['id']}",
            "ts": ts,
        })
    return items


def enrich(it):
    """Adds the full text of the release (or of its attached PDF) for the AI."""
    if it.get("enriched"):
        return
    it["enriched"] = True
    try:
        text = html_to_text(_get("/news-otcapi/news/content/id", newsId=it["news_id"]).text)
        if len(text) < 200:
            docs = _get("/news-otcapi/news/id", newsId=it["news_id"]).json().get("document") or []
            pdfs = [d for d in docs if (d.get("extension") or "").lower() == "pdf"]
            if pdfs:
                text = f"{text} {_pdf_text(pdfs[0]['id'])}".strip()
        if text:
            it["text"] = f"{it['title']}\n{text[:9000]}"
    except Exception:
        pass


def _pdf_text(doc_id):
    from pypdf import PdfReader
    r = net.get(BASE + "/news-otcapi/news/document/content/id", params={"id": doc_id},
                headers=dict(HEADERS), timeout=40)
    if len(r.content) > 15_000_000:
        return ""
    reader = PdfReader(io.BytesIO(r.content))
    return " ".join((p.extract_text() or "") for p in reader.pages[:4])


# ---------- Events ----------

def _date(r, *keys):
    for k in keys:
        if r.get(k):
            return r[k] / 1000
    return 0


def _event(kind, r, ts, signal, details):
    sym = (r.get("symbol") or r.get("newSymbol") or "").upper()
    return {
        "id": f"otca:{kind}:{sym}:{int(ts)}:{signal[2]}",
        "src": "OTCCA",
        "src_label": "OTC Markets – אירועי מסחר",
        "reason": signal[2],
        "signal": signal,
        "company": r.get("companyName") or r.get("issueName") or "",
        "syms": [sym],
        "title": signal[2],
        "text": "\n".join(d for d in details if d),
        "url": f"https://www.otcmarkets.com/stock/{sym}/overview",
        "ts": ts,
    }


def _tier_line(r):
    return f"דרגה נוכחית: {r['tierName']}" if r.get("tierName") else ""


def _ce(r, ts):
    removed = r.get("newFlag") == "N" or r.get("newValue") == "N"
    sig = ((+1, 3, "הוסר סימן הגולגולת (Caveat Emptor)", True) if removed
           else (-1, 3, "נוסף סימן גולגולת (Caveat Emptor)", False))
    return _event("ce", r, ts, sig, [_tier_line(r)])


def _shell(r, ts):
    if r.get("oldValue") == "Y" and r.get("newValue") == "N":
        sig = (+1, 3, "הוסר סטטוס Shell (חברת מעטפת)", True)
    elif r.get("newValue") == "Y":
        sig = (-1, 2, "החברה סומנה כ-Shell (חברת מעטפת)", False)
    else:
        return None
    return _event("shell", r, ts, sig, [_tier_line(r)])


def _tier(r, ts):
    old_name, new_name = r.get("oldTierName") or "", r.get("newTierName") or ""
    old = tier_rank(r.get("oldTierCode"), old_name)
    new = tier_rank(r.get("newTierCode"), new_name)
    if old is None or new is None or old == new:
        return None
    move = f"מ-{old_name} ל-{new_name}"
    if new > old:
        sig = (+1, 3 if new >= 2 and old <= 1 or new - old >= 2 else 2, f"שדרוג דרגת מסחר {move}", True)
    else:
        sig = (-1, 2, f"ירידה בדרגת המסחר {move}", False)
    return _event("tier", r, ts, sig, [])


def _pqe(r, ts):
    if r.get("isQualified") and r.get("tierCode") == "EM":
        sig = (+1, 3, "קיבלה זכאות לציטוט מחירים – יכולה לצאת מה-Expert Market", True)
    elif r.get("isQualified"):
        sig = (+1, 2, "קיבלה זכאות לציטוט מחירים (Piggyback)", True)
    else:
        sig = (-1, 2, "איבדה זכאות לציטוט מחירים", False)
    return _event("pqe", r, ts, sig, [_tier_line(r)])


def _venue(r, ts):
    old, new = r.get("oldVenueName") or "", r.get("newVenueName") or ""
    if r.get("oldVenueType") == "GM" and r.get("newVenueType") != "GM":
        sig = (+1, 2, f"יצאה מהשוק האפור ומתחילה להיסחר ב-{new}", True)
    elif r.get("newVenueType") == "GM":
        sig = (-1, 2, "עברה לשוק האפור (Grey Market)", False)
    else:
        sig = (0, 1, f"שינוי זירת מסחר מ-{old} ל-{new}", False)
    return _event("venue", r, ts, sig, [_tier_line(r)])


def _halt(r, ts):
    if r.get("isRevoked"):
        sig = (-1, 3, "רישום המניה בוטל (Revoked)", False)
    elif r.get("isSuspended"):
        sig = (-1, 3, "המסחר במניה הושעה (Suspended)", False)
    elif r.get("isHalted"):
        sig = (-1, 3, "המסחר במניה נעצר (Halt)", False)
    else:
        sig = (+1, 2, f"שינוי סטטוס מסחר: {r.get('name') or r.get('actionType') or ''}", False)
    return _event("halt", r, ts, sig, [_tier_line(r)])


def _promo(r, ts):
    sig = (-1, 2, "קידום בתשלום למניה (סימן אפשרי לפאמפ)", False)
    details = [_tier_line(r)]
    if r.get("promoStartDate"):
        details.append(f"הקידום התחיל: {datetime.fromtimestamp(r['promoStartDate'] / 1000):%d/%m/%Y}")
    return _event("promo", r, ts, sig, details)


def _susp(r, ts):
    status = r.get("currentStatus") or "Suspended"
    return _event("susp", r, ts, (-1, 3, f"SEC: השעיה / ביטול רישום ({status})", False), [_tier_line(r)])


FEEDS = [
    ("/otcapi/corp-actions/ce-changes", ("changeDate",), _ce),
    ("/compliance-otcapi/statistics/shell-status-changes", ("shellChangeDate", "changeDate"), _shell),
    ("/otcapi/corp-actions/tier-changes", ("changeDate", "effectiveDate"), _tier),
    ("/otcapi/corp-actions/proprietary-quote-eligible", ("changeDate",), _pqe),
    ("/otcapi/corp-actions/venue-changes", ("changeDate",), _venue),
    ("/otcapi/corp-actions/halts", ("changeDate",), _halt),
    ("/compliance-otcapi/statistics/promotions", ("latestPromoDate", "promoStartDate"), _promo),
    ("/compliance-otcapi/statistics/suspensions-revocations", ("effectiveDate",), _susp),
]


def _actions():
    items = []
    cutoff = time.time() - MAX_AGE_DAYS * 86400
    for path, date_keys, parse in FEEDS:
        try:
            for r in _records(path, pageSize=50):
                ts = _date(r, *date_keys)
                if ts < cutoff or not (r.get("symbol") or r.get("newSymbol")):
                    continue
                it = parse(r, ts)
                if it:
                    items.append(it)
        except Exception as e:
            problems.append(f"{path.rsplit('/', 1)[-1]}: {net.short_err(e)}")
    return items


# ---------- Market data ----------

def volume_leaders(pages=2):
    """Most active OTC stocks today by number of shares (delayed 15 minutes); 50 per page at most."""
    out = []
    for page in range(1, pages + 1):
        out += _get("/otcapi/market-data/active/current", page=page, pageSize=50,
                    sortOn="volume").json().get("records") or []
    return out
