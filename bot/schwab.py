"""Charles Schwab Trader API (quotes, orders, positions).

Login is handled by our small Cloudflare worker (schwab-worker/): Reuven logs in from a Telegram link once a week,
the worker keeps the tokens and gives this module a fresh 30-minute access token. Nothing here works until the
Schwab app is approved and its key/secret are stored in the worker.
"""
import time

from . import config, net

API = "https://api.schwabapi.com"

_token = {"value": "", "exp": 0}
_status = {"at": 0, "data": None}


class NotConnected(Exception):
    pass


def enabled():
    return bool(config.SCHWAB_BOT_KEY)


def _worker(path):
    r = net.session.get(config.SCHWAB_WORKER + path, headers={"X-Bot-Key": config.SCHWAB_BOT_KEY}, timeout=20)
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, {}


def status(max_age=600):
    """{"configured", "connected", "days_left"} from the worker (cached), or None when unreachable."""
    if not enabled():
        return None
    if _status["data"] is not None and time.time() - _status["at"] < max_age:
        return _status["data"]
    try:
        code, data = _worker("/status")
        _status.update(at=time.time(), data=data if code == 200 else None)
    except Exception:
        _status.update(at=time.time(), data=None)
    return _status["data"]


def login_link():
    return f"{config.SCHWAB_WORKER}/login?k={config.SCHWAB_LOGIN_KEY}"


def token():
    if _token["value"] and _token["exp"] - time.time() > 60:
        return _token["value"]
    if not enabled():
        raise NotConnected("no bot key")
    code, data = _worker("/token")
    if code != 200 or not data.get("access_token"):
        raise NotConnected(data.get("error") or f"worker {code}")
    _token.update(value=data["access_token"], exp=time.time() + 25 * 60)
    return _token["value"]


def _call(method, path, **kw):
    headers = {"Authorization": "Bearer " + token(), "Accept": "application/json"}
    r = net.session.request(method, API + path, headers=headers, timeout=20, **kw)
    if r.status_code == 401:
        _token["value"] = ""
        raise NotConnected("401")
    if r.status_code >= 400:
        raise net.HttpError(r.status_code, r.text[:100].replace("\n", " "))
    return r


# ---------- market data ----------

def quotes(syms):
    """{sym: {"bid", "ask", "last", "vol"}} - real bid/ask, unlike Yahoo."""
    if not syms:
        return {}
    j = _call("GET", "/marketdata/v1/quotes", params={"symbols": ",".join(syms), "fields": "quote"}).json()
    out = {}
    for sym, v in j.items():
        q = v.get("quote") or {}
        out[sym] = {"bid": q.get("bidPrice"), "ask": q.get("askPrice"), "last": q.get("lastPrice"),
                    "vol": q.get("totalVolume"), "bid_size": q.get("bidSize"), "ask_size": q.get("askSize")}
    return out


# ---------- account ----------

def account_hash(st):
    """Schwab wants an encrypted account id in every account call (cached in the bot's memory)."""
    h = st.setdefault("schwab", {}).get("acct")
    if h:
        return h
    rows = _call("GET", "/trader/v1/accounts/accountNumbers").json()
    if not rows:
        raise NotConnected("no account")
    st["schwab"]["acct"] = rows[0]["hashValue"]
    return st["schwab"]["acct"]


def account(st):
    """Cash available for trading and current positions {sym: shares}."""
    j = _call("GET", f"/trader/v1/accounts/{account_hash(st)}", params={"fields": "positions"}).json()
    a = j.get("securitiesAccount") or {}
    bal = a.get("currentBalances") or {}
    pos = {}
    for p in a.get("positions") or []:
        sym = (p.get("instrument") or {}).get("symbol")
        if sym:
            pos[sym] = int((p.get("longQuantity") or 0) - (p.get("shortQuantity") or 0))
    cash = bal.get("cashAvailableForTrading", bal.get("availableFunds", 0)) or 0
    return {"cash": cash, "pos": pos}


def _price(p):
    return f"{p:.4f}" if p < 1 else f"{p:.2f}"


def place(st, sym, side, qty, price):
    """Limit DAY order; returns the order id."""
    body = {"orderType": "LIMIT", "session": "NORMAL", "duration": "DAY", "orderStrategyType": "SINGLE",
            "price": _price(price),
            "orderLegCollection": [{"instruction": side, "quantity": int(qty),
                                    "instrument": {"symbol": sym, "assetType": "EQUITY"}}]}
    r = _call("POST", f"/trader/v1/accounts/{account_hash(st)}/orders", json=body)
    loc = r.headers.get("Location", "")
    oid = loc.rstrip("/").rsplit("/", 1)[-1]
    if not oid.isdigit():
        raise net.HttpError(r.status_code, "no order id")
    return oid


def order(st, oid):
    """{"status", "filled", "remaining", "avg"} for one of our orders."""
    j = _call("GET", f"/trader/v1/accounts/{account_hash(st)}/orders/{oid}").json()
    filled = j.get("filledQuantity") or 0
    total = cost = 0.0
    for act in j.get("orderActivityCollection") or []:
        for leg in act.get("executionLegs") or []:
            q, p = leg.get("quantity") or 0, leg.get("price") or 0
            total += q
            cost += q * p
    return {"status": j.get("status", ""), "filled": int(filled), "remaining": int(j.get("remainingQuantity") or 0),
            "avg": (cost / total) if total else None}


def cancel(st, oid):
    _call("DELETE", f"/trader/v1/accounts/{account_hash(st)}/orders/{oid}")


# ---------- live stream snapshot: Level 1 + Level 2 (book) ----------
# The stream needs the streamer details from /userPreference and a login message with the access token.
# Which book service carries OTC stocks is NOT verified yet - every candidate service is tried.

BOOKS = ("NASDAQ_BOOK", "NYSE_BOOK")
L1_FIELDS = {0: "sym", 1: "bid", 2: "ask", 3: "last", 4: "bid_size", 5: "ask_size", 8: "vol", 9: "last_size"}


def _levels(rows):
    out = []
    for x in rows or []:
        mms = [{"id": y.get("0"), "size": y.get("1") or 0} for y in x.get("3") or []]
        out.append({"price": x.get("0"), "size": x.get("1") or 0, "mm": mms})
    return out


def snapshot(syms, seconds=4.0):
    """{sym: {"l1": {...}, "bids": [...], "asks": [...], "book": service}} after listening `seconds`."""
    import json
    import websocket   # websocket-client

    pref = _call("GET", "/trader/v1/userPreference").json()
    si = (pref.get("streamerInfo") or [{}])[0]
    ws = websocket.create_connection(si["streamerSocketUrl"], timeout=8)
    rid = [0]

    def send(service, command, params):
        rid[0] += 1
        ws.send(json.dumps({"requests": [{"service": service, "command": command, "requestid": str(rid[0]),
                                          "SchwabClientCustomerId": si["schwabClientCustomerId"],
                                          "SchwabClientCorrelId": si["schwabClientCorrelId"], "parameters": params}]}))
    out = {s: {"l1": {}, "bids": [], "asks": [], "book": ""} for s in syms}
    try:
        send("ADMIN", "LOGIN", {"Authorization": token(), "SchwabClientChannel": si["schwabClientChannel"],
                                "SchwabClientFunctionId": si["schwabClientFunctionId"]})
        keys = ",".join(syms)
        send("LEVELONE_EQUITIES", "SUBS", {"keys": keys, "fields": ",".join(str(k) for k in L1_FIELDS)})
        for b in BOOKS:
            send(b, "SUBS", {"keys": keys, "fields": "0,1,2,3"})
        end = time.time() + seconds
        while time.time() < end:
            ws.settimeout(max(0.2, end - time.time()))
            try:
                msg = json.loads(ws.recv())
            except websocket.WebSocketTimeoutException:
                break
            for d in msg.get("data") or []:
                for c in d.get("content") or []:
                    row = out.get(c.get("key"))
                    if row is None:
                        continue
                    if d.get("service") == "LEVELONE_EQUITIES":
                        row["l1"].update({L1_FIELDS[int(k)]: v for k, v in c.items() if k.isdigit() and int(k) in L1_FIELDS})
                    elif d.get("service") in BOOKS and (c.get("2") or c.get("3")):
                        row.update(bids=_levels(c.get("2")), asks=_levels(c.get("3")), book=d["service"])
    finally:
        ws.close()
    return out
