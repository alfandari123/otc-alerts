"""Share facts read from the filing itself (merger / share exchange / offering), so an alert shows the REAL dilution
of the deal instead of last quarter's share count, and what KIND of dilution it is.

Lesson (STWI, 14.10.2026): the 8-K issued 183,098,434 restricted shares to the sellers (97.3%): 5.04M -> 188.1M shares,
authorized raised from 75M to 800M - while the quarterly data still said "5M shares, 75M authorized".
"""
import re

NUM = r"(\d{1,3}(?:,\d{3})+|\d{6,})"
_SP = re.compile(r"\s+")

OUTSTANDING = [re.compile(p, re.I) for p in (
    NUM + r" shares of (?:our |the Company'?s? )?common stock(?:, par value [^,]{1,30},)? (?:were |are )?(?:issued and )?outstanding",
    r"(?:there were|we had|has) " + NUM + r" shares of (?:our )?common stock (?:issued and )?outstanding",
)]
ISSUED = re.compile(r"(?<!to )(?<!may be )issued (?:to the \w+ )?(?:an aggregate of |a total of )?(?:approximately )?" + NUM
                    + r" (?:restricted |newly[- ]issued |unregistered )?shares", re.I)
PCT = re.compile(r"(?:represent|constitut)\w* (?:approximately |about )?(\d{1,2}(?:\.\d+)?)%", re.I)
AUTH = re.compile(r"authorized to issue (?:up to )?" + NUM + r" shares of (?:our )?common stock", re.I)
RESTRICTED = re.compile(r"restricted securities|Rule 144|Section 4\(a\)\(2\)|Regulation S\b|Regulation D\b|Rule 506", re.I)
TOXIC = re.compile(r"convertible (?:promissory )?notes?|conversion price|discount to the (?:market|lowest)|variable (?:conversion|price)"
                   r"|equity line|equity purchase agreement|commitment shares|at[- ]the[- ]market offering|resale registration", re.I)
MERGER = re.compile(r"share exchange|reverse merger|merger agreement|acquisition of all|business combination", re.I)
LOCKUP = re.compile(r"lock[- ]?up|leak[- ]?out", re.I)


def _n(s):
    return int(s.replace(",", ""))


def facts(text, items=()):
    """Numbers and the kind of issuance found in the filing text ({} when nothing useful)."""
    if not text:
        return {}
    t = _SP.sub(" ", text)
    out = {}
    outs = [_n(m.group(1)) for rx in OUTSTANDING for m in rx.finditer(t)]
    if outs:
        out["after"] = max(outs)          # after a share exchange the post-closing count is the biggest one
    issued = [_n(m.group(1)) for m in ISSUED.finditer(t)]
    if issued:
        out["issued"] = max(issued)
    m = PCT.search(t)
    if m:
        out["pct"] = float(m.group(1))
    auth = [_n(m.group(1)) for m in AUTH.finditer(t)]
    if auth:
        out["auth"] = max(auth)
    out["restricted"] = bool(RESTRICTED.search(t))
    out["toxic"] = bool(TOXIC.search(t))
    out["merger"] = bool(MERGER.search(t)) or bool({"2.01", "5.01"} & set(items))
    out["lockup"] = bool(LOCKUP.search(t))
    return out if (out.get("after") or out.get("issued") or out.get("auth")) else {}


def kind(f):
    """'merger_restricted' / 'toxic' / 'unknown'."""
    if not f:
        return "unknown"
    if f.get("toxic"):
        return "toxic"
    if f.get("merger") and f.get("restricted"):
        return "merger_restricted"
    return "unknown"


def _fmt(x):
    from .dilution import fmt
    return fmt(x)


def lines(f, before=None):
    """Hebrew lines for an alert. `before` = share count known before the deal (quarterly data)."""
    if not f:
        return []
    out = []
    after, issued = f.get("after"), f.get("issued")
    if after and before and after > before * 1.3:
        out.append(f"⚠️ לפי הדיווח: אחרי העסקה {_fmt(after)} מניות (פי {after / before:.0f} מ-{_fmt(before)})")
    elif after:
        out.append(f"📄 לפי הדיווח: {_fmt(after)} מניות במחזור")
    if issued:
        out.append(f"📄 הונפקו {_fmt(issued)} מניות" + (f" (כ-{f['pct']:g}% מהחברה)" if f.get("pct") else ""))
    if f.get("auth"):
        warn = after and after >= f["auth"] * 0.9
        out.append(("⚠️ " if warn else "📄 ") + f"מניות מורשות לפי הדיווח: {_fmt(f['auth'])}")
    k = kind(f)
    if k == "merger_restricted":
        out.append("ℹ️ סוג הדילול: מניות חסומות (Rule 144) לבעלי השליטה החדשים – לא נמכרות בשוק לפחות 6 חודשים; "
                   "גם אחר כך בעלי שליטה מוגבלים בכמות. זה לא מדלל שמוכר בשוק."
                   + (" יש גם הסכם נעילה (Lock-up)." if f.get("lockup") else ""))
    elif k == "toxic":
        out.append("🔴 סוג הדילול: חוב / מימון שמומר למניות – מניות כאלה בדרך כלל נמכרות בשוק (דילול רעיל)")
    return out


def ai_text(f):
    if not f:
        return ""
    parts = []
    for k, label in (("after", "shares outstanding after the deal"), ("issued", "shares issued"),
                     ("auth", "authorized shares")):
        if f.get(k):
            parts.append(f"{label}: {f[k]:,}")
    if f.get("pct"):
        parts.append(f"issued shares = {f['pct']}% of the company")
    parts.append("issuance kind: " + kind(f))
    return "; ".join(parts)
