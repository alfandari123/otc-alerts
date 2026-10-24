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
TRADE_FEE = 6.95              # Schwab online commission per OTC (non-NMS) order, per its April 2026 pricing guide
TRADE_POSITION_PCT = 0.05     # default size = 5% of the portfolio ...
TRADE_SIZE_BY_SCORE = {7: 0.05, 8: 0.07, 9: 0.10, 10: 0.10}   # ... stronger news = bigger
TRADE_TRIPS_FACTOR = 0.6      # trips ($0.000X): one tick is 10-100%, so smaller
TRADE_MAX_POSITION_PCT = 0.10
TRADE_MAX_POSITIONS = 5
TRADE_MIN_SCORE = 7           # AI score of the news needed for a buy
TRADE_MIN_DOLLARS = 150       # smaller buys are eaten by the fees
TRADE_RR = 3.0                # gain must be 3x the loss, after the spread and the fees
TRADE_MAX_RISK = 0.35         # stop no more than 35% below the buy price
TRADE_MAX_TARGET = 2.0        # skip when 1:3 needs more than +200%
TRADE_MAX_SPREAD = 0.25      # skip when the real ask is 25%+ above the bid (only known with Schwab quotes)
TRADE_L2_SECONDS = 3.0       # Level 2: listen to Schwab's stream this long per stock
TRADE_L2_LEVELS = 3           # buyers counted in the first 3 bid levels ...
TRADE_L2_BID_SHARE = 0.5      # ... we buy at most half of them (so they can take our shares back)
TRADE_L2_WALL_X = 5           # seller wall: an ask 5x the usual size ...
TRADE_L2_WALL_VS_US = 3       # ... and 3x our own position, before the target -> no buy
TRADE_L2_HOLE = 0.25          # a gap of 25%+ between two buyer levels = a hole (BDPT: 0.0040 -> 0.0023)
TRADE_L2_RISK_X = 1.3         # the stop would really fill 30%+ worse than planned -> buy less / skip
TRADE_L2_NEAR = 0.15          # buyers within 15% under the stop count as "near the stop"
TRADE_MAX_CHASE = 0.60        # do not buy after the stock already rose 60%+ from the pre-news price
TRADE_DUMP_FROM_HIGH = 0.30   # do not buy when the price already fell 30% from the day's high
TRADE_CONFIRM_BARS = 5        # price must be higher than 5 trading minutes ago
TRADE_VOL_CONFIRM_X = 1.0     # today's volume must already reach the 20-day average
TRADE_MAX_VOL_SHARE = 0.02    # our buy <= 2% of today's volume (so we can get out)
TRADE_EXIT_LIQ_SHARE = 0.10   # at the end of the day hold at most 10% of the day's volume
TRADE_WINDOW_MIN = 120        # look for an entry up to 2 hours after the news (or after the open)
TRADE_ENTRY_FROM = 9 * 60 + 35     # New York time, minutes after midnight
TRADE_ENTRY_UNTIL = 15 * 60 + 30
TRADE_EOD_MIN = 15 * 60 + 50       # end-of-day checks
TRADE_OPEN_CALM_MIN = 15          # first 15 minutes: dips under the stop must CLOSE under it; review at 9:45
TRADE_TRAIL_AFTER_R = 2.0     # after +2R the stop trails 1R below the high
TRADE_TAKE_PART = 2 / 3       # at the target sell 2/3, keep 1/3 as a bonus (only when no dilution danger)
TRADE_RUNNER_LOCK_R = 2.0     # the bonus part's stop is at least +2R (profit locked)
TRADE_RUNNER_TRAIL_R = 1.5    # ... and trails 1.5R below the high
TRADE_STALE_DAYS = 5          # a stock standing still this long may be swapped for much stronger news
TRADE_ROTATE_SCORE = 9
TRADE_MAX_DAYS = 30           # calendar days; standing still is normal, but not forever
TRADE_DIL_FLAG_DAYS = 14      # an offering / convertible note filed in the last 14 days blocks buying
TRADE_AUTH_USED_MAX = 0.95    # 95%+ of authorized shares issued = dilution coming
TRADE_TSO_CHECK_MIN = 60      # re-check share count of held stocks every hour
TRADE_DUMP_BARS = 10          # heavy selling: last 10 minutes ...
TRADE_DUMP_VOL_X = 3.0        # ... with 3x the normal volume ...
TRADE_DUMP_DROP = 0.15        # ... and a drop of 15%+  -> investigate why
TRADE_WALL_BARS = 30          # seller wall: in the last 30 bars ...
TRADE_WALL_TOUCHES = 8        # ... the same high touched 8+ times ...
TRADE_WALL_VOL_X = 2.0        # ... on 2x the normal volume, without a break
TRADE_EQ_EVERY_MIN = 15       # portfolio value history for the trading screen

# --- "ATM stocks" strategy (bot/atm_trader.py, virtual money; off in live mode for now) ---
ATM_ON = True
ATM_BUDGET_PCT = 0.40         # at most 40% of the portfolio in ATM stocks (20% left no room for bigger good-ATM buys)
ATM_MAX_POS = 5
ATM_MAX_TRADE = 500.0
ATM_MIN_TRADE = 25.0
ATM_MIN_GAIN = 20.0           # skip a round whose profit at the ceiling after both fees is under $20
# A "good ATM" (his request 24.10): the pattern repeated many times and no dilution signs -> a bigger buy.
# Backtest 24.10 (177 sub-penny tickers, 1y): 4+ swings & 5+ touches won 142/152 rounds vs ~half for the rest;
# the size limit that matters is the share of daily volume (2% kept buys ~$120), so good ATMs get 5% of it.
ATM_GOOD_SWINGS = 4
ATM_GOOD_TOUCHES = 5          # floor AND ceiling touches
ATM_GOOD_MAX_CHG6 = 0.05      # with share-count data: under 5% more shares in 6 months
ATM_GOOD_VOL_SHARE = 0.05     # vs TRADE_MAX_VOL_SHARE (2%) for the rest
ATM_GOOD_MAX_TRADE = 1500.0
ATM_QUEUE_X = 3               # sub-penny queue: filled after 3x our size traded at our price (or it went through)
ATM_NEWS_TRAIL = 0.25         # fresh news at the ceiling: no sale there, trailing stop 25% under the high
ATM_ADD_ON = True             # one seller dumped under the floor (no dilution): buy once more, cheaper
ATM_HEAVY_X = 2               # a day closing under the floor on 2x volume counts as a break day; 2 in a row = out
ATM_MAX_DAYS = 90
ATM_COOLDOWN_DAYS = 30        # after a dilution / broken-floor exit, leave the stock alone
ATM_SCAN_EVERY_H = 20         # the scan runs outside market hours, about once a day
ATM_SCAN_PAGES = 20           # 20 x 50 most active OTC stocks

# --- Radar for held stocks: Google News + SEC filings by company number (bot/radar.py) ---
RADAR_EVERY_MIN = 10

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

# --- Charles Schwab (real trading, later). Login helper worker: schwab-worker/ ---
SCHWAB_WORKER = "https://otc-schwab.range-guard.workers.dev"
SCHWAB_BOT_KEY = os.environ.get("SCHWAB_BOT_KEY", "").strip()
SCHWAB_LOGIN_KEY = os.environ.get("SCHWAB_LOGIN_KEY", "").strip()
SCHWAB_REMIND_DAYS_LEFT = 1.0      # remind to log in when the 7-day login has 1 day left (once a day)
SCHWAB_CHECK_EVERY_MIN = 30
# "paper" = virtual money only. "live" = real orders at Schwab (Reuven switches it on, after the paper test).
TRADE_MODE = "paper"
TRADE_LIVE_MAX_CAPITAL = 5000.0    # the bot never uses more than this of the real account (he trades there too)
TRADE_LIVE_ORDER_MIN = 3           # an unfilled buy is cancelled after 3 minutes
TRADE_LIVE_SELL_RETRY_MIN = 2      # an unfilled sell is re-priced at the new bid every 2 minutes

DASH_PAGE = "https://alfandari123.github.io/otc-alerts/"   # the trading screen (GitHub Pages, docs/)

SEC_UA = f"OTC-Alerts-Bot {SEC_CONTACT}"
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
