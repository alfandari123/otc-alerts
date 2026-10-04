"""SEC EDGAR: latest filings of OTC companies (8-K, 13D, 14F1, 14C, S-1, Reg A, Form 15...)."""
import re
import time
from datetime import datetime
from urllib.parse import quote

from .. import config, net
from ..util import html_to_text

FEED = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type={}&count=100&start={}&output=atom"
TICKERS = "https://www.sec.gov/files/company_tickers_exchange.json"
FORMS = ["8-K", "SCHEDULE 13D", "SC 14F1", "PRE 14C", "DEF 14C", "10-12G",
         "S-1", "1-A", "RW", "15-12G", "15-15D"]
MAX_PAGES = {"8-K": 4}
BIG_EXCHANGES = {"Nasdaq", "NYSE", "CBOE"}
# Strong control signals are kept even when the SEC has no ticker for the company.
NO_TICKER_KEEP = {"5.01", "5.06"}

ENTRY = re.compile(r"<entry>(.*?)</entry>", re.S)
TITLE = re.compile(r"<title>(.*?)</title>", re.S)
LINK = re.compile(r'<link[^>]*href="([^"]+)"')
SUMMARY = re.compile(r"<summary[^>]*>(.*?)</summary>", re.S)
UPDATED = re.compile(r"<updated>(.*?)</updated>")
ACC = re.compile(r"accession-number=([\d-]+)")
HEAD = re.compile(r"^(?P<form>.+?) - (?P<name>.+) \((?P<cik>\d{10})\) \((?P<role>[^)]+)\)$")
ITEM = re.compile(r"Item (\d+\.\d+)")
DOC = re.compile(r'href="(?:/ix\?doc=)?(/Archives/edgar/data/[^"]+?\.(?:htm|html|txt|xml))"', re.I)


def _get(url):
    time.sleep(0.15)  # SEC fair-access rule: at most 10 requests per second
    return net.get(url, ua=config.SEC_UA, timeout=25)


def _ticker_map():
    m = {}
    for cik, _name, ticker, exch in _get(TICKERS).json()["data"]:
        if ticker:
            m.setdefault(int(cik), []).append((ticker.upper(), exch))
    return m


def fetch(st):
    if not config.SEC_CONTACT:
        raise RuntimeError("missing SEC_CONTACT")
    tickers = _ticker_map()
    watch = set(st["watch"])
    since = (st.get("last_run") or 0) - 900
    found = {}
    for form in FORMS:
        for page in range(MAX_PAGES.get(form, 1)):
            entries = ENTRY.findall(_get(FEED.format(quote(form), page * 100)).text)
            oldest = None
            for e in entries:
                it, ts = _parse(e, tickers, watch)
                oldest = ts if ts else oldest
                if it and it["id"] not in found:
                    found[it["id"]] = it
            if len(entries) < 100 or not oldest or oldest < since:
                break
    return list(found.values())


def _parse(e, tickers, watch):
    m_title, m_link, m_acc = TITLE.search(e), LINK.search(e), ACC.search(e)
    m_upd = UPDATED.search(e)
    if not (m_title and m_link and m_acc):
        return None, None
    ts = None
    if m_upd:
        try:
            ts = datetime.fromisoformat(m_upd.group(1).strip()).timestamp()
        except ValueError:
            pass
    head = HEAD.match(html_to_text(m_title.group(1)))
    if not head or head["role"] in ("Filed by", "Reporting"):
        return None, ts
    form, name, cik = head["form"].strip(), head["name"].strip(), int(head["cik"])
    summary = html_to_text(SUMMARY.search(e).group(1)) if SUMMARY.search(e) else ""
    items = ITEM.findall(summary)

    known = tickers.get(cik, [])
    otc = [t for t, ex in known if ex not in BIG_EXCHANGES]
    listed = len(otc) < len(known)
    if any(t in watch for t, _ in known):
        syms = sorted({t for t, _ in known}, key=lambda t: (t not in watch, len(t)))
    elif otc and not listed:
        syms = sorted(set(otc), key=len)
    elif not known and (NO_TICKER_KEEP & set(items) or form.startswith("SC 14F1")):
        syms = []
    else:
        return None, ts

    item_lines = re.findall(r"Item \d+\.\d+:[^\n]*?(?=Item \d+\.\d+:|$)", summary)
    label = f"SEC {form}" + (f" (סעיפים {', '.join(items)})" if items else "")
    return {
        "id": f"sec:{m_acc.group(1)}",
        "src": "SEC",
        "src_label": label,
        "form": form,
        "items": items,
        "company": name,
        "syms": syms,
        "title": f"{form} – {name}",
        "text": f"{form} filing by {name}. " + " ".join(s.strip() for s in item_lines),
        "url": m_link.group(1),
        "ts": ts or time.time(),
    }, ts


def enrich(it):
    """Adds the filing's main document (and first exhibit, often the press release) for the AI."""
    try:
        idx = _get(it["url"]).text
        docs = []
        for d in DOC.findall(idx):
            if "-index" not in d and d not in docs:
                docs.append(d)
        texts = [html_to_text(_get("https://www.sec.gov" + d).text)[:5000] for d in docs[:2]]
        if texts:
            it["text"] += "\n\n" + "\n---\n".join(texts)
    except Exception:  # the AI can still work from the summary
        pass
