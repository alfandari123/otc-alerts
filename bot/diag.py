"""Checks which data sources answer from this machine (prints status codes only, never secrets).

    python -m bot.diag
"""
import sys
import time

import requests

from . import config

OTC_HEADERS = {"Origin": "https://www.otcmarkets.com", "Referer": "https://www.otcmarkets.com/",
               "Accept": "application/json"}
CHECKS = [
    ("SEC EDGAR", "GET", "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&count=10&output=atom", {}),
    ("FINRA", "POST", "https://api.finra.org/data/group/otcMarket/name/otcDailyList", {"json": {"limit": 1}}),
    ("PR Newswire", "GET", "https://www.prnewswire.com/rss/news-releases-list.rss", {}),
    ("Yahoo", "GET", "https://query1.finance.yahoo.com/v8/finance/chart/AAPL?interval=1d&range=5d", {}),
    ("GlobeNewswire", "GET", "https://www.globenewswire.com/RssFeed/orgclass/1/feedTitle/"
                             "GlobeNewswire%20-%20News%20about%20Public%20Companies", {}),
    ("OTC Markets API", "GET", "https://backend.otcmarkets.com/otcapi/company/profile/full/OTCM",
     {"headers": OTC_HEADERS}),
    ("OTC Markets site", "GET", "https://www.otcmarkets.com/stock/OTCM/news", {}),
    ("Accesswire", "GET", "https://www.accessnewswire.com/newsroom", {}),
    ("Newsfile", "GET", "https://www.newsfilecorp.com/", {}),
    ("Business Wire", "GET", "https://www.businesswire.com/newsroom", {}),
]


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    for name, method, url, kw in CHECKS:
        headers = {"User-Agent": config.SEC_UA if name.startswith("SEC") else config.BROWSER_UA}
        headers.update(kw.pop("headers", {}))
        if name.startswith("SEC") and not config.SEC_CONTACT:
            print(f"{name:18} skipped (no SEC_CONTACT)")
            continue
        t = time.time()
        try:
            r = requests.request(method, url, headers=headers, timeout=25, **kw)
            body = r.text[:3000].lower()
            hint = ("MAINTENANCE" if "temporarily unavailable" in body or "maintenance" in body
                    else "BLOCKED?" if "captcha" in body or "access denied" in body or "cloudflare" in body
                    else "rss" if "<rss" in body or "<feed" in body else "json" if body[:1] in "[{" else "html")
            print(f"{name:18} {r.status_code}  {time.time() - t:5.1f}s  {len(r.content):>8} bytes  {hint}")
        except requests.RequestException as e:
            print(f"{name:18} ERROR {type(e).__name__}  {time.time() - t:5.1f}s")


if __name__ == "__main__":
    main()
