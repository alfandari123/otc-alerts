"""Reads the Elevate Trading Telegram group (through Reuven's own account) for stock calls.

A "call" is a message with an OTC ticker and 👀 (watching it), an entry word (כניסה / סטרטר / הוספה...) or a tweet
(ציוץ / tweet / an x.com link). Each new call is checked with the bot's own engines (price, volume, dilution, bad news,
Google News) and sent to Reuven, and the paper agent checks it with its news-buy rules (trader.on_group; his request
29.10 - the backtest found no edge in following calls blindly, so the rules decide). After GROUP_SCORE_DAYS a call is
scored like tools/group_backtest.py (did +20% come before -15%?) into a per-user scoreboard (/group).
Group content is personal data: it lives only in the encrypted state, never in logs (the repo and its logs are public).
"""
import atexit
import re
import time
from datetime import datetime
from statistics import median

from . import config, dilution, market, net, radar, trader
from .net import short_err
from .util import ET, esc, et_now, fmt_price, il_time, log

GROUP_ID = -1002154889214
AUTO_BOT = 8766097729      # "Elevate trading" bot posting "first profit" messages
ANON = 0                   # anonymous admin posts (Sean Toizer, the group's owner)
DAY = 86400
BARS = "https://query1.finance.yahoo.com/v8/finance/chart/{}?interval=5m&range=10d&includePrePost=false"

EYES = "\U0001f440"
ENTRY = re.compile(r"כניסה|נכנסתי|סטרטר|הוספה|הוספתי|חיזקתי|העמסתי|השלמתי|קניתי|בפנים|"
                   r"\b(?:starter|entry|entered|bought|added|loaded)\b", re.I)
URL = re.compile(r"https?://\S+|\S+\.(?:com|co\.il|io|net)\S*", re.I)
TWEET = re.compile(r"ציוץ|ציוצים|צייצ|טוויט|\btweet(?:s|ed)?\b|(?:x|twitter)\.com/", re.I)
DOLLAR = re.compile(r"\$([A-Za-z]{2,5})(?![A-Za-z])")
WORD = re.compile(r"(?<![A-Za-z$])([A-Za-z]{3,5})(?![A-Za-z])")
STOP = set("""GLHF GLGL NICE NOICE NICEE NICEEE WEE WEEE WEEEE MORE BID ASK ASKS BIDS LOL LOLL OTC THE AND YOU FOR NOT
BUT ALL NET PNL REALIZED LEVEL ATM CEO SEC LOCKED LOADED BABY TRIPS TRIP DUBS SUBS FOMO HOD LOD VWAP EMA RSI GAP RUN
RUNS DAY NOW YES YEAH OMG WOW LETS GOOD BAD HOLD SELL BUY LONG SHORT MOON PUMP DUMP NEWS TWEET CATCHING SQUAD KING
KINGS GOAT FIRE NICE WTF BRO BROS THX THANKS GUYS TODAY OPEN CLOSE HIGH LOW WAIT WAITING HERE THERE WITH THIS THAT
WHAT WHEN WHERE WHO WHY HOW CAN WILL JUST STILL SOON BACK NEXT LAST FIRST NEW OLD BIG HUGE ONE TWO TEN MAX MIN AVG
VOL VOLUME CHART FILL FILLED SOLD BOUGHT ENTRY STARTER ADDED ANOTHER BEST LOVE HAHA HAHAHA YALLA SHEKEL USD ILS
SCHWAB IBKR TOS LEVEL TWEETS POST ALERT CALLS CALL PUT PUTS OTCM FINRA DTC DTCC SHELL SPLIT TICKER LOADING""".split())
KIND_HE = {"eyes": "👀 במעקב", "entry": "🛒 כניסה", "tweet": "🐦 ציוץ"}

_client = None


def parse(text):
    """{"kind": "eyes"|"entry"|"tweet"|"mention", "syms": [...]} or None. Tickers are NOT validated here."""
    if not text:
        return None
    t = URL.sub(" ", text)
    if EYES in t:
        kind = "eyes"
    elif ENTRY.search(t):
        kind = "entry"
    elif TWEET.search(text):            # before the links are removed: an x.com link is a tweet
        kind = "tweet"
    else:
        kind = "mention"
    words = t.split()
    short = len(words) <= 4
    syms = [m.upper() for m in DOLLAR.findall(t)]
    for m in WORD.findall(t):
        # all-caps anywhere; Capitalized/lowercase only in a call or a very short message ("Tips👀", "Ugiz") -
        # not in a tweet (its copied English text is full of 3-5 letter words)
        if m.isupper() or kind in ("eyes", "entry") or short:
            syms.append(m.upper())
    out = []
    for s in syms:
        if s not in STOP and s not in out:
            out.append(s)
    if not out or (kind == "mention" and not short):
        return None
    return {"kind": kind, "syms": out[:3]}


def outcome(bars, ts):
    """How a call did, from 5-minute bars [(t, high, low, close, volume)...].
    Entry = close of the first bar that STARTS at or after the post (you could not buy earlier)."""
    after = [x for x in bars if x[0] >= ts]
    if not after or after[0][0] - ts > 3 * DAY:
        return None
    entry = after[0][3]
    if not entry:
        return None
    res = {"entry": entry, "late_min": (after[0][0] - ts) / 60}
    for days in (1, 3, 5):
        w = [x for x in after if x[0] < after[0][0] + days * DAY]
        if after[-1][0] < after[0][0] + days * DAY * 0.9:
            continue                                        # not enough time passed yet
        res[f"max{days}"] = max(x[1] for x in w) / entry - 1
        res[f"min{days}"] = min(x[2] for x in w) / entry - 1
        res[f"close{days}"] = w[-1][3] / entry - 1
    # first to happen: +20% or -15%
    res["first"] = "open"
    for x in after[1:]:
        if x[2] <= entry * 0.85:
            res["first"] = "-15%"
            break
        if x[1] >= entry * 1.20:
            res["first"] = "+20%"
            break
    return res


def bars5(sym):
    r = net.get(BARS.format(sym), timeout=20).json()["chart"]["result"]
    out = []
    if r:
        q = r[0]["indicators"]["quote"][0]
        for i, t in enumerate(r[0].get("timestamp") or []):
            h, l, c, v = q["high"][i], q["low"][i], q["close"][i], q["volume"][i]
            if None not in (h, l, c):
                out.append((t, h, l, c, v or 0))
    return out


# ---------- live reading ----------

def init(st):
    g = st.setdefault("grp", {})
    for k, v in (("last", 0), ("chk", 0), ("users", {}), ("names", {}), ("calls", []), ("done", []),
                 ("otc", {}), ("alerted", {}), ("off", False), ("dead", False)):
        g.setdefault(k, v)
    return g


def _connect():
    global _client
    if _client is None:
        from telethon.sessions import StringSession
        from telethon.sync import TelegramClient
        _client = TelegramClient(StringSession(config.TG_SESSION), int(config.TG_API_ID), config.TG_API_HASH)
        _client.connect()
        atexit.register(_client.disconnect)
    return _client


def _uid(m):
    sid = m.sender_id
    return ANON if not sid or sid < 0 else sid


def _name(g, uid):
    if uid == ANON:
        return "מנהל אנונימי (כנראה שון)"
    return g["names"].get(str(uid)) or "משתמש"


def _is_otc(g, sym):
    hit = g["otc"].get(sym)
    if hit and time.time() - hit[0] < 7 * DAY:
        return hit[1]
    c = market.chart(sym)
    if c is None:                       # unknown to Yahoo (or Yahoo is down): ask again next time
        return False
    g["otc"][sym] = [time.time(), bool(c.get("otc"))]
    return c.get("otc")


def cycle(run):
    st = run.st
    if not (config.TG_SESSION and config.TG_API_ID and config.TG_API_HASH):
        return
    g = init(st)
    if time.time() - g["chk"] < (config.GROUP_EVERY_SEC / 2 if not g["dead"] else 3600):
        return
    g["chk"] = time.time()
    try:
        c = _connect()
        if not c.is_user_authorized():
            if not g["dead"] and run.notify("🔌 <b>החיבור לקבוצת Elevate Trading נותק</b>\n"
                                            "כנראה החיבור בוטל בטלגרם (הגדרות ← מכשירים). צריך להתחבר מחדש מהמחשב."):
                g["dead"] = True
            log("group: not authorized")
            return
        g["dead"] = False
        if not g["last"]:
            top = c.get_messages(GROUP_ID, limit=1)
            g["last"] = top[0].id if top else 0
            log("group: warm-up")
            return
        msgs = sorted(c.get_messages(GROUP_ID, min_id=g["last"], limit=200), key=lambda m: m.id)
    except Exception as e:
        log(f"group: FAILED ({short_err(e)})")
        return
    calls = 0
    for m in msgs:
        g["last"] = max(g["last"], m.id)
        try:
            calls += on_message(run, g, m)
        except Exception as e:
            log(f"group: message skipped ({short_err(e)})")
    log(f"group: {len(msgs)} messages, {calls} calls")
    score(g)


def on_message(run, g, m):
    sig = parse(m.message or "")
    if not sig or sig["kind"] == "mention":
        return 0
    uid = _uid(m)
    if uid == AUTO_BOT:
        return 0
    if uid != ANON and m.sender:
        name = " ".join(x for x in (getattr(m.sender, "first_name", None), getattr(m.sender, "last_name", None)) if x)
        g["names"][str(uid)] = name[:30] or "משתמש"
    n = 0
    ts = m.date.timestamp()
    for sym in sig["syms"]:
        if not _is_otc(g, sym):
            continue
        n += 1
        call = {"uid": uid, "sym": sym, "kind": sig["kind"], "ts": ts}
        g["calls"].append(call)
        if time.time() - ts >= config.GROUP_ALERT_MAX_AGE_MIN * 60:
            continue                     # an old post (the bot was down): scored only
        agent = trader.on_group(run.st, sym, f"👥 קריאה בקבוצה – {_name(g, uid)} · {KIND_HE[call['kind']]}", ts)
        if not g["off"] and time.time() - g["alerted"].get(sym, 0) >= config.GROUP_REALERT_H * 3600:
            if run.notify(alert(run.st, g, call, m.message, agent)):
                g["alerted"][sym] = time.time()
    return n


# ---------- alert ----------

def record_text(u):
    """Hebrew one-liner: how this user's calls did, or '' when there is no record yet."""
    if not u or not u["n"]:
        return ""
    dec = u["w"] + u["l"]
    text = f"{u['w']} מתוך {dec} הגיעו ל-+20% לפני -15%" if dec else "אין עדיין קריאות שהוכרעו"
    if u["c3"]:
        text += f" · חציון אחרי 3 ימים {median(u['c3']) * 100:+.0f}%"
    return text + f" ({u['n']} קריאות)"


def _day_stats(sym):
    """(day change %, today's volume, 20-day average volume) from the daily chart; None where unknown."""
    c = market.chart(sym)
    if not c or not c["ts"] or len(c["ts"]) != len(c["vol"]):
        return None, None, None
    last_today = datetime.fromtimestamp(c["ts"][-1], ET).date() == et_now().date()
    vols = [v or 0 for v in c["vol"]]
    closes = [x for x in (c["close"][:-1] if last_today else c["close"]) if x]
    hist = (vols[:-1] if last_today else vols)[-20:]
    avg = sum(hist) / len(hist) if hist else None
    prev = closes[-1] if closes else c["prev"]
    chg = (c["price"] / prev - 1) * 100 if prev and c["price"] else None
    return chg, (vols[-1] if last_today else 0), avg


def alert(st, g, call, text, agent=""):
    sym, uid = call["sym"], call["uid"]
    price = market.price([sym])
    chg, vol, avg = _day_stats(sym)
    head = f"👥 <b>{esc(sym)}</b>"
    if price:
        head += f" · ${fmt_price(price)}" + (f" ({chg:+.0f}% היום)" if chg is not None else "")
    lines = [head + " · קריאה בקבוצה",
             f"<b>{esc(_name(g, uid))}</b> · {KIND_HE[call['kind']]} · {il_time(call['ts'])}"]
    rec = record_text(g["users"].get(str(uid)))
    lines.append(f"📋 רקורד: {rec}" if rec else "📋 אין עדיין רקורד לכותב הזה")
    snippet = " ".join((text or "").split())
    if snippet:
        lines.append("💬 " + esc(snippet[:150] + ("…" if len(snippet) > 150 else "")))
    if vol is not None:
        ratio = f"פי {vol / avg:.1f} מהממוצע" if avg else "בדרך כלל כמעט אין בה מסחר"
        lines.append(f"📈 ווליום היום: {vol:,} – {ratio}" if vol else "📈 עוד אין מסחר היום")
    dil = dilution.summary(sym, st, str(et_now().date()))
    lines += dilution.lines(dil)
    try:
        danger = trader._danger(st, sym)
    except Exception as e:
        danger = ""
        log(f"group: danger check failed ({short_err(e)})")
    if danger:
        lines.append(f"⛔ בדיקת הבוט: {esc(danger)}")
    elif dil:
        lines.append("✅ בדיקת הבוט: אין דילול פעיל או חדשה רעה ידועה")
    else:
        lines.append("⚪ בדיקת הבוט: אין חדשה רעה ידועה, אבל אין נתוני מניות – דילול לא נבדק")
    news = _news(sym)
    if news:
        lines.append(f"📰 חדשות ב-{radar.FRESH_DAYS} הימים האחרונים ({len(news)}): "
                     f'<a href="{esc(news[0]["url"])}">{esc(news[0]["title"][:120])}</a>')
    elif news is not None:
        lines.append(f"📰 אין חדשות ב-Google News ב-{radar.FRESH_DAYS} הימים האחרונים")
    lines += market.liquidity_lines(sym)
    lines.append(f'<a href="https://www.otcmarkets.com/stock/{sym}/overview">OTC Markets</a>')
    if danger and agent.startswith("🤖 הסוכן בודק"):
        agent = "🤖 הסוכן לא יקנה – יש סימן סכנה (למעלה)"
    lines.append(agent or "ℹ️ התראה בלבד – הסוכן לא קונה לפי הקבוצה כרגע")
    return "\n".join(lines)


def _news(sym):
    """Google News of the last days about this stock: the ticker as a ticker ($ABCD, OTC: ABCD) or the company's
    name in the title. A plain search for a ticker that is also a word (TIPS, BEST...) brings unrelated news."""
    company = re.sub(r"[,.]?\s+(inc|corp|corporation|co|ltd|plc|holdings?|group|company)\.?$", "",
                     (market.chart(sym) or {}).get("name") or "", flags=re.I).strip()
    try:
        found = radar._news(sym, company)
    except Exception:
        return None
    tick = re.compile(rf"(?:\$|\(|OTC[A-Z]*\s*:\s*){re.escape(sym)}(?![A-Za-z])")   # $ABCD, (ABCD), OTC: ABCD
    return sorted((x for x in found if tick.search(x["title"])
                   or (len(company) >= 4 and company.lower() in x["title"].lower())), key=lambda x: -x["ts"])


# ---------- scoring ----------

def add_result(g, uid, res):
    u = g["users"].setdefault(str(uid), {"n": 0, "w": 0, "l": 0, "c3": []})
    u["n"] += 1
    u["w"] += res["first"] == "+20%"
    u["l"] += res["first"] == "-15%"
    if "close3" in res:
        u["c3"] = (u["c3"] + [round(res["close3"], 3)])[-60:]


def score(g, limit=10):
    """Calls older than GROUP_SCORE_DAYS get their result (a few per run; Yahoo is asked once per ticker)."""
    now, keep, cache = time.time(), [], {}
    for cl in g["calls"]:
        age = now - cl["ts"]
        if age < config.GROUP_SCORE_DAYS * DAY or len(cache) >= limit:
            keep.append(cl)
            continue
        try:
            if cl["sym"] not in cache:
                cache[cl["sym"]] = bars5(cl["sym"])
            res = outcome(cache[cl["sym"]], cl["ts"])
        except Exception:
            res = None
        if res and "close3" in res:
            add_result(g, cl["uid"], res)
            g["done"] = (g["done"] + [{**cl, "first": res["first"], "c3": round(res["close3"], 3)}])[-30:]
        elif age < 10 * DAY:
            keep.append(cl)          # bars not there yet (weekend / Yahoo hiccup): try again later
    g["calls"] = keep


# ---------- /group command ----------

def command(st, args):
    g = init(st)
    a = " ".join(args).lower()
    if a in ("off", "כבה", "השתק"):
        g["off"] = True
        return "🔕 התראות הקבוצה הושתקו (הבוט ממשיך לקרוא ולעדכן את הטבלה). החזרה: <code>/group on</code>"
    if a in ("on", "הפעל"):
        g["off"] = False
        return "🔔 התראות הקבוצה חזרו."
    if not config.TG_SESSION:
        return "👥 קורא הקבוצה עוד לא מחובר."
    lines = ["👥 <b>קבוצת Elevate Trading – טבלת אמינות</b>",
             "לכל כותב: כמה מהקריאות (👀 / כניסה / ציוץ) הגיעו ל-+20% לפני ירידה של 15%, וחציון התשואה אחרי 3 ימים.", ""]
    ranked = sorted(((uid, u) for uid, u in g["users"].items() if u["n"]),   # judged users first, best hit rate
                    key=lambda x: (x[1]["n"] < 5, -x[1]["w"] / max(1, x[1]["w"] + x[1]["l"]), -x[1]["n"]))
    for uid, u in ranked[:15]:
        lines.append(f"• <b>{esc(_name(g, int(uid)))}</b>: {record_text(u)}"
                     + (" · מעט מדי לשיפוט" if u["n"] < 5 else ""))
    if not ranked:
        lines.append("עוד אין קריאות שהוכרעו.")
    if g["done"]:
        lines += ["", "<b>אחרונות שהוכרעו:</b>"]
        for d in g["done"][-8:][::-1]:
            res = {"+20%": "✅ +20% קודם", "-15%": "🔻 -15% קודם"}.get(d["first"], "⚪ לא הוכרע")
            lines.append(f"{esc(d['sym'])} · {esc(_name(g, d['uid']))} · {res} · אחרי 3 ימים {d['c3'] * 100:+.0f}%")
    pt = trader.init(st)
    grp = [x for x in pt["closed"] if x.get("src") == "group"]
    held = [s for s, p in pt["pos"].items() if p.get("src") == "group"]
    lines += ["", "<b>🤖 קניות הסוכן לפי הקבוצה (וירטואלי):</b>", trader._stats(grp) if grp else "עוד אין מכירות."]
    if held:
        lines.append("מוחזקות עכשיו: " + " · ".join(held))
    lines += ["", f"⏳ ממתינות לתוצאה: {len(g['calls'])} · "
                  + ("🔕 התראות מושתקות (<code>/group on</code>)" if g["off"] else "השתקה: <code>/group off</code>")]
    return "\n".join(lines)


def seed(st, users, names, secid):
    """History from tools/group_backtest.py (sent through the inbox) replaces the scoreboard; the OTC Markets ids
    of the group's stocks (looked up at home) let the cloud read their share counts (dilution)."""
    g = init(st)
    g["users"] = {str(k): v for k, v in users.items()}
    g["names"].update({str(k): v for k, v in names.items()})
    st.setdefault("secid", {}).update(secid)
    return len(g["users"])
