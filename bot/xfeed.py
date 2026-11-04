"""X (Twitter) reader: new posts of the accounts Sean Toizer (@SeanTwizer) follows, read through the bot's own X account
(@polyy20266, cookies in the X_COOKIES secret). Every post that names an OTC stock gets an AI read (who is talking,
pump or real early information), the same checks as a group call, an alert, and - when strong - goes to the paper agent.
Never log post text, names or tickers (the Actions logs are public)."""

import json
import os
import random
import re
import time

from . import ai, config, group, market, trader
from .net import short_err
from .util import esc, fmt_price, il_time, log

DAY = 86400
USERS_FILE = os.path.join(os.path.dirname(__file__), "x_users.txt")
CASH = re.compile(r"\$([A-Za-z]{2,5})(?![A-Za-z])")
ROLE_HE = {"insider": "👔", "company": "🏢", "promoter": "📣", "trader": "📊", "news": "📰"}


class Dead(Exception):
    """X refused the bot's login (logged out / locked)."""


def init(st):
    x = st.setdefault("x", {})
    for k, v in (("last", 0), ("chk", 0), ("users", []), ("alerted", {}), ("off", False), ("dead", False),
                 ("followed", []), ("fts", []), ("fnext", 0)):
        x.setdefault(k, v)
    with open(USERS_FILE, encoding="utf-8") as f:       # X shows others only ~70 of Sean's newest follows:
        have = {h.lower() for h in x["users"]}          # the file grows when we collect more from his Chrome
        x["users"] += [h for h in f.read().split() if not h.startswith("#") and h.lower() not in have]
    return x


def _call(url, **kw):
    """One X web call as the bot's account. POST: X answers 404 to a GET search from this account, and
    twscrape's own client gets 403 from GitHub - a plain browser-like (curl-cffi) request works."""
    from curl_cffi import requests as creq
    from twscrape.account import TOKEN
    ck = dict(kv.strip().split("=", 1) for kv in config.X_COOKIES.split(";") if "=" in kv)
    r = creq.post(url, headers={"authorization": TOKEN, "x-csrf-token": ck.get("ct0", ""),
                                "x-twitter-active-user": "yes", "x-twitter-auth-type": "OAuth2Session",
                                "cookie": "; ".join(f"{k}={v}" for k, v in ck.items())},
                  impersonate="chrome", timeout=30, **kw)
    if r.status_code == 401 or '"code":32' in r.text[:300]:
        raise Dead(f"HTTP {r.status_code}")
    return r


def _gql(op, variables):
    from twscrape.api import GQL_FEATURES, GQL_URL
    r = _call(f"{GQL_URL}/{op}", json={"variables": variables, "features": GQL_FEATURES, "queryId": op.split("/")[0]})
    if r.status_code == 403:
        raise Dead("HTTP 403")
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}")
    return r.json()


def _follow(x):
    """Follow one more of Sean's accounts from the bot account - slowly, so X does not lock it."""
    now = time.time()
    x["fts"] = [t for t in x["fts"] if now - t < DAY]
    done = {h.lower() for h in x["followed"]}
    todo = [h for h in x["users"] if h.lower() not in done]
    if not todo or now < x["fnext"] or len(x["fts"]) >= config.X_FOLLOW_PER_DAY:
        return
    r = _call("https://x.com/i/api/1.1/friendships/create.json", data={"screen_name": todo[0]})
    if r.status_code == 200 or '"code":160' in r.text:      # 160 = already asked to follow
        x["followed"].append(todo[0])
        x["fts"].append(now)
        x["fnext"] = now + config.X_FOLLOW_GAP_SEC * random.uniform(1, 2)
    elif '"code":108' in r.text or '"code":50' in r.text:     # account gone / renamed
        x["users"].remove(todo[0])
    else:                                                     # 161 = follow limit, 226 = looks automated
        x["fnext"] = now + 6 * 3600
        code = re.findall(r'"code":(\d+)', r.text)[:1]
        log(f"x: follow paused (HTTP {r.status_code} {code})")


def _home(x):
    """New posts of everyone the bot follows - the chronological Following feed, up to 3 pages back."""
    from twscrape.models import parse_tweets
    posts, cursor = {}, None
    for _ in range(3):
        v = {"count": 40, "includePromotedContent": False, "latestControlAvailable": True, "requestContext": "launch"}
        if cursor:
            v["cursor"] = cursor
        rep = _gql(config.X_HOME_OP, v)
        got = list(parse_tweets(rep))
        posts.update((t.id, t) for t in got if t.id > x["last"])
        c = re.search(r'"value":"([^"]+)","cursorType":"Bottom"', json.dumps(rep, separators=(",", ":")))
        if not got or not c or min(t.id for t in got) <= x["last"] or not x["last"]:
            break
        cursor = c.group(1)
    return posts


def _fetch(x):
    """New posts (oldest first) of Sean's accounts: the Following feed for those the bot follows, search for the rest."""
    from twscrape.api import OP_SearchTimeline
    from twscrape.models import parse_tweets
    users = {h.lower() for h in x["users"]}
    done = {h.lower() for h in x["followed"]}
    handles = [h for h in x["users"] if h.lower() not in done]
    posts = {}
    if done:
        try:
            posts = _home(x)
        except RuntimeError as e:                 # op id changed by X: search everyone until it is updated
            log(f"x: home feed failed ({short_err(e)}) - searching")
            handles = x["users"]
    handles = handles[:config.X_MAX_HANDLES]
    for i in range(0, len(handles), config.X_PER_QUERY):
        q = " OR ".join(f"from:{h}" for h in handles[i:i + config.X_PER_QUERY])
        rep = _gql(OP_SearchTimeline, {"rawQuery": q, "count": 40, "product": "Latest", "querySource": "typed_query"})
        posts.update((t.id, t) for t in parse_tweets(rep) if t.id > x["last"])
    return sorted((t for t in posts.values() if t.user.username.lower() in users), key=lambda t: t.id)


def cycle(run):
    st = run.st
    if not config.X_COOKIES:
        return
    x = init(st)
    if time.time() - x["chk"] < (config.X_EVERY_SEC if not x["dead"] else 900):
        return
    x["chk"] = time.time()
    try:
        posts = _fetch(x)
    except Exception as e:
        if isinstance(e, Dead):
            if not x["dead"] and run.notify("🔌 <b>החיבור לטוויטר נותק</b>\n"
                                            "כנראה טוויטר ניתק את חשבון הבוט. צריך להתחבר מחדש ולהעביר מפתח חדש."):
                x["dead"] = True
        log(f"x: FAILED ({short_err(e)})")
        return
    x["dead"] = False
    try:
        _follow(x)
    except Exception as e:
        log(f"x: follow failed ({short_err(e)})")
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
