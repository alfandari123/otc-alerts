"""PR Newswire RSS: press releases that mention an OTC ticker, e.g. "(OTC Pink: ABCD)".

The feed only has a short teaser, so every new release is opened once to look for an OTC ticker.
"""
import hashlib
import re
import time
from email.utils import parsedate_to_datetime

from .. import net
from ..util import html_to_text

FEEDS = [
    "https://www.prnewswire.com/rss/news-releases-list.rss",
    "https://www.prnewswire.com/rss/financial-services-latest-news/financial-services-latest-news-list.rss",
    "https://www.prnewswire.com/rss/financial-services-latest-news/acquisitions-mergers-and-takeovers-list.rss",
]
MAX_FETCH = 40          # articles opened per run
CHECKED_DAYS = 3
ITEM = re.compile(r"<item>(.*?)</item>", re.S)
TICKER = re.compile(r"(?i:\b(?:OTC\s?(?:Markets|Pink|QB|QX|ID)?|OTCMKTS|OTCQB|OTCQX|Pink(?:\s?Sheets)?)"
                    r"\s*(?:[:\-]|Ticker:?|Symbol:?)\s*)\$?([A-Z]{2,5})\b")


def _tag(raw, name):
    m = re.search(rf"<{name}[^>]*>(.*?)</{name}>", raw, re.S)
    return html_to_text(m.group(1)) if m else ""


def tickers_in(text):
    return list(dict.fromkeys(TICKER.findall(text)))


def _article(url):
    text = html_to_text(net.get(url).text)
    i = text.find("/PRNewswire/")
    return text[i:i + 6000] if i >= 0 else ""


def fetch(st):
    now = time.time()
    checked = {k: v for k, v in st.get("prn_checked", {}).items() if v[0] > now - CHECKED_DAYS * 86400}
    st["prn_checked"] = checked     # release key -> [time checked, OTC tickers found]
    out, seen_keys, fetched = [], set(), 0
    for url in FEEDS:
        for raw in ITEM.findall(net.get(url).text):
            link = _tag(raw, "link")
            key = hashlib.md5((_tag(raw, "guid") or link).encode()).hexdigest()[:16]
            if key in seen_keys:
                continue
            seen_keys.add(key)
            title, desc = _tag(raw, "title"), _tag(raw, "description")
            body = ""
            if key in checked:
                syms = checked[key][1]
            else:
                syms = tickers_in(f"{title} {desc}")
                if not syms:
                    if fetched >= MAX_FETCH:
                        continue
                    fetched += 1
                    try:
                        body = _article(link)
                    except net.HttpError:
                        continue
                    syms = tickers_in(body[:4000])
                checked[key] = [now, syms]
            if not syms:
                continue
            try:
                ts = parsedate_to_datetime(_tag(raw, "pubDate")).timestamp()
            except (TypeError, ValueError):
                ts = now
            out.append({
                "id": "prn:" + key,
                "src": "PRN",
                "src_label": "PR Newswire",
                "company": "",
                "syms": syms,
                "title": title,
                "text": body or desc,
                "url": link,
                "ts": ts,
                "enriched": bool(body),
            })
    return out


def enrich(it):
    """Adds the start of the full press release for the AI."""
    if it.get("enriched"):
        return
    try:
        it["text"] = _article(it["url"]) or it["text"]
        it["enriched"] = True
    except Exception:
        pass
