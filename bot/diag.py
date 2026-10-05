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


PRN_TRIES = [
    ("browser UA", {"User-Agent": config.BROWSER_UA}),
    ("browser UA + accept", {"User-Agent": config.BROWSER_UA, "Accept": "application/rss+xml,application/xml;q=0.9,*/*;q=0.8",
                             "Accept-Language": "en-US,en;q=0.9"}),
    ("feed reader UA", {"User-Agent": "Mozilla/5.0 (compatible; Feedly/1.0; +http://www.feedly.com/fetcher.html)"}),
    ("python default UA", {}),
    ("curl UA", {"User-Agent": "curl/8.5.0", "Accept": "*/*"}),
]
PRN_URLS = ["https://www.prnewswire.com/rss/news-releases-list.rss",
            "https://www.prnewswire.com/rss/financial-services-latest-news/financial-services-latest-news-list.rss",
            "https://www.prnewswire.com/news-releases/news-releases-list/"]


def prn():
    for url in PRN_URLS:
        for label, headers in PRN_TRIES:
            try:
                r = requests.get(url, headers=headers, timeout=20)
                print(f"PRN {url[29:70]:42} {label:22} {r.status_code} {len(r.content):>7} items={r.text.count('<item>')}")
            except requests.RequestException as e:
                print(f"PRN {url[29:70]:42} {label:22} ERROR {type(e).__name__}")


def gemini():
    """Which Gemini models answer with this key (prints status codes only)."""
    if not config.GEMINI_API_KEY:
        print("Gemini             skipped (no GEMINI_API_KEY)")
        return
    try:
        r = requests.get("https://generativelanguage.googleapis.com/v1beta/models", params={"pageSize": 200},
                         headers={"x-goog-api-key": config.GEMINI_API_KEY}, timeout=30)
        names = [m["name"].split("/")[-1] for m in r.json().get("models", [])
                 if "generateContent" in m.get("supportedGenerationMethods", []) and "flash" in m["name"]]
        print("Gemini models with generateContent:", ", ".join(names))
    except (requests.RequestException, ValueError, KeyError) as e:
        print("Gemini model list ERROR", type(e).__name__)
    url = "https://generativelanguage.googleapis.com/v1beta/models/{}:generateContent"
    body = {"contents": [{"role": "user", "parts": [{"text": 'Answer with JSON {"ok": true}'}]}],
            "generationConfig": {"responseMimeType": "application/json"}}
    for m in config.GEMINI_MODELS:
        t = time.time()
        try:
            r = requests.post(url.format(m), json=body, timeout=60, headers={"x-goog-api-key": config.GEMINI_API_KEY})
            note = "OK" if r.status_code == 200 else r.text[:160].replace("\n", " ")
            print(f"Gemini {m:26} {r.status_code}  {time.time() - t:4.1f}s  {note}")
        except requests.RequestException as e:
            print(f"Gemini {m:26} ERROR {type(e).__name__}")


def ai_item():
    """End-to-end AI check on a public SEC filing (change of control at TMGI, Oct 2026)."""
    import json
    from . import ai
    from .sources import sec
    it = {"src": "SEC", "src_label": "SEC 8-K (items 1.01, 1.02, 3.02, 5.01, 5.02)", "form": "8-K",
          "company": "Transglobal Management Group, Inc.", "syms": ["TMGI"], "title": "8-K – Transglobal Management Group",
          "text": "8-K filing. Item 5.01 Changes in Control of Registrant.",
          "url": "https://www.sec.gov/Archives/edgar/data/1434601/000168316826007548/0001683168-26-007548-index.htm"}
    sec.enrich(it)
    print("AI item text length:", len(it["text"]))
    print("AI result:", json.dumps(ai.analyze(it, 0.0001), ensure_ascii=False, indent=1), "| model:", ai._model,
          "| error:", ai.last_error)


def dil():
    """OTC Markets share-data endpoints (profile -> security id -> share history), 3 tries each."""
    base = "https://backend.otcmarkets.com"
    h = {"User-Agent": config.BROWSER_UA, **OTC_HEADERS}
    known = {"AIBT": 235867, "TIPS": 73661, "TMGI": 417889}
    for sym, sid in known.items():
        for path in (f"/otcapi/company/profile/full/{sym}", f"/otcapi/stock/trade/inside/{sym}?symbol={sym}",
                     f"/gateway/share-data-api/shares?page=1&pageSize=1&secId={sid}",
                     f"/gateway/share-data-api/shares-dilution?page=1&pageSize=1&secId={sid}"):
            res = []
            for _ in range(3):
                try:
                    r = requests.get(base + path, headers=h, timeout=20)
                    res.append(f"{r.status_code}/{'html' if 'html' in r.headers.get('Content-Type', '') else 'json'}")
                except requests.RequestException as e:
                    res.append(type(e).__name__)
                time.sleep(2)
            print(f"DIL {sym} {path.split('?')[0][-40:]:42} {' '.join(res)}")


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if "prn" in sys.argv:
        return prn()
    if "dil" in sys.argv:
        return dil()
    if "ai-item" in sys.argv:
        return ai_item()
    if "ai" in sys.argv:
        return gemini()
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
