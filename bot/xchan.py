"""Reads the Telegram channel "X OTC Updates" (through Reuven's own account, the same connection as bot/group.py).

The channel reposts tweets of OTC companies and traders: first line = the X handle, then the tweet, last line = its
link. It is noisy (companies post marketing all day), so only IMPORTANT posts reach him (his request 13.11): every
post that names an OTC stock gets the same AI read as bot/xfeed.py, and only a high score or early information alerts
- being a company account is not enough here. A strong post without pump signs goes to the paper agent with the same
rules as any tweet (trader.on_group). Alerts and the per-ticker quiet time are shared with xfeed, so a tweet that
arrives from both sources alerts once. Never log post text, names or tickers (the Actions logs are public).
"""
import re
import time
from types import SimpleNamespace

from . import ai, config, group, trader, xfeed
from .net import short_err
from .util import log

CHAN_ID = -1003669189410
LINK = re.compile(r"https?://(?:www\.)?(?:twitter|x)\.com/(\w{1,20})/status/(\d+)\S*", re.I)


def init(st):
    c = st.setdefault("xch", {})
    for k, v in (("last", 0), ("chk", 0), ("ai", []), ("low", {})):
        c.setdefault(k, v)
    return c


def parse(text):
    """{"handle", "text", "url", "syms"} of one channel message, or None when it is not a reposted tweet."""
    m = None
    for m in LINK.finditer(text or ""):
        pass                                  # the tweet's own link is the last one
    if not m:
        return None
    handle = m.group(1)
    body = (text[:m.start()] + text[m.end():]).strip()
    lines = body.split("\n")
    if lines and lines[0].strip().lstrip("@").lower() == handle.lower():
        body = "\n".join(lines[1:]).strip()
    syms = []
    for s in (x.upper() for x in xfeed.CASH.findall(body)):
        if s not in syms and s not in group.STOP:
            syms.append(s)
    if not body or not syms:
        return None
    return {"handle": handle, "text": body, "url": f"https://x.com/{handle}/status/{m.group(2)}", "syms": syms[:4]}


def _messages(client, last):
    try:
        return client.get_messages(CHAN_ID, min_id=last, limit=100) if last else client.get_messages(CHAN_ID, limit=1)
    except ValueError:                        # the channel is not known to this connection yet: load his chat list once
        client.get_dialogs()
        return client.get_messages(CHAN_ID, min_id=last, limit=100) if last else client.get_messages(CHAN_ID, limit=1)


def cycle(run):
    st = run.st
    if not (config.TG_SESSION and config.TG_API_ID and config.TG_API_HASH):
        return
    c = init(st)
    if time.time() - c["chk"] < config.XCHAN_EVERY_SEC or group.init(st)["dead"]:
        return
    c["chk"] = time.time()
    try:
        client = group._connect()
        if not client.is_user_authorized():   # bot/group.py tells him once
            return
        msgs = sorted(_messages(client, c["last"]), key=lambda m: m.id)
    except Exception as e:
        log(f"xchan: FAILED ({short_err(e)})")
        return
    if not c["last"]:                         # first run: start from now, old posts are not news
        c["last"] = msgs[-1].id if msgs else 1
        log("xchan: warm-up")
        return
    n = 0
    for m in msgs:
        c["last"] = max(c["last"], m.id)
        try:
            n += on_message(run, c, m.message or "", m.date.timestamp())
        except Exception as e:
            log(f"xchan: message skipped ({short_err(e)})")
    log(f"xchan: {len(msgs)} messages, {n} with OTC tickers")


def on_message(run, c, text, ts):
    p = parse(text)
    if not p:
        return 0
    st, now = run.st, time.time()
    g = group.init(st)
    syms = [s for s in p["syms"] if group._is_otc(g, s)]
    if not syms or now - ts >= config.GROUP_ALERT_MAX_AGE_MIN * 60:
        return len(syms)
    # an account that keeps posting unimportant things about the same stock is not read again for a while
    c["low"] = {k: t for k, t in c["low"].items() if now - t < config.XCHAN_LOW_QUIET_H * 3600}
    keys = [f"{p['handle'].lower()}:{s}" for s in syms]
    if all(k in c["low"] for k in keys):
        return len(syms)
    c["ai"] = [t for t in c["ai"] if now - t < 3600]
    if len(c["ai"]) >= config.XCHAN_AI_PER_HOUR or not ai.usable():
        return len(syms)                      # no AI read = no way to tell important from noise: stay quiet
    post = {"name": p["handle"], "handle": p["handle"], "bio": "", "followers": "unknown", "syms": syms,
            "text": p["text"]}
    a = ai.analyze_tweet(post)
    if not a:
        return len(syms)
    c["ai"].append(now)
    if a["score"] < config.X_ALERT_MIN_SCORE and not a["early"]:
        for k in keys:
            c["low"][k] = now
        return len(syms)
    x = xfeed.init(st)
    t = SimpleNamespace(date=SimpleNamespace(timestamp=lambda: ts), url=p["url"])
    for sym in syms:
        agent = ""
        if a["score"] >= config.TRADE_MIN_SCORE and a["pump"] <= config.X_TRADE_MAX_PUMP:
            agent = trader.on_group(st, sym, f"🐦 ציוץ – @{p['handle']}", ts)
        if not x["off"] and now - x["alerted"].get(sym, 0) >= config.GROUP_REALERT_H * 3600:
            if run.notify(xfeed.alert(st, sym, post, a, t, agent, via="ערוץ X OTC Updates")):
                x["alerted"][sym] = now
    return len(syms)
