"""Settings for the OTC alerts bot. Tune the numbers here."""
import os

# --- Market-wide scan: price band (USD) ---
MIN_PRICE = 0.0001
MAX_PRICE = 0.01

# --- AI score (1-10) needed for a market-wide alert ---
SCORE_IN_RANGE = 7        # price inside the band
SCORE_UNKNOWN_PRICE = 8   # price could not be fetched
SCORE_ABOVE_RANGE = 9     # price above the band -> only very strong news

# --- AI (Google Gemini, free tier). First model that works is used. ---
GEMINI_MODELS = [
    "gemini-flash-lite-latest",
    "gemini-2.5-flash-lite",
    "gemini-flash-latest",
    "gemini-2.5-flash",
]
AI_DAILY_LIMIT = 400      # stay under the free daily quota
AI_PER_RUN_LIMIT = 25     # keep each run short; the rest waits for the next run
RUN_DEADLINE_SEC = 300    # stop starting new AI work after this many seconds

# --- Volume spikes (watchlist only) ---
VOLUME_SPIKE_X = 5            # today's volume vs. the 20-day average
VOLUME_MIN_DOLLARS = 1000     # ignore spikes smaller than this (price x volume)
VOLUME_CHECK_EVERY_MIN = 15

# --- Housekeeping ---
WATCHLIST_MAX = 150
SEEN_TTL_DAYS = 10
SOURCE_DOWN_WARN_HOURS = 3

# --- Secrets (GitHub Actions secrets; never commit them) ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
STATE_KEY = os.environ.get("STATE_KEY", "").strip()
SEC_CONTACT = os.environ.get("SEC_CONTACT", "").strip()  # SEC requires a contact e-mail

IN_CLOUD = os.environ.get("GITHUB_ACTIONS") == "true"

SEC_UA = f"OTC-Alerts-Bot {SEC_CONTACT}"
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
