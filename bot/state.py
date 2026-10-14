"""The bot's memory between runs (watchlist, already-seen items...).

In the cloud it is stored encrypted (state.bin) on the `state` branch, because the repo is public.
Locally, without STATE_KEY, it is a plain JSON file for testing.
"""
import base64
import hashlib
import json
import os
import time

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

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
        "warm": [],             # sources whose existing items were already marked as seen
        "avgvol": {},           # ticker -> [day, average daily volume]
        "mvol_last": 0,
        "otc_actions_last": 0,
        "dil": {},              # ticker -> [day, dilution summary]
        "tso": {},              # ticker -> last seen outstanding/authorized shares (watchlist)
        "secid": {},            # ticker -> OTC Markets security id
        "dilw_last": 0,
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


# ---------- trading screen data (dashboard) ----------
# Stored next to state.bin; the web page decrypts it in the browser with a key that is only in the link
# Reuven gets from his bot (the part after # never leaves the browser).

DASH_ENC = "dash.bin"
DASH_PLAIN = "dash.local.json"


def dash_key():
    return hashlib.sha256((config.STATE_KEY + "|dash").encode()).digest() if config.STATE_KEY else b""


def dash_url():
    k = base64.urlsafe_b64encode(dash_key()).decode().rstrip("=")
    return f"{config.DASH_PAGE}#k={k}"


def save_dash(data):
    raw = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()
    if config.STATE_KEY:
        nonce = os.urandom(12)
        path, raw = DASH_ENC, base64.b64encode(nonce + AESGCM(dash_key()).encrypt(nonce, raw, None))
    else:
        path = DASH_PLAIN
    with open(path + ".tmp", "wb") as f:
        f.write(raw)
    os.replace(path + ".tmp", path)
