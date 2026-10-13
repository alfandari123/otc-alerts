"""Settings for the OTC alerts bot. Tune the numbers here."""
import os

# --- Market-wide scan: price band (USD) ---
MIN_PRICE = 0.0001
MAX_PRICE = 0.01

# --- AI score (1-10) needed for a market-wide alert ---
SCORE_IN_RANGE = 7        # price inside the band
SCORE_UNKNOWN_PRICE = 8   # price could not be fetched
SCORE_ABOVE_RANGE = 9     # price above the band -> only very strong news
EVENT_ABOVE_RANGE_MAX_PRICE = 0.10   # strong events (e.g. Caveat Emptor removed) above the band, up to this price

# --- AI (Google Gemini, free tier). First model that works is used. ---
GEMINI_MODELS = [            # each model has its own free daily quota
    "gemini-flash-lite-latest",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-flash-latest",
]
AI_DAILY_LIMIT = 400      # stay under the free daily quota
AI_PER_RUN_LIMIT = 25     # keep each run short; the rest waits for the next run
RUN_DEADLINE_SEC = 300    # stop starting new AI work after this many seconds
POLL_EVERY_SEC = 60       # in US news hours the bot checks every minute (see run.yml)

# --- Volume spikes (watchlist only) ---
VOLUME_SPIKE_X = 5            # today's volume vs. the 20-day average
VOLUME_MIN_DOLLARS = 1000     # ignore spikes smaller than this (price x volume)
VOLUME_CHECK_EVERY_MIN = 15

# --- Volume spikes, whole market (most active stocks in the price band) ---
VOLUME_SPIKE_X_MARKET = 10
VOLUME_MIN_DOLLARS_MARKET = 5000
MARKET_VOLUME_MAX_ALERTS = 5    # per check

# --- Dilution (share counts from OTC Markets' transfer-agent feed) ---
DILUTION_WARN_3M = 0.10       # warn when shares grew 10%+ in 3 months
DILUTION_WARN_6M = 0.25       # ... or 25%+ in 6 months
AUTH_USED_WARN = 0.85         # warn when 85%+ of the authorized shares are already issued
DILUTION_ALERT_MIN = 0.005    # watchlist alert when outstanding/authorized shares change by 0.5%+
DILUTION_WATCH_EVERY_MIN = 180

# --- Paper trading agent (virtual money, bot/trader.py) ---
TRADE_ON = True
TRADE_START_CASH = 5000.0
TRADE_POSITION_PCT = 0.05     # each buy = 5% of the portfolio
TRADE_MAX_POSITIONS = 5
TRADE_MIN_SCORE = 7           # AI score of the news needed for a buy
TRADE_MIN_DOLLARS = 50        # smaller buys are not worth it
TRADE_RR = 3.0                # possible gain must be 3x the possible loss (after the spread)
TRADE_MAX_RISK = 0.35         # stop no more than 35% below the buy price
TRADE_MAX_TARGET = 2.0        # skip when 1:3 needs more than +200%
TRADE_MAX_CHASE = 0.60        # do not buy after the stock already rose 60%+ from the pre-news price
TRADE_DUMP_FROM_HIGH = 0.30   # do not buy when the price already fell 30% from the day's high
TRADE_CONFIRM_BARS = 5        # price must be higher than 5 minutes ago
TRADE_VOL_CONFIRM_X = 1.0     # today's volume must already reach the 20-day average
TRADE_MAX_VOL_SHARE = 0.02    # our buy <= 2% of today's volume (so we can get out)
TRADE_WINDOW_MIN = 120        # look for an entry up to 2 hours after the news (or after the open)
TRADE_ENTRY_FROM = 9 * 60 + 35     # New York time, minutes after midnight
TRADE_ENTRY_UNTIL = 15 * 60 + 30
TRADE_EOD_MIN = 15 * 60 + 50       # end-of-day check
TRADE_OVERNIGHT_MIN_R = 0.5   # keep overnight only when the gain is at least half the risk
TRADE_TRAIL_AFTER_R = 2.0     # after +2R the stop trails 1R below the high
TRADE_MAX_DAYS = 5
TRADE_DIL_FLAG_DAYS = 14      # an offering / convertible note filed in the last 14 days blocks buying
TRADE_AUTH_USED_MAX = 0.95    # 95%+ of authorized shares issued = dilution coming
TRADE_TSO_CHECK_MIN = 60      # re-check share count of held stocks every hour
TRADE_DUMP_BARS = 10          # heavy selling: last 10 minutes ...
TRADE_DUMP_VOL_X = 3.0        # ... with 3x the normal volume ...
TRADE_DUMP_DROP = 0.15        # ... and a drop of 15%+

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
