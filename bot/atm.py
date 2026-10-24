""""ATM stocks" (מניות כספומט): a stock that has bounced between the same floor and the same ceiling for 2-3 months.
Buy at the floor, sell just under the ceiling; out if the floor breaks (the pattern is over). Keep running past the
ceiling only when fresh strong news can push it higher.

find_range() works on daily bars [{"t","o","h","l","c","v"}] (oldest first) and is shared by the backtest
(tools/atm_backtest.py) and the live scanner.
"""
import math

WINDOW = 60            # trading days looked at (~3 months)
MIN_TOUCHES = 3        # times the floor / the ceiling were reached
MIN_SPREAD_DAYS = 20   # first and last touch at least this far apart
MIN_SWINGS = 2         # full floor -> ceiling -> floor trips
MIN_RATIO = 1.15       # ceiling above the floor by more than one tick's noise (no fixed % target: sell AT the ceiling)
MIN_PRICE = 0.0001     # trips included (buy 0.0001, sell 0.0002 is his classic round)
MAX_BREAK = 0.05       # share of days allowed to close outside the range
MIN_ACTIVE = 0.8       # share of days with trades
MIN_DOLLARS = 2000     # average dollar volume per day


def tick(p):
    return 0.0001 if p < 1 else 0.01


def _tol(p):
    """How close counts as touching a level: the exact tick for sub-penny prices, 3% above that."""
    return 1e-9 if p < 0.001 else max(tick(p), p * 0.03)


def _level(values, pct):
    v = sorted(values)
    return v[min(len(v) - 1, max(0, int(len(v) * pct)))]


def _grid(x, up):
    t = tick(x)
    n = math.ceil(x / t - 1e-9) if up else math.floor(x / t + 1e-9)
    return round(n * t, 4 if t < 0.01 else 2)


def find_range(bars):
    """{"floor", "ceiling", "touches_f", "touches_c", "swings", "dollars"} or None when there is no clean range."""
    w = bars[-WINDOW:]
    if len(w) < WINDOW * 0.75:
        return None
    active = [b for b in w if b["v"]]
    if len(active) < len(w) * MIN_ACTIVE:
        return None
    dollars = sum(b["c"] * b["v"] for b in w) / len(w)
    if dollars < MIN_DOLLARS:
        return None
    floor = _grid(_level([b["l"] for b in active], 0.10), False)
    ceil_ = _grid(_level([b["h"] for b in active], 0.90), True)
    if floor < MIN_PRICE or ceil_ < floor * MIN_RATIO:
        return None
    tf, tc = _tol(floor), _tol(ceil_)
    tf_idx = [i for i, b in enumerate(w) if b["v"] and b["l"] <= floor + tf]
    tc_idx = [i for i, b in enumerate(w) if b["v"] and b["h"] >= ceil_ - tc]
    if len(tf_idx) < MIN_TOUCHES or len(tc_idx) < MIN_TOUCHES:
        return None
    if tf_idx[-1] - tf_idx[0] < MIN_SPREAD_DAYS or tc_idx[-1] - tc_idx[0] < MIN_SPREAD_DAYS:
        return None
    outside = sum(1 for b in active if b["c"] < floor - tf or b["c"] > ceil_ + tc)
    if outside > len(active) * MAX_BREAK:
        return None
    # swings: walk the days and count trips floor -> ceiling -> floor
    side, trips = None, 0
    for i in range(len(w)):
        if i in tf_idx and side != "f":
            if side == "c":
                trips += 0.5
            side = "f"
        elif i in tc_idx and side != "c":
            if side == "f":
                trips += 0.5
            side = "c"
    if trips < MIN_SWINGS:
        return None
    # the floor must not be sinking (a diluter selling at the ceiling pushes it down step by step)
    half = len(active) // 2
    if min(b["l"] for b in active[half:]) < min(b["l"] for b in active[:half]) - tf:
        return None
    return {"floor": floor, "ceiling": ceil_, "touches_f": len(tf_idx), "touches_c": len(tc_idx),
            "swings": trips, "dollars": dollars}


DUMP_X = 3.0           # checklist: a day with this many times the 30-day average volume...
DUMP_DAYS = 30         # ...that closed lower (and did not break out up) = a dump, likely dilution
LOOKBACK = 120         # checklist: days before the range window looked at for much lower prices
RAN_UP = 0.5           # ...if the stock traded under half the floor there, holders from far lower can dump it back


def checklist(bars, rng):
    """His ATM checklist on daily bars: '' when the stock passes, else a short Hebrew reason.
    Dump days (heavy volume + lower close, no breakout up) hint at a diluter; a range far above where the stock traded
    a few months ago has holders from much lower prices who can push it back down."""
    w = bars[-DUMP_DAYS:]
    avg = sum(b["v"] for b in w) / max(1, len(w))
    for i in range(1, len(w)):
        b, prev = w[i], w[i - 1]
        top = max(x["h"] for x in w[max(0, i - 5):i])
        if avg and b["v"] >= DUMP_X * avg and b["c"] < prev["c"] - 1e-9 and b["h"] <= top + 1e-9:
            return "יום זריקה בווליום חריג בחודש האחרון (סימן לדילול)"
    before = bars[-(WINDOW + LOOKBACK):-WINDOW]
    if before and min(b["l"] for b in before if b["v"] or True) < rng["floor"] * RAN_UP - 1e-9:
        return "המניה נסחרה במחירים נמוכים בהרבה בחודשים האחרונים (מחזיקים יכולים לזרוק אותה בחזרה)"
    return ""


def plan(rng, fee, budget, rr=3.0):
    """Entry / stop / target for a range trade, or None when 1:3 after fees is not possible."""
    f, c = rng["floor"], rng["ceiling"]
    t = tick(f)
    entry = _grid(f + t, True)            # buy one tick above the floor (the ask)
    stop = _grid(f - t, False)            # floor broken
    stop_fill = max(stop - t, t)
    target = _grid(c - t, False)          # sell just under the ceiling, before the crowd
    shares = int(budget / entry)
    if shares <= 0:
        return None
    gain = shares * (target - entry) - 2 * fee
    loss = shares * (entry - stop_fill) + 2 * fee
    if gain < rr * loss:
        return None
    return {"entry": entry, "stop": stop, "stop_fill": stop_fill, "target": target, "shares": shares,
            "gain": gain, "loss": loss}


def plan_ticks(rng, fee, budget, min_gain=0.0):
    """His way: a buy order waiting AT the floor, a sell order waiting AT the ceiling, no price stop (an exit only
    for dilution or a floor that is gone for good). None when a round would not make money after fees."""
    f, c = rng["floor"], rng["ceiling"]
    shares = int(budget / f)
    if shares <= 0:
        return None
    gain = shares * (c - f) - 2 * fee
    if gain < min_gain * shares * f:
        return None
    return {"entry": f, "target": c, "shares": shares, "gain": gain}
