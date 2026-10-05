"""One run of the OTC alerts bot (GitHub Actions starts it every ~5 minutes).

Local testing (prints alerts instead of sending them):
    python -m bot.main --dry
    python -m bot.main --dry --all --watch ABCD,EFGH     # treat everything as new
"""
import argparse
import html
import re
import sys
import time

from . import ai, config, dilution, market, rules, state, telegram
from .net import short_err
from .sources import finra, otcm, prnews, sec
from .util import esc, et_now, fmt_price, il_time, log

SOURCES = [("OTC Markets", otcm), ("SEC", sec), ("FINRA", finra), ("PR Newswire", prnews)]
ENRICH = {"SEC": sec.enrich, "PRN": prnews.enrich, "OTCM": otcm.enrich}
TEMPLATED = ("FINRA", "OTCCA")       # events with a fixed message (no AI needed)
HIGH_TIERS = ("QX", "QB")            # OTCQX/OTCQB require a bid of $0.01 or more
TICKER_OK = re.compile(r"^[A-Z][A-Z0-9.]{0,9}$")
ICON = {"positive": "🟢", "negative": "🔴", "neutral": "⚪"}

HELP = """🤖 <b>בוט התראות OTC</b>

<b>פקודות:</b>
/add ABCD EFGH – הוספת מניות לרשימה שלך
/remove ABCD – הסרת מניה מהרשימה
/list – הרשימה שלך
/status – מצב הבוט והמקורות
/test – הודעת ניסיון
/help – העזרה הזו
אפשר גם בעברית: <code>הוסף ABCD</code> · <code>הסר ABCD</code> · <code>רשימה</code>

<b>סימנים בהתראות:</b>
🟢 חדשה חיובית · 🔴 חדשה שלילית · ⚪ ניטרלית · 📈 ווליום חריג · ⭐ מניה מהרשימה שלך

⏱️ הבוט בודק כל 5-15 דקות, ולכן גם התשובה לפקודה מגיעה תוך כמה דקות."""

WELCOME = "👋 שלום! הבוט מחובר אליך, ומעכשיו הוא ישלח לך התראות.\n\n"

SAMPLE = """🧪 <b>הודעת ניסיון</b> – כך תיראה התראה:

🟢 <b>ABCD</b> · $0.0008
<b>החלפת שליטה</b> · ציון 8/10
Example Holdings Inc.
בעלי שליטה חדשים רכשו את מניות השליטה ומונה מנכ"ל חדש. החברה מודיעה על כוונה למיזוג עם חברה פרטית.
⚠️ אין עדיין פרטים על החברה הממוזגת.
📄 SEC 8-K (סעיפים 5.01, 5.02) · למקור · OTC Markets"""


class Defer(Exception):
    """The item waits for the next run (this run's AI budget is used up)."""


class Run:
    def __init__(self, st, dry):
        self.st, self.dry = st, dry
        self.start = time.time()
        self.sent = 0

    def notify(self, text):
        if self.dry:
            print("\n" + "-" * 60 + "\n" + text, flush=True)
            self.sent += 1
            return True
        if not self.st["owner"]:
            return False
        try:
            telegram.send(self.st["owner"], text)
        except Exception as e:
            log(f"Telegram send failed: {short_err(e)}")
            return False
        self.sent += 1
        return True


# ---------- Telegram commands ----------

def _tickers(args):
    out = []
    for a in args:
        a = a.strip().upper().lstrip("$")
        if TICKER_OK.match(a) and a not in out:
            out.append(a)
    return out


def list_text(st):
    if not st["watch"]:
        return "📋 הרשימה שלך ריקה. הוספה: <code>/add ABCD</code>"
    return f"📋 <b>הרשימה שלך</b> ({len(st['watch'])}):\n" + " · ".join(sorted(st["watch"]))


def status_text(st):
    lines = ["📊 <b>מצב הבוט</b>"]
    if st["last_run"]:
        lines.append(f"ריצה אחרונה: {il_time(st['last_run'])} (שעון ישראל)")
    lines.append(f"מניות ברשימה: {len(st['watch'])}")
    lines.append(f"התראות היום: {st['sent']['n']}")
    if config.GEMINI_API_KEY:
        model = f" · {st['ai']['model']}" if st["ai"].get("model") else ""
        lines.append(f"שימוש ב-AI היום: {st['ai']['n']}/{config.AI_DAILY_LIMIT}{model}")
    lines += ["", "<b>מקורות:</b>"]
    for name in ("OTC Markets", "SEC", "FINRA", "PR Newswire", "Yahoo", "AI"):
        h = st["health"].get(name)
        if h:
            lines.append(f"✅ {name}" if not h.get("err") else f"❌ {name} – {esc(h['err'])}")
    if not config.GEMINI_API_KEY:
        lines.append("⚪ AI – לא הוגדר מפתח")
    return "\n".join(lines)


def command(st, text):
    parts = text.replace(",", " ").split()
    cmd, args = parts[0].lower().split("@")[0], parts[1:]
    if cmd in ("/add", "add", "הוסף"):
        syms = _tickers(args)
        if not syms:
            return "כתוב כך: <code>/add ABCD EFGH</code>"
        room = max(0, config.WATCHLIST_MAX - len(st["watch"]))
        added = [s for s in syms if s not in st["watch"]][:room]
        st["watch"] += added
        head = f"✅ נוספו: {', '.join(added)}" if added else "המניות כבר ברשימה (או שהרשימה מלאה)."
        return head + "\n\n" + list_text(st)
    if cmd in ("/remove", "/del", "/delete", "remove", "הסר", "מחק"):
        removed = [s for s in _tickers(args) if s in st["watch"]]
        st["watch"] = [s for s in st["watch"] if s not in removed]
        head = f"🗑️ הוסרו: {', '.join(removed)}" if removed else "המניות האלו לא ברשימה."
        return head + "\n\n" + list_text(st)
    if cmd in ("/list", "list", "רשימה"):
        return list_text(st)
    if cmd in ("/status", "status", "מצב", "סטטוס"):
        return status_text(st)
    if cmd in ("/test", "test", "בדיקה"):
        return SAMPLE
    if cmd in ("/start", "/help", "help", "עזרה"):
        return HELP
    return "לא הבנתי 🙂\n\n" + HELP


def handle_commands(run):
    st = run.st
    if not config.TELEGRAM_TOKEN:
        return
    try:
        ups = telegram.updates(st["tg_offset"])
    except Exception as e:
        log(f"Telegram getUpdates failed: {short_err(e)}")
        return
    for u in ups:
        st["tg_offset"] = u["update_id"] + 1
        msg = u.get("message") or {}
        chat, text = msg.get("chat") or {}, (msg.get("text") or "").strip()
        if not text or chat.get("type") != "private":
            continue
        if st["owner"] is None and text.startswith("/start"):
            st["owner"] = chat["id"]
            log("owner registered")
            reply = WELCOME + HELP
        elif chat.get("id") != st["owner"]:
            continue
        else:
            reply = command(st, text)
        try:
            telegram.send(chat["id"], reply)
        except Exception as e:
            log(f"Telegram reply failed: {short_err(e)}")


# ---------- Health of the data sources ----------

def set_health(st, name, err):
    h = st["health"].setdefault(name, {"ok": time.time(), "err": "", "warned": False})
    if err:
        h["err"] = err
    else:
        h["ok"], h["err"] = time.time(), ""


def health_warnings(run):
    for name, h in run.st["health"].items():
        down = time.time() - h.get("ok", 0)
        if h.get("err") and not h.get("warned") and down > config.SOURCE_DOWN_WARN_HOURS * 3600:
            if run.notify(f"⚠️ המקור <b>{name}</b> לא עובד כבר {int(down // 3600)} שעות ({esc(h['err'])}).\n"
                          "הבוט ממשיך לעבוד עם שאר המקורות."):
                h["warned"] = True
        elif not h.get("err") and h.get("warned"):
            if run.notify(f"✅ המקור <b>{name}</b> חזר לעבוד."):
                h["warned"] = False


# ---------- News items ----------

def collect(run):
    items = []
    for name, mod in SOURCES:
        try:
            got = mod.fetch(run.st)
            items += got
            problems = getattr(mod, "problems", None)   # some feeds of the source failed
            set_health(run.st, name, "; ".join(problems)[:120] if problems else None)
            log(f"{name}: {len(got)} items" + (f" (partial: {len(problems)} feeds failed)" if problems else ""))
        except Exception as e:
            set_health(run.st, name, short_err(e))
            log(f"{name}: FAILED ({short_err(e)})")
    return items


def ai_available(st):
    return (ai.usable() and st["ai"]["n"] < config.AI_DAILY_LIMIT
            and st["ai"].get("dead_day") != st["ai"]["day"])


def _watch_hits(syms, watch):
    """Tickers on the watchlist (a temporary 5th letter D after a reverse split still counts)."""
    return [s for s in syms if s in watch or (len(s) == 5 and s.endswith("D") and s[:-1] in watch)]


def _dil(st, syms):
    return dilution.summary(syms[0], st, str(et_now().date())) if syms else None


def evaluate(run, it):
    """Returns the alert text, or None when the item is not worth an alert."""
    st = run.st
    hit = _watch_hits(it["syms"], set(st["watch"]))
    rule = rules.classify(it)
    syms = hit or it["syms"]
    if not hit and not rule["candidate"]:
        # A plain headline can hide a big story: read the full release when the stock is in the price band.
        if it["src"] not in ("OTCM", "PRN") or not syms or it.get("tier_code") in HIGH_TIERS:
            return None
        if market.band(market.price(syms[:2])) != "in":
            return None
        ENRICH[it["src"]](it)
        rule = rules.classify(it)
        if not rule["candidate"]:
            return None
    if not hit and it["src"] == "OTCM" and syms:
        # OTC Markets files a release under every company it mentions: skip it when it is about another one.
        ENRICH["OTCM"](it)
        named = prnews.tickers_in(it["text"][:3000])
        if named and syms[0] not in named:
            return None
    price = market.price(syms[:2]) if syms else None
    band = market.band(price)
    if not hit and band == "above" and rule["ps"] < 3:
        return None

    if it["src"] in TEMPLATED:
        strong = rule["ps"] >= 3 and (price or 0) <= config.EVENT_ABOVE_RANGE_MAX_PRICE
        if hit or band == "in" or strong:
            return action_message(it, rule, syms, price, bool(hit), _dil(st, syms))
        return None

    res = None
    if ai_available(st):
        if ai.calls >= config.AI_PER_RUN_LIMIT or time.time() - run.start > config.RUN_DEADLINE_SEC:
            raise Defer
        if it["src"] in ENRICH:
            ENRICH[it["src"]](it)
        it["shares"] = dilution.ai_text(_dil(st, syms))
        res = ai.analyze(it, price)
        if res:
            st["ai"]["n"] += 1
            st["ai"]["model"] = ai._model or ""
            if not syms and res["ticker"]:
                syms = [res["ticker"]]
                price = market.price(syms)
                band = market.band(price)
        elif ai.all_quota_dead():
            st["ai"]["dead_day"] = st["ai"]["day"]

    if res:
        sentiment, score = res["sentiment"], res["score"]
    else:
        sentiment = {1: "positive", -1: "negative", 0: "neutral"}[rule["pol"]]
        score = rules.fallback_score(rule)
    if not hit:
        need = {"in": config.SCORE_IN_RANGE, "unknown": config.SCORE_UNKNOWN_PRICE,
                "above": config.SCORE_ABOVE_RANGE}[band]
        if sentiment != "positive" or score < need:
            return None
    return alert_message(it, rule, res, syms, price, bool(hit), sentiment, score, _dil(st, syms))


def _head(icon, sym, price, hit):
    head = f"{icon} <b>{esc(sym) or 'טיקר לא ידוע'}</b>"
    if price:
        head += f" · ${fmt_price(price)}"
    if hit:
        head += " · ⭐ ברשימה שלך"
    return head


def _links(it, sym):
    links = f'<a href="{html.escape(it["url"])}">למקור</a>'
    otc_page = f"https://www.otcmarkets.com/stock/{sym}/overview"
    if sym and it["url"] != otc_page:
        links += f' · <a href="{otc_page}">OTC Markets</a>'
    return f"📄 {esc(it['src_label'])} · {links}"


def alert_message(it, rule, res, syms, price, hit, sentiment, score, dil=None):
    sym = syms[0] if syms else ""
    lines = [_head(ICON[sentiment], sym, price, hit)]
    if hit and sentiment == "negative":
        lines.append("<b>⚠️ אזהרה – חדשה שלילית למניה שלך</b>")
    cat = (res and res["category_he"]) or rule["label"] or "חדשה"
    lines.append(f"<b>{esc(cat)}</b>" + (f" · ציון {score}/10" if res else ""))
    about = " · ".join(x for x in (it.get("company"), it.get("tier")) if x)
    if about:
        lines.append(esc(about))
    if res and res["summary_he"]:
        lines.append(esc(res["summary_he"]))
    else:
        if it["src"] != "SEC":
            lines.append(esc(it["title"]))
        if rule["labels"]:
            lines.append("זוהה: " + esc(" · ".join(rule["labels"][:3])))
    if res and res["risk_he"]:
        lines.append("⚠️ " + esc(res["risk_he"]))
    lines += dilution.lines(dil)
    lines.append(_links(it, sym))
    if not res:
        lines.append("<i>(בלי AI – זיהוי לפי מילות מפתח)</i>")
    return "\n".join(lines)


def action_message(it, rule, syms, price, hit, dil=None):
    """Fixed-format message for FINRA / OTC Markets events."""
    pol = rule["pol"]
    sym = syms[0] if syms else ""
    lines = [_head("🔴" if pol < 0 else "🟢" if pol > 0 else "⚪", sym, price, hit)]
    if hit and pol < 0:
        lines.append("<b>⚠️ אזהרה – שינוי שלילי במניה שלך</b>")
    lines.append(f"<b>{esc(rule['label'] or it.get('reason', ''))}</b>")
    if it.get("company"):
        lines.append(esc(it["company"]))
    if it.get("text"):
        lines.append(esc(it["text"]))
    lines += dilution.lines(dil)
    lines.append(_links(it, sym))
    return "\n".join(lines)


def _title_key(it):
    """The same press release can arrive from PR Newswire and from OTC Markets."""
    if it["src"] not in ("PRN", "OTCM") or not it["syms"]:
        return None
    return "t:" + it["syms"][0] + ":" + re.sub(r"[^a-z0-9]", "", it["title"].lower())[:60]


def _mark_seen(st, it):
    now = time.time()
    st["seen"][it["id"]] = now
    tk = _title_key(it)
    if tk:
        st["seen"][tk] = now


def process(run, items):
    st = run.st
    new = sorted((it for it in items if it["id"] not in st["seen"]), key=lambda it: it["ts"])
    sources = {it["src"] for it in items}
    if not st["boot"] or (not st["owner"] and not run.dry):
        # First run (or nobody to send to yet): remember what exists, without flooding old news.
        for it in new:
            _mark_seen(st, it)
        st["boot"] = True
        st["warm"] = sorted(set(st["warm"]) | sources)
        log(f"warm-up: {len(new)} existing items marked as seen")
        return
    cold = sources - set(st["warm"])   # a source new to the bot: its existing items are not news
    log(f"new items: {len(new)}" + (f" (warm-up of {len(cold)} new sources)" if cold else ""))
    for it in new:
        tk = _title_key(it)
        if it["src"] in cold or (tk and tk in st["seen"]):
            _mark_seen(st, it)
            continue
        try:
            text = evaluate(run, it)
        except Defer:
            continue
        except Exception as e:
            log(f"item skipped after error: {short_err(e)}")
            text = None
        if text and not run.notify(text):
            break  # Telegram is down: keep the rest for the next run
        _mark_seen(st, it)
    st["warm"] = sorted(set(st["warm"]) | sources)


# ---------- Volume spikes ----------

def _market_open(now):
    mins = now.hour * 60 + now.minute
    return now.weekday() < 5 and 9 * 60 + 40 <= mins <= 16 * 60 + 15


def volume_check(run, force=False):
    """Watchlist: today's volume vs. the 20-day average."""
    st = run.st
    now = et_now()
    if not st["watch"]:
        return
    if not force:
        if not _market_open(now) or time.time() - st["vol_last"] < config.VOLUME_CHECK_EVERY_MIN * 60:
            return
    st["vol_last"] = time.time()
    today = now.date()
    st["vol"] = {s: d for s, d in st["vol"].items() if d == str(today)}
    for sym in st["watch"]:
        if sym in st["vol"]:
            continue
        sp = market.volume_spike(sym, today)
        if not sp:
            continue
        ratio = f"פי {sp['ratio']:.1f} מהממוצע" if sp["ratio"] else "בדרך כלל כמעט אין בה מסחר"
        chg = f" ({sp['chg']:+.1f}%)" if sp["chg"] is not None else ""
        text = (f"📈 <b>{sym}</b> · ⭐ ברשימה שלך\n<b>ווליום חריג</b>\n"
                f"{sp['vol']:,} מניות היום – {ratio} (ממוצע: {sp['avg']:,.0f})\n"
                f"מחיר: ${fmt_price(sp['price'])}{chg}\n"
                + "".join(x + "\n" for x in dilution.lines(_dil(st, [sym])))
                + f'<a href="https://www.otcmarkets.com/stock/{sym}/overview">OTC Markets</a>')
        if run.notify(text):
            st["vol"][sym] = str(today)


def market_volume_check(run, force=False):
    """Whole market: the most active stocks in the price band, vs. their 20-day average."""
    st = run.st
    now = et_now()
    if not force and (not _market_open(now)
                      or time.time() - st["mvol_last"] < config.VOLUME_CHECK_EVERY_MIN * 60):
        return
    st["mvol_last"] = time.time()
    try:
        leaders = otcm.volume_leaders()
    except Exception as e:
        log(f"market volume: FAILED ({short_err(e)})")
        return
    today = now.date()
    cache = st["avgvol"] = {s: v for s, v in st["avgvol"].items() if v[0] == str(today)}
    watch = set(st["watch"])
    found = []
    for r in leaders:
        sym, p = (r.get("symbol") or "").upper(), r.get("price") or 0
        if not sym or sym in watch or "m:" + sym in st["vol"]:
            continue
        if not config.MIN_PRICE <= p <= config.MAX_PRICE or (r.get("dollarVolume") or 0) < config.VOLUME_MIN_DOLLARS_MARKET:
            continue
        avg = market.avg_volume(sym, today, cache)
        if avg is None:
            continue
        ratio = (r.get("shareVolume") or 0) / avg if avg > 0 else float("inf")
        if ratio >= config.VOLUME_SPIKE_X_MARKET:
            found.append((ratio, avg, r))
    found.sort(key=lambda x: -x[0])
    for ratio, avg, r in found[:config.MARKET_VOLUME_MAX_ALERTS]:
        sym = r["symbol"].upper()
        times = f"פי {ratio:.1f} מהממוצע" if ratio != float("inf") else "בדרך כלל כמעט אין בה מסחר"
        lines = [f"📈 <b>{sym}</b> · ${fmt_price(r['price'])} ({r.get('pctChange') or 0:+.1f}%)",
                 "<b>ווליום חריג בשוק</b>",
                 f"{int(r.get('shareVolume') or 0):,} מניות היום – {times} (ממוצע: {avg:,.0f})",
                 f"מחזור: ${r.get('dollarVolume') or 0:,.0f}" + (f" · {r['tierName']}" if r.get("tierName") else "")]
        if r.get("isCaveatEmptor"):
            lines.append("⚠️ המניה מסומנת Caveat Emptor (גולגולת)")
        lines += dilution.lines(_dil(st, [sym]))
        lines.append(f'<a href="https://www.otcmarkets.com/stock/{sym}/overview">OTC Markets</a>')
        if run.notify("\n".join(lines)):
            st["vol"]["m:" + sym] = str(today)


# ---------- Dilution (watchlist) ----------

def dilution_check(run, force=False):
    """Alerts when outstanding or authorized shares of a watchlist stock change (transfer-agent data)."""
    st = run.st
    if not st["watch"] or (not force and time.time() - st["dilw_last"] < config.DILUTION_WATCH_EVERY_MIN * 60):
        return
    st["dilw_last"] = time.time()
    st["tso"] = {s: v for s, v in st["tso"].items() if s in st["watch"]}
    for sym, kind, old, new, date in dilution.watch(st):
        up = new > old
        if kind == "tso":
            title, what = ("דילול חדש – נוספו מניות" if up else "מספר המניות ירד (ביטול מניות)"), "מספר המניות במחזור"
        else:
            title, what = ("הגדלת המניות המורשות" if up else "הקטנת המניות המורשות"), "מספר המניות המורשות"
        run.notify(f"{'🔴' if up else '🟢'} <b>{sym}</b> · ⭐ ברשימה שלך\n<b>{title}</b>\n"
                   f"{what} {'עלה' if up else 'ירד'} מ-{dilution.fmt(old)} ל-{dilution.fmt(new)} "
                   f"(שינוי של {abs(new - old) / old * 100:.1f}%)\n"
                   f"לפי סוכן ההעברות (דרך OTC Markets), נכון ל-{date}\n"
                   f'<a href="https://www.otcmarkets.com/stock/{sym}/security">OTC Markets – מבנה המניות</a>')


# ---------- Main ----------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="print alerts instead of sending them")
    ap.add_argument("--all", action="store_true", help="treat every collected item as new (testing)")
    ap.add_argument("--watch", default="", help="comma-separated watchlist to use (testing)")
    ap.add_argument("--force-vol", action="store_true", help="run the volume check now (testing)")
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    if config.IN_CLOUD and not (config.STATE_KEY and config.TELEGRAM_TOKEN):
        log("Secrets are not set yet (run SETUP.bat). Nothing to do.")
        return

    st = state.load()
    state.prune(st)
    run = Run(st, args.dry)
    if args.watch:
        st["watch"] = _tickers(args.watch.split(","))
    if args.all:
        st["seen"], st["boot"] = {}, True
        st["warm"] = ["FINRA", "OTCCA", "OTCM", "PRN", "SEC"]
    if st["boot"] and not st["warm"]:
        st["warm"] = ["FINRA", "PRN", "SEC"]   # sources that existed before per-source warm-up
    day = str(et_now().date())
    if st["ai"]["day"] != day:
        st["ai"].update(day=day, n=0)
    if st["sent"]["day"] != day:
        st["sent"] = {"day": day, "n": 0}

    items = []
    try:
        handle_commands(run)
        items = collect(run)
        process(run, items)
        volume_check(run, force=args.force_vol)
        market_volume_check(run, force=args.force_vol)
        dilution_check(run, force=args.force_vol)
        if market.stats["ok"] or market.stats["fail"]:
            set_health(st, "Yahoo", None if market.stats["ok"] else market.stats["err"])
        if ai.calls:
            set_health(st, "AI", None)
        elif ai.last_error:
            set_health(st, "AI", ai.last_error)
        health_warnings(run)
    finally:
        st["last_run"] = time.time()
        st["runs"] += 1
        st["sent"]["n"] += run.sent
        state.save(st)
    log(f"done: {len(items)} items, {run.sent} alerts, {ai.calls} AI calls, {time.time() - run.start:.0f}s")


if __name__ == "__main__":
    main()
