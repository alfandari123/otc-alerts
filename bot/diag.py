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


def movetest(limit=80, days=30, max_price=0.05):
    """Course news taxonomy check: the AI labels recent OTC Markets news pump / pop_drop / sell / none, then we look
    at what the price really did 1, 3 and 5 trading days later (Yahoo daily bars)."""
    import statistics
    from . import ai
    from .sources import otcm
    from .util import fmt_price
    now = time.time()
    rows = []
    for page in range(1, 25):
        recs = otcm._get("/news-otcapi/news", page=page, pageSize=100, sortOn="releaseDate", sortDir="DESC").json()
        recs = recs.get("records") or []
        rows += recs
        if not recs or otcm._ts(recs[-1].get("releaseDate")) < now - days * 86400:
            break
    seen, out = set(), []
    for r in rows:
        sym, ts = (r.get("symbol") or "").upper(), otcm._ts(r.get("releaseDate"))
        if not sym or sym in seen or not (now - days * 86400 < ts < now - 4 * 86400):
            continue
        try:
            q = requests.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?interval=1d&range=3mo",
                             headers={"User-Agent": config.BROWSER_UA}, timeout=20).json()["chart"]["result"][0]
            t, c, h = q["timestamp"], q["indicators"]["quote"][0]["close"], q["indicators"]["quote"][0]["high"]
        except Exception:
            continue
        bars = [(t[i], c[i], h[i]) for i in range(len(t)) if c[i] is not None]
        before = [b for b in bars if b[0] < ts - 6 * 3600]
        after = [b for b in bars if b[0] >= ts - 6 * 3600]
        if not before or len(after) < 5 or not 0 < before[-1][1] <= max_price:
            continue
        seen.add(sym)
        it = {"id": r["id"], "src": "OTCM", "src_label": "OTC Markets news", "news_id": r["id"], "syms": [sym],
              "tier": (r.get("tier") or {}).get("description") or "", "company": "", "title": r.get("title") or "",
              "text": r.get("title") or ""}
        otcm.enrich(it)
        res = ai.analyze(it, before[-1][1]) or {}
        ref = before[-1][1]
        ret = lambda k: after[k - 1][1] / ref - 1
        out.append({"sym": sym, "move": res.get("move") or "?", "score": res.get("score"), "r1": ret(1),
                    "r3": ret(3), "r5": ret(5), "up3": max(b[2] or 0 for b in after[:3]) / ref - 1,
                    "late": after[4][1] / after[0][1] - 1,  # bought at the news day's close, held 4 more days
                    "title": it["title"][:70], "ref": ref})
        print(f"{sym:6} {out[-1]['move']:8} s{out[-1]['score']} ref {fmt_price(ref)}  1d {ret(1):+.0%}  3d {ret(3):+.0%}"
              f"  5d {ret(5):+.0%}  max3 {out[-1]['up3']:+.0%}  late {out[-1]['late']:+.0%}  | {out[-1]['title']}", flush=True)
        if len(out) >= limit:
            break
        time.sleep(1)
    print("\nby move (median 1d / 3d / 5d, max up in 3 days, share up after 5 days):")
    for m in ("pump", "pop_drop", "sell", "none", "?"):
        g = [x for x in out if x["move"] == m]
        if g:
            med = lambda k: statistics.median(x[k] for x in g)
            print(f"  {m:8} n={len(g):3}  {med('r1'):+.0%} / {med('r3'):+.0%} / {med('r5'):+.0%}  max3 {med('up3'):+.0%}"
                  f"  up5 {sum(x['r5'] > 0 for x in g) / len(g):.0%}  late {med('late'):+.0%}")
    print("model:", ai._model, "| error:", ai.last_error)


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
    from .dilution import _from_sec
    for sym in known:
        try:
            print(f"DIL SEC {sym}: {_from_sec(sym)}")
        except Exception as e:
            print(f"DIL SEC {sym}: ERROR {type(e).__name__} {e}")


def ins():
    """Insider data (Form 4 / 144) for a few OTC SEC filers - prints counts only (the logs are public)."""
    from . import insiders
    from .sources import sec
    rows = [r for r in sec._get(sec.TICKERS).json()["data"] if r[3] == "OTC"]
    found = 0
    for _cik, _name, sym, _ex in rows[:300]:
        try:
            data = insiders.fetch(sym)
        except Exception as e:
            print("INS error", type(e).__name__, e)
            continue
        if data and (data["tx"] or data["f144"]):
            found += 1
            s = insiders.summary(data, days=90)
            codes = sorted({t["code"] for t in data["tx"]})
            print(f"INS #{found}: {len(data['tx'])} tx, codes {codes}, buy ${s['buy']:,.0f}, sell ${s['sell']:,.0f}, "
                  f"conv {s['conv_shares']:,.0f}, f144 {len(data['f144'])}, danger={bool(insiders.danger(data))}, "
                  f"sample={ {k: v for k, v in data['tx'][0].items() if k not in ('owner',)} if data['tx'] else None}")
        if found >= 5:
            break
    print("INS done, companies with insider filings:", found)


def deal_check(url):
    """Share facts read from one real filing (bot/deal.py)."""
    from . import deal
    from .sources import sec
    it = {"url": url, "text": "", "items": ["2.01", "3.02", "5.01"]}
    sec.enrich(it)
    print("DEAL full text chars:", len(it.get("full", "")))
    f = deal.facts(it.get("full", ""), it["items"])
    print("DEAL facts:", f, "| kind:", deal.kind(f))
    for line in deal.lines(f, 5044334):
        print("DEAL line:", line)


def radar_check(sym, company):
    """The radar's sources for one stock (bot/radar.py) - counts and titles only."""
    import time as _t
    from . import radar
    st = {"radar": {"last": 0, "seen": {}, "cik": {}}}
    print("RADAR cik:", radar._cik(st, sym, company))
    p = {"opened": _t.time() - 30 * 86400, "company": company}
    for f in radar._filings(st, sym, p)[:5]:
        print("RADAR filing:", f["src_label"], f["title"], f["url"])
    for n in radar._news(sym, company)[:5]:
        print("RADAR news:", n["src_label"], "|", n["title"][:100])


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if "radar" in sys.argv:
        return radar_check(sys.argv[2], " ".join(sys.argv[3:]))
    if "deal" in sys.argv:
        return deal_check(sys.argv[-1])
    if "ins" in sys.argv:
        return ins()
    if "movetest" in sys.argv:
        nums = [int(a) for a in sys.argv if a.isdigit()]  # optional: movetest <limit> <days>
        return movetest(*nums[:2])
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
