"""Telegram Bot API: send messages and read the owner's commands."""
import requests

from . import config
from .net import HttpError, session

API = "https://api.telegram.org/bot{}/{}"
MAX_LEN = 4000


def call(method, **params):
    try:
        r = session.post(API.format(config.TELEGRAM_TOKEN, method), json=params, timeout=30)
    except requests.RequestException as e:
        raise HttpError(0, type(e).__name__) from None  # never let the token-bearing URL reach the logs
    try:
        data = r.json()
    except ValueError:
        raise HttpError(r.status_code) from None
    if not data.get("ok"):
        raise HttpError(r.status_code, data.get("description", "")[:80])
    return data["result"]


def send(chat_id, text):
    while text:
        part, text = text[:MAX_LEN], text[MAX_LEN:]
        call("sendMessage", chat_id=chat_id, text=part, parse_mode="HTML",
             link_preview_options={"is_disabled": True})


def updates(offset):
    return call("getUpdates", offset=offset, timeout=0, allowed_updates=["message"])
