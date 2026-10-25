"""Course claim check (mil-38 "pre-loading"): unusual volume in the days BEFORE a news item = insiders loaded early
and will sell on the news. Recent OTC Markets news (<= $0.05) vs Yahoo daily bars, no AI needed.

usage: python tools/preload_test.py [days=60] [labels.txt from diag movetest, optional]
"""
import re
import statistics as st
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bot import config  # noqa: E402
from bot.sources import otcm  # noqa: E402


def bars(sym):
    q = requests.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?interval=1d&range=6mo",
                     headers={"User-Agent": config.BROWSER_UA}, timeout=20).json()["chart"]["result"][0]
    t, qq = q["timestamp"], q["indicators"]["quote"][0]
    return [(t[i], qq["close"][i], qq["high"][i], qq["volume"][i] or 0) for i in range(len(t)) if qq["close"][i]]


def main(days=60, labels=None):
    lab = {}
    if labels:
        for line in Path(labels).read_text(encoding="utf-8").splitlines():
            m = re.match(r"(\S+)\s+(pump|pop_drop|sell|none|\?)\s", line)
            if m:
                lab[m.group(1)] = m.group(2)
    now = time.time()
    rows = []
    for page in range(1, 40):
        recs = otcm._get("/news-otcapi/news", page=page, pageSize=100, sortOn="releaseDate", sortDir="DESC").json()
        recs = recs.get("records") or []
        rows += recs
        if not recs or otcm._ts(recs[-1].get("releaseDate")) < now - days * 86400:
            break
    seen, out = set(), []
    for r in rows:
        sym, ts = (r.get("symbol") or "").upper(), otcm._ts(r.get("releaseDate"))
        if not sym or sym in seen or not (now - days * 86400 < ts < now - 8 * 86400):
            continue
        seen.add(sym)
        try:
            b = bars(sym)
        except Exception:
            continue
        before = [x for x in b if x[0] < ts - 6 * 3600]
        after = [x for x in b if x[0] >= ts - 6 * 3600]
        if len(before) < 25 or len(after) < 5 or not 0 < before[-1][1] <= 0.05:
            continue
        base = st.mean(x[3] for x in before[-23:-3]) or 1
        pre = st.mean(x[3] for x in before[-3:]) / base          # last 3 days before the news vs the 20 before
        ref = before[-1][1]
        if not any(x[3] for x in before[-23:]) or not after[0][3]:
            continue                                             # dead stock, no trades around the news
        low20 = min(x[1] for x in before[-20:])
        out.append({"sym": sym, "pre": pre, "off": ref / low20 if low20 else 0, "r1": after[0][1] / ref - 1,
                    "max3": max(x[2] for x in after[:3]) / ref - 1, "late": after[4][1] / after[0][1] - 1,
                    "label": lab.get(sym, "")})
        print(f"{sym:6} off {out[-1]['off']:4.1f}x pre {pre:5.1f}x  1d {out[-1]['r1']:+.0%}  max3 {out[-1]['max3']:+.0%}  late {out[-1]['late']:+.0%}"
              f"  {out[-1]['label']}", flush=True)
        time.sleep(0.5)

    def rep(name, g):
        if g:
            print(f"{name:28} n={len(g):3}  1d med {st.median(x['r1'] for x in g):+.0%}  max3 med "
                  f"{st.median(x['max3'] for x in g):+.0%}  late med {st.median(x['late'] for x in g):+.0%}"
                  f"  late mean {st.mean(x['late'] for x in g):+.0%}  late<0 {sum(x['late'] < 0 for x in g) / len(g):.0%}")
    print("\npre = volume of the 3 days before the news / the 20 days before that")
    for lo, hi in [(0, 1.5), (1.5, 3), (3, 10 ** 9)]:
        rep(f"pre {lo}-{hi if hi < 10 ** 8 else 'inf'}x", [x for x in out if lo <= x["pre"] < hi])
    for lo, hi in [(0, 1.5), (3, 10 ** 9)]:
        rep(f"news day +20%, pre {lo}-{hi if hi < 10 ** 8 else 'inf'}x",
            [x for x in out if lo <= x["pre"] < hi and x["r1"] >= 0.2])
        rep(f"pump label, pre {lo}-{hi if hi < 10 ** 8 else 'inf'}x",
            [x for x in out if lo <= x["pre"] < hi and x["label"] == "pump"])


if __name__ == "__main__":
    a = sys.argv[1:]
    main(int(a[0]) if a else 60, a[1] if len(a) > 1 else None)
