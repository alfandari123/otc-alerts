"""FINRA OTC Daily List: ticker/name changes, splits, deletions, reinstatements."""
from datetime import datetime, timedelta

from .. import net
from ..util import ET, et_now

URL = "https://api.finra.org/data/group/otcMarket/name/otcDailyList"
SKIP = ("Dividend", "Round Lot")


def fetch(st):
    today = et_now().date()
    body = {"limit": 5000, "dateRangeFilters": [{
        "fieldName": "dailyListDatetime",
        "startDate": str(today - timedelta(days=4)), "endDate": str(today)}]}
    rows = net.post(URL, json=body, headers={"Accept": "application/json"}, timeout=40).json()
    items = []
    for r in rows:
        reason = r.get("dailyListReasonDescription") or ""
        if not reason or any(s in reason for s in SKIP):
            continue
        old, new = r.get("oldSymbolCode"), r.get("newSymbolCode")
        syms = [s for s in dict.fromkeys([new, old]) if s]
        old_name, new_name = r.get("oldSecurityDescription") or "", r.get("newSecurityDescription") or ""
        details = []
        if old and new and old != new:
            details.append(f"טיקר ישן: {old} · טיקר חדש: {new}")
        if r.get("changeSecurityDescriptionFlag") == "Y" and new_name and new_name != old_name:
            details.append(f"שם חדש: {new_name}" + (f" (היה: {old_name})" if old_name else ""))
        if r.get("reverseSplitRate"):
            details.append(f"יחס ספליט הפוך: {r['reverseSplitRate']}")
        if r.get("forwardSplitRate"):
            details.append(f"יחס פיצול: {r['forwardSplitRate']}")
        if r.get("exDate"):
            details.append(f"בתוקף מ: {r['exDate'][:10]}")
        if r.get("commentText"):
            details.append(r["commentText"][:300])
        try:
            ts = datetime.strptime(r["dailyListDatetime"][:10], "%Y-%m-%d").replace(tzinfo=ET).timestamp()
        except (KeyError, TypeError, ValueError):
            ts = 0
        items.append({
            "id": f"finra:{r.get('OTCDailyListID')}",
            "src": "FINRA",
            "src_label": f"FINRA Daily List – {reason}",
            "reason": reason,
            "dsuffix": _d_suffix(old, new),
            "company": new_name or old_name,
            "syms": syms,
            "title": f"{reason}: {new_name or old_name}",
            "text": "\n".join(details),
            "url": "https://otce.finra.org/otce/dailyList",
            "ts": ts,
        })
    return items


def _d_suffix(old, new):
    """A fifth letter "D" is added for ~20 trading days after a reverse split, then removed."""
    if old and new and new == old + "D":
        return "added"
    if old and new and old == new + "D":
        return "removed"
    return ""
