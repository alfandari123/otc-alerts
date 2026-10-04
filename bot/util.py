"""Small shared helpers: logging, time zones, formatting."""
import html
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
IL = ZoneInfo("Asia/Jerusalem")

_TAGS = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"\s+")
_JUNK = re.compile(r"<(script|style|ix:header)\b.*?</\1>", re.S | re.I)


def log(msg):
    # Logs are public on GitHub: never log tickers, keys or URLs here.
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def et_now():
    return datetime.now(ET)


def il_time(ts):
    return datetime.fromtimestamp(ts, IL).strftime("%d/%m %H:%M")


def fmt_price(p):
    if p is None:
        return "?"
    if p < 1:
        return ("%.6f" % p).rstrip("0").rstrip(".")
    return "%.2f" % p


def esc(s):
    return html.escape(s or "", quote=False)


def html_to_text(raw):
    raw = raw.replace("<![CDATA[", "").replace("]]>", "")
    for _ in range(2):  # twice: feeds often contain HTML that is itself escaped
        raw = _JUNK.sub(" ", raw)
        raw = html.unescape(_TAGS.sub(" ", raw))
    return _SPACE.sub(" ", raw).strip()
