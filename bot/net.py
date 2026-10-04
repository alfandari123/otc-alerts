"""HTTP helpers. Errors never carry URLs (they may contain keys or tickers)."""
import time

import requests

from . import config

session = requests.Session()


class HttpError(Exception):
    def __init__(self, status, detail=""):
        super().__init__(f"HTTP {status} {detail}".strip())
        self.status = status


def _request(method, url, ua=None, timeout=20, retries=1, **kw):
    headers = kw.pop("headers", {})
    headers.setdefault("User-Agent", ua or config.BROWSER_UA)
    for attempt in range(retries + 1):
        try:
            r = session.request(method, url, headers=headers, timeout=timeout, **kw)
        except requests.RequestException as e:
            if attempt < retries:
                time.sleep(2)
                continue
            raise HttpError(0, type(e).__name__) from None
        if r.status_code >= 500 and attempt < retries:
            time.sleep(2)
            continue
        if r.status_code != 200:
            raise HttpError(r.status_code)
        return r


def get(url, **kw):
    return _request("GET", url, **kw)


def post(url, **kw):
    return _request("POST", url, **kw)


def short_err(e):
    if isinstance(e, HttpError):
        return str(e)
    if isinstance(e, RuntimeError):
        return str(e)[:80]
    return type(e).__name__
