"""X (Twitter) reader: new posts of the accounts Sean Toizer (@SeanTwizer) follows, read through the bot's own X account
(@polyy20266, cookies in the X_COOKIES secret). Every post that names an OTC stock gets an AI read (who is talking,
pump or real early information), the same checks as a group call, an alert, and - when strong - goes to the paper agent.
Never log post text, names or tickers (the Actions logs are public)."""

import json
import os
import re
import time

from . import ai, config, group, market, trader
from .net import short_err
from .util import esc, fmt_price, il_time, log

SEAN = "SeanTwizer"
DAY = 86400
USERS_FILE = os.path.join(os.path.dirname(__file__), "x_users.txt")
CASH = re.compile(r"\$([A-Za-z]{2,5})(?![A-Za-z])")
ROLE_HE = {"insider": "👔", "company": "🏢", "promoter": "📣", "trader": "📊", "news": "📰"}


class Dead(Exception):
    """X refused the bot's login (logged out / locked)."""


def init(st):
    x = st.setdefault("x", {})
    for k, v in (("last", 0), ("chk", 0), ("users", []), ("uchk", 0), ("alerted", {}), ("off", False),
                 ("dead", False)):
        x.setdefault(k, v)
    if not x["users"]:
        with open(USERS_FILE, encoding="utf-8") as f:
            x["users"] = [h for h in f.read().split() if not h.startswith("#")]
    return x


def _gql(op, variables):
    """One X web GraphQL call as the bot's account. POST: X answers 404 to a GET search from this account, and
    twscrape's own client gets 403 from GitHub - a plain browser-like (curl-cffi) POST works."""
    from curl_cffi import requests as creq
    from twscrape.account import TOKEN
    from twscrape.api import GQL_FEATURES, GQL_URL
    ck = dict(kv.strip().split("=", 1) for kv in config.X_COOKIES.split(";") if "=" in kv)
    r = creq.post(f"{GQL_URL}/{op}", json={"variables": variables, "features": GQL_FEATURES,
                                           "queryId": op.split("/")[0]},
                  headers={"authorization": TOKEN, "x-csrf-token": ck.get("ct0", ""), "x-twitter-active-user": "yes",
                           "x-twitter-auth-type": "OAuth2Session",
                           "cookie": "; ".join(f"{k}={v}" for k, v in ck.items())},
                  impersonate="chrome", timeout=30)
    if r.status_code in (401, 403) or '"code":32' in r.text[:300]:
        raise Dead(f"HTTP {r.status_code}")
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}")
    return r.json()


def _following():
    from twscrape.api import OP_Following, OP_UserByScreenName
    from twscrape.models import parse_users
    m = re.search(r'"rest_id":"(\d+)"', json.dumps(_gql(OP_UserByScreenName, {"screen_name": SEAN})))
    if not m:
        return []
    out, cursor = [], None
    for _ in range(25):
        v = {"userId": m.group(1), "count": 100, "includePromotedContent": False}
        if cursor:
            v["cursor"] = cursor
        rep = _gql(OP_Following, v)
        got = [u.username for u in parse_users(rep)]
        out += [h for h in got if h not in out]
        c = re.search(r'"value":"([^"]+)","cursorType":"Bottom"', json.dumps(rep))
        if not got or not c:
            break
        cursor = c.group(1)
        time.sleep(1)
    return out


def _fetch(x):
    """New posts (oldest first) and, once a day, the fresh list of accounts Sean follows."""
    from twscrape.api import OP_SearchTimeline
    from twscrape.models import parse_tweets
    users = None
    if time.time() - x["uchk"] > DAY:
        x["uchk"] = time.time()
        try:
            got = _following()
            log(f"x: Sean follows {len(got)} (visible)")
            if len(got) >= len(x["users"]) * 0.8:     # a short page (X limits) must not shrink the list
                users = got
        except Dead:
            raise
        except Exception as e:
            log(f"x: following list failed ({short_err(e)})")
    handles = (users or x["users"])[:config.X_MAX_HANDLES]
    posts = {}
    for i in range(0, len(handles), config.X_PER_QUERY):
        q = " OR ".join(f"from:{h}" for h in handles[i:i + config.X_PER_QUERY])
        rep = _gql(OP_SearchTimeline, {"rawQuery": q, "count": 40, "product": "Latest", "querySource": "typed_query"})
        for t in parse_tweets(rep):
            if t.id > x["last"]:
                posts[t.id] = t
    return sorted(posts.values(), key=lambda t: t.id), users


def cycle(run):
    st = run.st
    if not config.X_COOKIES:
        return
    x = init(st)
    if time.time() - x["chk"] < (config.X_EVERY_SEC if not x["dead"] else 900):
        return
    x["chk"] = time.time()
    try:
        posts, users = _fetch(x)
    except Exception as e:
        if isinstance(e, Dead):
            if not x["dead"] and run.notify("🔌 <b>החיבור לטוויטר נותק</b>\n"
                                            "כנראה טוויטר ניתק את חשבון הבוט. צריך להתחבר מחדש ולהעביר מפתח חדש."):
                x["dead"] = True
        log(f"x: FAILED ({short_err(e)})")
        return
    x["dead"] = False
    if users:
        x["users"] = users
    if not x["last"]:                      # first run: start from now, do not alert old posts
        x["last"] = posts[-1].id if posts else 1
        log("x: warm-up")
        return
    n = 0
    for t in posts:
        x["last"] = max(x["last"], t.id)
        try:
            n += on_post(run, x, t)
        except Exception as e:
            log(f"x: post skipped ({short_err(e)})")
    log(f"x: {len(posts)} posts, {n} with OTC tickers")


def on_post(run, x, t):
    if t.retweetedTweet:
        return 0
    text = t.rawContent or ""
    if t.quotedTweet:
        text += "\n[quoted] " + (t.quotedTweet.rawContent or "")
    syms = []
    for s in [c.upper() for c in (t.cashtags or [])] + [m.upper() for m in CASH.findall(text)]:
        if s not in syms and s not in group.STOP:
            syms.append(s)
    g = group.init(run.st)
    syms = [s for s in syms[:4] if group._is_otc(g, s)]
    ts = t.date.timestamp()
    if not syms or time.time() - ts >= config.GROUP_ALERT_MAX_AGE_MIN * 60:
        return len(syms)
    u = t.user
    post = {"name": u.displayname, "handle": u.username, "bio": u.rawDescription, "followers": u.followersCount,
            "syms": syms, "text": text}
    x["ai"] = [s for s in x.get("ai", []) if time.time() - s < 3600]
    if len(x["ai"]) >= config.X_AI_PER_HOUR:      # the free AI quota is shared with the news alerts
        log("x: AI hourly cap reached, post skipped")
        return len(syms)
    a = ai.analyze_tweet(post) if ai.usable() else None
    if a:
        x["ai"].append(time.time())
    if a and a["score"] < config.X_ALERT_MIN_SCORE and not a["early"] and a["role"] not in ("insider", "company"):
        return len(syms)
    for sym in syms:
        agent = ""
        if a and a["score"] >= config.TRADE_MIN_SCORE and a["pump"] <= config.X_TRADE_MAX_PUMP:
            agent = trader.on_group(run.st, sym, f"🐦 ציוץ – @{u.username}", ts)
        if not x["off"] and time.time() - x["alerted"].get(sym, 0) >= config.GROUP_REALERT_H * 3600:
            if run.notify(alert(run.st, sym, post, a, t, agent)):
                x["alerted"][sym] = time.time()
    return len(syms)


def alert(st, sym, post, a, t, agent):
    price = market.price([sym])
    chg, vol, avg = group._day_stats(sym)
    head = f"🐦 <b>{esc(sym)}</b>"
    if price:
        head += f" · ${fmt_price(price)}" + (f" ({chg:+.0f}% היום)" if chg is not None else "")
    lines = [head + " · ציוץ בטוויטר",
             f"<b>{esc(post['name'])}</b> (@{esc(post['handle'])}) · {il_time(t.date.timestamp())}"]
    if a:
        lines[-1] += f" · {ROLE_HE.get(a['role'], '👤')} {esc(a['role_he'])}"
        tags = [f"🤖 ציון AI: {a['score']}/10"]
        if a["early"]:
            tags.append("🎯 מידע מוקדם")
        if a["pump"] >= 6:
            tags.append(f"🚨 סימני פאמפ {a['pump']}/10")
        lines.append(" · ".join(tags))
        if a["summary_he"]:
            lines.append(esc(a["summary_he"]))
        if a["risk_he"]:
            lines.append("⚠️ " + esc(a["risk_he"]))
    else:
        lines.append("🤖 ה-AI לא זמין כרגע – לא נבדק מי כותב והאם זה פאמפ")
    snippet = " ".join(post["text"].split())
    lines.append("💬 " + esc(snippet[:200] + ("…" if len(snippet) > 200 else "")))
    lines += group.checks(st, sym, vol, avg, agent or "ℹ️ התראה בלבד – הסוכן קונה רק לפי ציוץ חזק בלי סימני פאמפ")
    lines.append(f'<a href="{esc(t.url)}">לציוץ</a>')
    return "\n".join(lines)
