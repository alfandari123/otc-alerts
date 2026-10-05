"""Reads a news item with Google Gemini (free tier) and scores it, with a Hebrew summary."""
import json
import re
import time

import requests

from . import config
from .net import session

URL = "https://generativelanguage.googleapis.com/v1beta/models/{}:generateContent"

PROMPT = """You are a sharp analyst helping a trader of US OTC penny stocks (mostly sub-penny, $0.0001-$0.01).
He wants to catch news that can push a stock price UP, for example:
- merger, reverse merger, acquisition, share exchange, letter of intent
- change of control, custodianship, new controlling shareholder, new management
- end of dilution: share cancellation/retirement, reduction of authorized shares, toxic/convertible debt paid off, lock-up
- ticker/name change tied to a new business, uplisting (tiers from best to worst: OTCQX, OTCQB, OTCID,
  Pink Limited, Expert Market/Grey), leaving the Expert Market, Caveat Emptor or shell flag removed,
  becoming an SEC reporting company
- big contracts, real revenue, buybacks
Bad for shareholders: new share issuance, convertible notes, Reg A / S-1 offerings, increase in authorized shares,
reverse split, going dark, delisting, bankruptcy, SEC trading suspension, Caveat Emptor.

Analyze the item below and answer ONLY with JSON:
{{
 "sentiment": "positive" | "negative" | "neutral",
 "score": integer 1-10, how likely this news is to push the share price UP
          (1 = bad or irrelevant, 5 = minor, 7 = meaningful catalyst, 9-10 = major catalyst such as a real merger or change of control),
 "category_he": "short category in Hebrew, 2-4 words",
 "summary_he": "1-3 short, simple Hebrew sentences: what happened and why it matters to the share price",
 "risk_he": "short Hebrew note on red flags or pump signs (vague promises, no numbers, promotion, dilution); empty string if none",
 "ticker": "the company's OTC ticker if it appears in the text, else empty string"
}}
Be skeptical: vague, promotional or routine news (conferences, minor partnerships, "exploring opportunities") gets a low score.

ITEM
Source: {src}
Company: {company}
Ticker: {tickers}
Tier: {tier}
Current price: {price}
Share structure: {shares}
Title: {title}
Text:
{text}
"""

calls = 0            # successful calls in this run
last_error = ""
_model = None        # first model that worked in this run
_dead = set()        # models that are missing or out of daily quota
_skip = set()        # models that are slow / overloaded right now (skipped for this run only)
_auth_failed = False


def usable():
    return (bool(config.GEMINI_API_KEY) and not _auth_failed
            and len(_dead | _skip) < len(config.GEMINI_MODELS))


def all_quota_dead():
    return len(_dead) >= len(config.GEMINI_MODELS)


def broken():
    """A lasting problem worth telling the owner about (not a one-off timeout)."""
    return _auth_failed or all_quota_dead()


def analyze(item, price=None):
    """Returns a dict, or None when the AI is unavailable."""
    global calls, last_error, _model, _auth_failed
    prompt = PROMPT.format(
        src=item.get("src_label", ""), company=item.get("company", ""),
        tickers=", ".join(item.get("syms") or []) or "unknown", tier=item.get("tier") or "unknown",
        price=f"${price}" if price else "unknown", shares=item.get("shares") or "unknown",
        title=item.get("title", ""), text=(item.get("text") or "")[:9000],
    )
    body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json"},
    }
    models = ([_model] if _model else []) + [m for m in config.GEMINI_MODELS if m != _model]
    for m in models:
        if m in _dead or m in _skip:
            continue
        for attempt in range(2):
            try:
                r = session.post(URL.format(m), json=body, timeout=40,
                                 headers={"x-goog-api-key": config.GEMINI_API_KEY})
            except requests.RequestException as e:
                last_error = type(e).__name__
                _skip.add(m)         # slow or unreachable right now: try the next model
                break
            if r.status_code >= 500:
                last_error = f"HTTP {r.status_code}"
                _skip.add(m)         # overloaded right now (e.g. 503 "high demand"): try the next model
                break
            if r.status_code == 200:
                res = _parse(r)
                if res:
                    _model = m
                    calls += 1
                    return res
                last_error = "bad AI answer"
                return None
            txt = r.text
            if r.status_code in (401, 403) or "API_KEY_INVALID" in txt or "API key not valid" in txt:
                _auth_failed = True
                last_error = "מפתח AI לא תקין"
                return None
            if r.status_code == 429 and "PerDay" not in txt and attempt == 0:
                time.sleep(20)       # per-minute limit: wait a bit and retry once
                continue
            # 404 model missing, 400 unsupported, 429 daily quota -> try the next model
            last_error = f"HTTP {r.status_code}"
            _dead.add(m)
            if _model == m:
                _model = None
            break
    return None


def _parse(r):
    try:
        parts = r.json()["candidates"][0]["content"]["parts"]
        text = "".join(p.get("text", "") for p in parts)
        text = re.sub(r"^```(?:json)?|```$", "", text.strip()).strip()
        d = json.loads(text)
        if isinstance(d, list):
            d = d[0]
        sentiment = str(d.get("sentiment", "neutral")).lower()
        if sentiment not in ("positive", "negative", "neutral"):
            sentiment = "neutral"
        score = max(1, min(10, int(float(d.get("score", 1)))))
        return {
            "sentiment": sentiment,
            "score": score,
            "category_he": str(d.get("category_he", ""))[:60],
            "summary_he": str(d.get("summary_he", ""))[:700],
            "risk_he": str(d.get("risk_he", ""))[:300],
            "ticker": re.sub(r"[^A-Z]", "", str(d.get("ticker", "")).upper())[:6],
        }
    except (KeyError, IndexError, ValueError, TypeError):
        return None
