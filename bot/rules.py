"""Cheap first filter (before the AI): filing types, 8-K items, FINRA events and keywords.

Every rule is (polarity, strength, Hebrew label):
polarity +1 good for the share price / -1 bad / 0 depends on details; strength 1 (weak) - 3 (strong).
"""
import re

SEC_ITEMS = {
    "5.01": (+1, 3, "החלפת שליטה"),
    "2.01": (+1, 3, "השלמת רכישה / מיזוג"),
    "5.06": (+1, 3, "שינוי סטטוס שלד (Shell)"),
    "1.01": (+1, 2, "הסכם מהותי (ייתכן מיזוג)"),
    "5.03": (0, 2, "שינוי תקנון (שם / מניות מורשות / ספליט)"),
    "3.03": (0, 2, "שינוי בזכויות בעלי המניות"),
    "5.02": (+1, 1, "שינוי בהנהלה"),
    "3.02": (-1, 2, "הנפקת מניות (דילול)"),
    "1.03": (-1, 3, "פשיטת רגל"),
    "3.01": (-1, 2, "הודעה על מחיקה מהמסחר"),
    "4.02": (-1, 1, "תיקון דוחות כספיים"),
    "1.02": (-1, 1, "ביטול הסכם מהותי"),
    "2.03": (-1, 1, "התחייבות פיננסית / הלוואה"),
    "8.01": (0, 1, "אירוע אחר"),
    "7.01": (0, 1, "הודעה לשוק"),
}

# Checked longest-first, so "1-A-W" wins over "1-A".
SEC_FORMS = {
    "SC 14F1": (+1, 3, "החלפת רוב הדירקטורים (החלפת שליטה)"),
    "SCHEDULE 13D": (+1, 2, "משקיע צבר מעל 5% מהמניות"),
    "PRE 14C": (0, 2, "הודעה לבעלי המניות (שם / ספליט / מניות מורשות)"),
    "DEF 14C": (0, 2, "הודעה לבעלי המניות (שם / ספליט / מניות מורשות)"),
    "10-12G": (+1, 2, "רישום מלא ב-SEC (הופכת לחברה מדווחת)"),
    "1-A-W": (+1, 2, "ביטול הנפקת Reg A"),
    "1-A": (-1, 2, "הנפקת Reg A (דילול)"),
    "S-1": (-1, 2, "תשקיף הנפקה (דילול)"),
    "RW": (+1, 1, "משיכת תשקיף הנפקה"),
    "15-12G": (-1, 3, "הפסקת דיווח ל-SEC"),
    "15-15D": (-1, 3, "הפסקת דיווח ל-SEC"),
}

# FINRA daily list: (polarity, strength, label, alert also in the market-wide scan?)
FINRA_REASONS = [
    ("Listed on", (+1, 3, "עלייה לבורסה ראשית", True)),
    ("Delisted from", (-1, 2, "ירידה מהבורסה ל-OTC", False)),
    ("Symbol", (+1, 2, "שינוי טיקר", True)),
    ("Name", (+1, 2, "שינוי שם", True)),
    ("Reinstatement", (+1, 2, "חזרה למסחר", True)),
    ("Reverse Split", (-1, 2, "ספליט הפוך", False)),
    ("Forward Split", (0, 1, "פיצול מניות (ספליט רגיל)", False)),
    ("Acquisition/Merger", (0, 2, "מחיקה בעקבות מיזוג / רכישה", False)),
    ("Inactive", (-1, 2, "מחיקה מהמסחר", False)),
    ("Charter Cancelled", (-1, 3, "ביטול התאגדות", False)),
    ("Suspended", (-1, 2, "השעיה", False)),
    ("Liquidation", (-1, 2, "פירוק", False)),
    ("Bankruptcy", (-1, 3, "פשיטת רגל", False)),
    ("Addition", (0, 1, "מניה חדשה במסחר", False)),
]

CAVEAT_REMOVED = r"caveat emptor\b.{0,40}\b(remov\w*|lift\w*)|(remov\w*|lift\w*)\b.{0,40}\bcaveat emptor"

KEYWORDS = [
    (+1, 3, "החלפת שליטה",
     r"changes? (of|in) control|controlling (interest|shareholder|stake)|majority (stake|interest|shareholder)"),
    (+1, 3, "מיזוג / רכישה",
     r"reverse merger|merger agreement|agreement and plan of merger|share exchange agreement"
     r"|definitive agreement to (acquire|merge)|to be acquired|complet\w* (the |its )?(acquisition|merger)"),
    (+1, 3, "Custodianship", r"custodianship|court[- ]appointed custodian|\bcustodian\b"),
    (+1, 2, "כוונה למיזוג / רכישה",
     r"letter of intent|\bLOI\b|acquisition of|to acquire|\bmerger\b|\bacquires\b|\bacquired\b"),
    (+1, 2, "סוף הדילול",
     r"cancel+(ed|ation|ing|s)?\b.{0,60}\bshares|shares\b.{0,40}\b(cancel+ed|retired|returned to (the )?treasury)"
     r"|(reduc|decreas)\w*\b.{0,30}\bauthori[sz]ed|(paid off|pays off|retire[sd]?|eliminat\w*|extinguish\w*|settle[sd]?)"
     r"\b.{0,50}\b(convertible|toxic|notes?|debt)|no (further |more |future )?dilution|lock[- ]?up agreement"
     r"|share buy ?back|repurchase (program|plan)"),
    (+1, 2, "שדרוג דרגת מסחר",
     r"uplist\w*|(approved|accepted|begins? trading|move[sd]?|graduat\w*|upgrade[sd]?) (on|to|for) (the )?(OTCQB|OTCQX)"
     r"|pink current|current information tier|shell (status|designation|risk)\b.{0,40}\b(remov\w*|lift\w*)"
     r"|DTC eligib\w*|" + CAVEAT_REMOVED),
    (+1, 2, "שינוי שם / טיקר",
     r"name change|symbol change|new (ticker|trading symbol|symbol)|ticker (symbol )?change"),
    (+1, 1, "הנהלה חדשה", r"(appoint\w*|names|new)\b.{0,20}\b(CEO|chief executive|president)"),
    (-1, 2, "ספליט הפוך", r"reverse (stock )?split"),
    (-1, 2, "הגדלת מניות מורשות", r"increase\w*\b.{0,40}\bauthori[sz]ed"),
    (-1, 2, "גיוס / דילול",
     r"convertible (note|debenture|preferred)|regulation a\b|\breg a\b|offering circular|private placement"
     r"|registered direct|equity line|at[- ]the[- ]market offering"),
    (-1, 3, "סכנה",
     r"bankrupt\w*|chapter (7|11)\b|going dark|deregist\w*|trading suspension|(SEC|commission) suspend\w*"),
]
_KEYWORDS = [(p, s, lab, re.compile(rx, re.I)) for p, s, lab, rx in KEYWORDS]
_CAVEAT = re.compile(r"caveat emptor", re.I)
_CAVEAT_REMOVED = re.compile(CAVEAT_REMOVED, re.I)


def _form_rule(form):
    form = form.upper()
    for key in sorted(SEC_FORMS, key=len, reverse=True):
        if form.startswith(key):
            return SEC_FORMS[key]
    return None


def classify(it):
    """Returns {"pol", "ps" (strongest positive), "strength", "label", "labels", "candidate"}."""
    signals = []
    market_ok = False
    if it["src"] == "SEC":
        r = _form_rule(it.get("form", ""))
        if r:
            signals.append(r)
        signals += [SEC_ITEMS[i] for i in it.get("items", []) if i in SEC_ITEMS]
    elif it["src"] == "FINRA" and it.get("dsuffix") == "added":
        signals.append((-1, 2, "ספליט הפוך (נוספה סיומת D לטיקר)"))
    elif it["src"] == "FINRA" and it.get("dsuffix") == "removed":
        signals.append((0, 1, "הוסרה סיומת D (כ-20 יום אחרי ספליט הפוך)"))
    elif it["src"] == "FINRA":
        for key, (p, s, lab, market) in FINRA_REASONS:
            if key.lower() in it.get("reason", "").lower():
                signals.append((p, s, lab))
                market_ok = market
                break
    if it["src"] != "FINRA":
        text = f"{it.get('title', '')} {it.get('text', '')}"
        for p, s, lab, rx in _KEYWORDS:
            if rx.search(text):
                signals.append((p, s, lab))
        if _CAVEAT.search(text) and not _CAVEAT_REMOVED.search(text):
            signals.append((-1, 2, "Caveat Emptor (גולגולת)"))

    pos = max([s for p, s, _ in signals if p > 0], default=0)
    neg = max([s for p, s, _ in signals if p < 0], default=0)
    neu = max([s for p, s, _ in signals if p == 0], default=0)
    pol = 1 if pos > neg else (-1 if neg > pos else 0)
    labels = []
    for _, _, lab in sorted(signals, key=lambda x: (-x[1], -x[0])):
        if lab not in labels:
            labels.append(lab)
    if it["src"] == "FINRA":
        candidate = market_ok
    else:
        candidate = pos >= 2 or (neu >= 2 and neg < 2)
    return {
        "pol": pol, "ps": pos, "strength": max(pos, neg, neu),
        "label": labels[0] if labels else "", "labels": labels, "candidate": candidate,
    }


def fallback_score(rule):
    """Score when the AI is unavailable."""
    if rule["pol"] > 0:
        return {3: 8, 2: 6}.get(rule["ps"], 4)
    return 2 if rule["pol"] < 0 else 5
