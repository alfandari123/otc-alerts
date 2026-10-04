"""The bot's memory between runs (watchlist, already-seen items...).

In the cloud it is stored encrypted (state.bin) on the `state` branch, because the repo is public.
Locally, without STATE_KEY, it is a plain JSON file for testing.
"""
import json
import os
import time

from cryptography.fernet import Fernet

from . import config

PATH_ENC = "state.bin"
PATH_PLAIN = "state.local.json"


def fresh():
    return {
        "v": 1,
        "owner": None,          # Telegram chat id of the owner (first /start)
        "tg_offset": 0,
        "watch": [],
        "seen": {},             # item id -> unix time
        "prn_checked": {},      # PR Newswire release -> [time, OTC tickers]
        "ai": {"day": "", "n": 0, "dead_day": "", "model": ""},
        "vol": {},              # ticker -> day of last volume alert
        "vol_last": 0,
        "boot": False,
        "health": {},           # source -> {"ok": ts, "err": str, "warned": bool}
        "runs": 0,
        "last_run": 0,
        "sent": {"day": "", "n": 0},
    }


def load():
    st = None
    if config.STATE_KEY:
        if os.path.exists(PATH_ENC):
            with open(PATH_ENC, "rb") as f:
                data = Fernet(config.STATE_KEY.encode()).decrypt(f.read())
            st = json.loads(data)
    elif os.path.exists(PATH_PLAIN):
        with open(PATH_PLAIN, encoding="utf-8") as f:
            st = json.load(f)
    base = fresh()
    if st:
        base.update(st)
    return base


def save(st):
    data = json.dumps(st, ensure_ascii=False, separators=(",", ":")).encode()
    if config.STATE_KEY:
        path, data = PATH_ENC, Fernet(config.STATE_KEY.encode()).encrypt(data)
    else:
        path = PATH_PLAIN
    with open(path + ".tmp", "wb") as f:
        f.write(data)
    os.replace(path + ".tmp", path)


def prune(st):
    cutoff = time.time() - config.SEEN_TTL_DAYS * 86400
    st["seen"] = {k: v for k, v in st["seen"].items() if v >= cutoff}
