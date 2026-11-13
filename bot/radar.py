"""Radar for the stocks the agent holds: news the main feeds can miss ("under the radar").

Every RADAR_EVERY_MIN minutes, for each open position:
- Google News search for the ticker and the company name (sites, blogs and wires the bot does not read directly);
- SEC filings by the company's SEC number, also when the SEC has no ticker for it (SPYR filed Form 15 in 2023,
  so its new filings would not carry a ticker and the main SEC feed could not link them).
Dilution or bad news sets the agent's flags, and the agent sells at once. Telegram gets only what matters
(dilution, bad news, or a clearly positive event) - ordinary articles are seen by the agent but not sent (13.11).
The first scan of a stock only remembers what already exists (old news is not news).
"""
import html
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote

from . import ai, config, net, rules, trader
from .sources import sec
from .util import esc, log

GNEWS = "https://news.google.com/rss/search?q={}&hl=en-US&gl=US&ceid=US:en"
SUGGEST = "https://efts.sec.gov/LATEST/search-index?keysTyped={}"
SUBMISSIONS = "https://data.sec.gov/submissions/CIK{:010d}.json"
FRESH_DAYS = 3

ITEM = re.compile(r"<item>(.*?)</item>", re.S)
SUFFIX = r"(?:inc|corp|corporation|co|ltd|plc|holdings?|group|company|international|technologies|enterprises)"


def about(sym, company, title, source=""):
    """Is this Google News headline really about the stock? A search for a ticker that is also a name brings other
    things (13.11: HIRU brought every article of the Sri Lankan site "Hiru News", a game and a person named Hiru).
    Yes when the ticker is written as a ticker ($ABCD, (ABCD), OTC: ABCD) or the company's name is in the headline -
    a short one-word name only together with its Inc/Corp word. The " - Source" ending is not part of the headline."""
    t = title[:-len(source) - 3] if source and title.endswith(" - " + source) else title
    if re.search(rf"(?:\$|\(|OTC\w*\s*:\s*){re.escape(sym)}(?![A-Za-z])", t):
        return True
    base = re.sub(rf"(?:[,.]?\s+{SUFFIX}\.?)+$", "", company or "", flags=re.I).strip()
    if len(base) < 3:
        return False
    if len(base.split()) >= 2 or len(base) >= 8:
        return base.lower() in t.lower()
    return bool(re.search(rf"\b{re.escape(base)},?\s+{SUFFIX}\b", t, re.I))


def _tag(block, name):
    m = re.search(rf"<{name}[^>]*>(.*?)</{name}>", block, re.S)
    return html.unescape(m.group(1)).strip() if m else ""


def _news(sym, company):
    q = f'"{sym}"' + (f' OR "{company}"' if company else "")
    out = []
    raw = net.get(GNEWS.format(quote(q)), timeout=20).text
    for b in ITEM.findall(raw):
        title, link = _tag(b, "title"), _tag(b, "link")
        try:
            ts = parsedate_to_datetime(_tag(b, "pubDate")).timestamp()
        except (TypeError, ValueError):
            ts = time.time()
        if not title or time.time() - ts > FRESH_DAYS * 86400:
            continue
        out.append({"id": "g:" + (link or title)[-120:], "src": "PRN", "src_label": _tag(b, "source") or "Google News",
                    "title": title, "text": "", "url": link, "syms": [sym], "ts": ts})
    return out


def _cik(st, sym, company):
    """SEC company number: from the ticker list, else from EDGAR's company-name search."""
    cache = st["radar"]["cik"]
    if sym in cache:
        return cache[sym]
    cik = sec.cik_for(sym)
    if not cik and config.SEC_CONTACT:
        try:
            hits = (sec._get(SUGGEST.format(quote(company or sym))).json().get("hits") or {}).get("hits") or []
            want = re.sub(r"[^a-z0-9]", "", (company or sym).lower())
            for h in hits:
                name = re.sub(r"[^a-z0-9]", "", ((h.get("_source") or {}).get("entity") or "").lower())
                if name.startswith(want) or want.startswith(name[:max(4, len(want))]):
                    cik = int(h["_id"])
                    break
        except Exception as e:
            log(f"radar: SEC name search failed ({net.short_err(e)})")
            return None          # try again next time
    cache[sym] = cik
    return cik


def _filings(st, sym, p):
    cik = _cik(st, sym, p.get("company", ""))
    if not cik:
        return []
    r = sec._get(SUBMISSIONS.format(cik)).json()["filings"]["recent"]
    since = datetime.fromtimestamp(p["opened"] - 86400, timezone.utc).date().isoformat()
    out = []
    for i, form in enumerate(r["form"]):
        if r["filingDate"][i] < since:
            break
        acc = r["accessionNumber"][i]
        out.append({"id": "s:" + acc, "src": "SEC", "src_label": f"SEC {form}", "form": form,
                    "items": [x.strip() for x in (r.get("items", [""] * len(r["form"]))[i] or "").split(",") if x.strip()],
                    "title": f"{form} – {sym}", "text": r.get("primaryDocDescription", [""] * len(r["form"]))[i] or "",
                    "url": f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc.replace('-', '')}/",
                    "syms": [sym], "ts": time.time()})
    return out


def scan(run):
    st = run.st
    pt = trader.init(st)
    rd = st.setdefault("radar", {"last": 0, "seen": {}, "cik": {}})
    held = {**pt["pos"], **(pt.get("atm") or {}).get("pos", {})}          # news + ATM strategies
    if not held or time.time() - rd["last"] < config.RADAR_EVERY_MIN * 60:
        return
    rd["last"] = time.time()
    rd["seen"] = {s: v for s, v in rd["seen"].items() if s in held}
    for sym, p in list(held.items()):
        first = sym not in rd["seen"]
        seen = rd["seen"].setdefault(sym, [])
        items = []
        for fetch in (lambda: _news(sym, p.get("company", "")), lambda: _filings(st, sym, p)):
            try:
                items += fetch()
            except Exception as e:
                log(f"radar: source failed ({net.short_err(e)})")
        for it in items:
            if it["id"] in seen:
                continue
            seen.append(it["id"])
            if first:
                continue
            if it["src"] == "PRN" and not about(sym, p.get("company", ""), it["title"], it["src_label"]):
                continue                 # another "Hiru": not this stock - not news for him or for the agent
            rule = rules.classify(it)
            trader.on_item(st, it, rule)
            dil = set(rule["labels"]) & trader.DIL_LABELS
            good = rule["pol"] > 0 and rule["ps"] >= 2
            if not (dil or rule["pol"] < 0 or good):
                continue                 # his request 13.11: ordinary articles flooded him - the agent still saw it
            he = ai.title_he(it["title"]) if it["src"] == "PRN" else None      # his request: Hebrew, not English
            lines = [f"📡 <b>רדאר – {sym}</b> (מניה שהסוכן מחזיק)", esc(he or it["title"]),
                     f"{esc(it['src_label'])} · <a href=\"{html.escape(it['url'])}\">למקור</a>"]
            if dil:
                lines.append("🔴 נראה כמו דילול: " + esc(" · ".join(dil)) + " – הסוכן יוצא מהמניה")
            elif rule["pol"] < 0:
                lines.append("🔴 חדשה שלילית: " + esc(rule["label"]) + " – הסוכן יוצא מהמניה")
            else:
                lines.append("🟢 חדשה חיובית: " + esc(" · ".join(rule["labels"][:3])))
            run.notify("\n".join(lines))
        rd["seen"][sym] = seen[-300:]
    log(f"radar: {len(held)} positions checked")
