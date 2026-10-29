"""Backtest the Elevate Trading group calls (👀 / entry posts) - LOCAL ONLY (group data never goes to the repo).

    python tools/group_backtest.py [messages=6000]

Reads the group with the saved user session (~/.otc-secrets), finds ticker calls, prices them with Yahoo 5-minute
bars (last 60 days) and writes per-call and per-user results to ~/.otc-secrets/group_backtest.txt, plus the bot's
scoreboard seed to ~/.otc-secrets/group_seed.json. Do not run it while the cloud bot uses the same session.
"""
import json
import os
import sys
import time
from collections import defaultdict
from statistics import median

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests
from telethon.sync import TelegramClient
from telethon.sessions import StringSession

from bot import dilution, group, market

SECRETS = os.path.join(os.path.expanduser("~"), ".otc-secrets")
N = int(sys.argv[1]) if len(sys.argv) > 1 else 6000
BARS = "https://query1.finance.yahoo.com/v8/finance/chart/{}?interval=5m&range=60d&includePrePost=false"
UA = {"User-Agent": "Mozilla/5.0"}
DAY = 86400

_bars = {}


def bars(sym):
    if sym not in _bars:
        out, otc = [], False
        try:
            r = requests.get(BARS.format(sym), headers=UA, timeout=20).json()["chart"]["result"]
            if r:
                r = r[0]
                otc = market.is_otc_meta(r["meta"])
                q = r["indicators"]["quote"][0]
                for i, t in enumerate(r.get("timestamp") or []):
                    h, l, c, v = q["high"][i], q["low"][i], q["close"][i], q["volume"][i]
                    if None not in (h, l, c):
                        out.append((t, h, l, c, v or 0))
        except Exception:
            pass
        _bars[sym] = (out, otc)
        time.sleep(0.3)
    return _bars[sym]


def outcome(sym, ts):
    b, otc = bars(sym)
    return group.outcome(b, ts) if otc else None


def main():
    cfg = json.load(open(os.path.join(SECRETS, "tg_user.json"), encoding="utf-8-sig"))
    sess = open(os.path.join(SECRETS, "tg_session.txt"), encoding="utf-8").read().strip()
    calls, names = [], {}
    with TelegramClient(StringSession(sess), cfg["api_id"], cfg["api_hash"]) as c:
        for m in c.iter_messages(group.GROUP_ID, limit=N):
            if time.time() - m.date.timestamp() > 58 * DAY:
                break
            sig = group.parse(m.message or "")
            if not sig:
                continue
            uid = m.sender_id or 0
            if m.sender and uid not in names:
                names[uid] = (getattr(m.sender, "first_name", None) or getattr(m.sender, "title", "") or "")[:20]
            for sym in sig["syms"]:
                calls.append({"uid": uid, "sym": sym, "kind": sig["kind"], "ts": m.date.timestamp(),
                              "text": (m.message or "")[:60].replace("\n", " ")})
    names[0] = "אנונימי (מנהל)"
    rows = []
    for cl in sorted(calls, key=lambda x: x["ts"]):
        o = outcome(cl["sym"], cl["ts"])
        if o:
            rows.append({**cl, **o})

    out = open(os.path.join(SECRETS, "group_backtest.txt"), "w", encoding="utf-8")
    out.write(f"calls found {len(calls)}, priced OTC calls {len(rows)}\n\n")

    def summary(label, rs):
        if not rs:
            return
        m3 = [r["max3"] for r in rs if "max3" in r]
        c3 = [r["close3"] for r in rs if "close3" in r]
        n20 = sum(1 for r in rs if r["first"] == "+20%")
        n15 = sum(1 for r in rs if r["first"] == "-15%")
        out.write(f"{label:28s} n={len(rs):3d} | +20% before -15%: {n20}/{n20 + n15} | "
                  f"max 3d median {median(m3) * 100:+.0f}% | close 3d median {median(c3) * 100:+.0f}% "
                  f"mean {sum(c3) / len(c3) * 100:+.0f}%\n" if m3 and c3 else f"{label} n={len(rs)} (too new)\n")

    for kind in ("eyes", "entry", "tweet", "mention"):
        summary(f"KIND {kind}", [r for r in rows if r["kind"] == kind])
    summary("ALL", rows)
    out.write("\nBY USER (eyes+entry):\n")
    by = defaultdict(list)
    for r in rows:
        if r["kind"] != "mention":
            by[r["uid"]].append(r)
    for uid, rs in sorted(by.items(), key=lambda kv: -len(kv[1])):
        summary(f"{names.get(uid, '?')} [{uid}]", rs)
    out.write("\nCALLS (eyes+entry):\n")
    for r in rows:
        if r["kind"] == "mention":
            continue
        out.write(f"{time.strftime('%m-%d %H:%M', time.localtime(r['ts']))} {r['kind']:5s} {r['sym']:5s} "
                  f"{names.get(r['uid'], '?')[:12]:12s} entry {r['entry']:.4f} late {r['late_min']:.0f}m "
                  f"max1 {r.get('max1', 0) * 100:+5.0f}% max3 {r.get('max3', 0) * 100:+5.0f}% "
                  f"close3 {r.get('close3', 0) * 100:+5.0f}% first {r['first']} | {r['text']}\n")
    out.close()

    # scoreboard seed for the bot (python tools/bot_cmd.py grpseed): same numbers as "BY USER" above
    seed = {"users": {}}
    for r in rows:
        if r["kind"] != "mention" and "close3" in r:
            group.add_result(seed, r["uid"], r)
    secid = {}
    for sym in sorted({r["sym"] for r in rows}):
        try:
            sid = dilution.resolve_id(sym)
        except Exception:
            sid = None
        if sid:
            secid[sym] = sid
        time.sleep(1)                                   # OTC Markets throttles fast lookups
    json.dump({"users": seed["users"], "names": {k: v for k, v in names.items() if k}, "secid": secid},
              open(os.path.join(SECRETS, "group_seed.json"), "w", encoding="utf-8"), ensure_ascii=False)
    print("done", len(calls), len(rows), "ids", len(secid))


if __name__ == "__main__":
    main()
