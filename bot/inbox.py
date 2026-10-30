"""Commands for the bot left from Claude's PC (tools/bot_cmd.py), e.g. add / remove watchlist tickers.

The bot's memory can only be written safely by the bot itself (during US hours a run is always waiting), so
commands go into an encrypted file on the `inbox` branch; each run applies the ones it has not applied yet and
confirms them in Telegram. Same effect as typing /add or /remove in Telegram.
"""
import json
import os

from cryptography.fernet import Fernet

from . import config, group
from .util import log

PATH = "inbox.bin"


def apply(run, tickers):
    st = run.st
    if not (config.STATE_KEY and os.path.exists(PATH)):
        return
    try:
        cmds = json.loads(Fernet(config.STATE_KEY.encode()).decrypt(open(PATH, "rb").read()))
    except Exception as e:
        log(f"inbox: unreadable ({type(e).__name__})")
        return
    done = st.setdefault("inbox_done", [])
    for c in cmds:
        if c.get("id") in done:
            continue
        syms = tickers(c.get("syms") or [])
        if c.get("op") == "add":
            added = [s for s in syms if s not in st["watch"]][:max(0, config.WATCHLIST_MAX - len(st["watch"]))]
            st["watch"] += added
            text = f"✅ נוספו לרשימת המעקב (מהמחשב): {', '.join(added) or 'כבר היו ברשימה'}"
        elif c.get("op") == "remove":
            removed = [s for s in syms if s in st["watch"]]
            st["watch"] = [s for s in st["watch"] if s not in removed]
            text = f"🗑️ הוסרו מרשימת המעקב (מהמחשב): {', '.join(removed) or 'לא היו ברשימה'}"
        elif c.get("op") == "grpseed":
            n = group.seed(st, c.get("users") or {}, c.get("names") or {}, c.get("secid") or {})
            text = f"👥 טבלת האמינות של הקבוצה נטענה מהבדיקה ההיסטורית ({n} כותבים). לצפייה: /group"
        elif c.get("op") == "secid":       # OTC Markets ids looked up later at home: silent, the table stays
            st.setdefault("secid", {}).update(c.get("secid") or {})
            text = ""
        else:
            text = ""
        if text and c.get("note"):
            text += f"\n{c['note']}"
        if text and c.get("op") == "grpseed":
            run.notify(text)
        elif text:
            run.notify(text + f"\n\n📋 הרשימה: {' · '.join(sorted(st['watch'])) or 'ריקה'}")
        done.append(c.get("id"))
        log("inbox: command applied")
    st["inbox_done"] = done[-200:]
