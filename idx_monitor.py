"""
idx_monitor.py
==============
Two independent monitors running in the same loop:

  1. ANNOUNCEMENT MONITOR  - watches IDX Keterbukaan Informasi for new posts
                             matching configurable keyword/emiten rules.

  2. SUSPENSION TRACKER    - watches IDX suspension/unsuspension feed, detects
                             stocks in their 2nd suspension cycle (<=20 trading
                             days between last unsuspension and new suspension),
                             verifies >=-8% daily drop on trading days 1-3, then
                             notifies on trading days 6 and 7.

Usage:
    python idx_monitor.py

Requirements:
    pip install requests python-dotenv curl_cffi pandas_market_calendars yfinance

Environment variables (in .env):
    TELEGRAM_BOT_TOKEN   - Telegram bot token
    TELEGRAM_CHAT_ID     - Telegram chat/user ID
"""

import json
import logging
import os
import time
from datetime import datetime, date, timedelta
from itertools import product
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import yfinance as yf
import pandas_market_calendars as mcal
from curl_cffi.requests import Session as CurlSession
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# LOGGING
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# CONFIGURATION — edit this section to customise behaviour
# ---------------------------------------------------------------------------

# ── Feature toggles (set False to disable for debugging without stopping the other) ──
ENABLE_ANNOUNCEMENT_MONITOR = True
ENABLE_SUSPENSION_TRACKER   = True

# ── Polling intervals ────────────────────────────────────────────────────────
MARKET_HOURS_INTERVAL_SEC = 5 * 60    # 5 minutes during market hours
OFF_HOURS_INTERVAL_SEC    = 60 * 60   # 1 hour outside market hours
MARKET_OPEN_TIME          = (8, 30)   # 08:30 WIB
MARKET_CLOSE_TIME         = (16, 30)  # 16:30 WIB
WIB                       = ZoneInfo("Asia/Jakarta")

# ── State storage ─────────────────────────────────────────────────────────────
STATE_DIR                 = Path("state")
ANNOUNCEMENT_STATE_FILE   = STATE_DIR / "seen_posts.json"
SUSPENSION_STATE_FILE     = STATE_DIR / "suspension_tracking.json"
MAX_SEEN_IDS              = None   # None = keep all IDs. Set int to cap.

# ── IDX Announcement API ──────────────────────────────────────────────────────
IDX_ANNOUNCEMENT_URL  = "https://www.idx.co.id/primary/ListedCompany/GetAnnouncement"
IDX_SUSPENSION_URL    = "https://www.idx.co.id/primary/NewsAnnouncement/GetSuspension"
IDX_PAGE_SIZE         = 50
IDX_SUSP_PAGE_SIZE    = 100
IDX_EMITEN_TYPE       = "*"
IDX_LANG              = "id"
IDX_DATE_FROM         = "19010101"
# Backfill window: how many calendar days back to fetch on first suspension run
SUSPENSION_BACKFILL_DAYS = 60

REQUEST_TIMEOUT = 15
REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.idx.co.id/",
    "Accept": "application/json, text/plain, */*",
}

# ── Suspension tracker settings ───────────────────────────────────────────────
SUSP_MAX_GAP_TRADING_DAYS = 20    # unsuspension→suspension gap to qualify as 2nd cycle
SUSP_PRICE_DROP_THRESHOLD = -8.0  # % — days 1-3 must each drop more than this
SUSP_NOTIFY_DAYS          = {6, 7} # which trading days trigger notification

# ── Telegram ──────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")
TELEGRAM_API_URL   = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

# ── Announcement watch rules ──────────────────────────────────────────────────
#
# Each rule is a dict with two keys:
#   "keywords"  list[str]  — Empty list = no keyword filter (matches all posts).
#   "emiten"    list[str]  — Empty list = all emiten.
# Multiple keywords/emiten generate a cartesian product of API calls.
# A post matched by multiple rules is notified only once.
#
WATCH_RULES = [
    # General monitoring
    {"keywords": ["pengambilalihan"], "emiten": []},

    # General monitoring
    # {"keywords": ["Pemegang Saham di atas 1%"], "emiten": []},

    # RI Monitoring
    #{"keywords": ["HMETD", "PMHETD"], "emiten": ["BUVA", "UANG"]},

    # Investing stock monitoring
    {"keywords": [], "emiten": ["IRSX"]},
    {"keywords": [], "emiten": ["PYFA"]},
    {"keywords": [], "emiten": ["BUVA"]},
]

# ---------------------------------------------------------------------------
# IDX TRADING CALENDAR
# ---------------------------------------------------------------------------

_IDX_CALENDAR = mcal.get_calendar("XIDX")


def get_trading_days(start: date, end: date) -> list[date]:
    """Return list of IDX trading days between start and end (inclusive)."""
    schedule = _IDX_CALENDAR.schedule(
        start_date=start.isoformat(),
        end_date=end.isoformat(),
    )
    return [d.date() for d in schedule.index]


def is_trading_day(d: date) -> bool:
    days = get_trading_days(d, d)
    return len(days) > 0


def next_trading_day(d: date) -> date:
    """Return the first trading day strictly after d."""
    candidate = d + timedelta(days=1)
    for _ in range(14):   # safety limit — no holiday streak > 14 days
        if is_trading_day(candidate):
            return candidate
        candidate += timedelta(days=1)
    raise RuntimeError(f"Could not find next trading day after {d}")


def nth_trading_day(start: date, n: int) -> date:
    """Return the nth trading day on or after start (1-indexed)."""
    days = get_trading_days(start, start + timedelta(days=n * 3 + 30))
    if len(days) < n:
        raise RuntimeError(f"Not enough trading days found from {start}")
    return days[n - 1]


def count_trading_days_between(d1: date, d2: date) -> int:
    """Count trading days from d1 (exclusive) to d2 (inclusive)."""
    if d2 <= d1:
        return 0
    return len(get_trading_days(d1 + timedelta(days=1), d2))


def determine_day1(announcement_dt: datetime) -> date:
    """
    Determine Day 1 of counting based on announcement datetime.
    If announcement is before MARKET_OPEN_TIME → Day 1 = announcement date (if trading day).
    If announcement is at or after MARKET_OPEN_TIME → Day 1 = next trading day.
    """
    ann_date = announcement_dt.date()
    open_h, open_m = MARKET_OPEN_TIME
    open_minutes = open_h * 60 + open_m
    ann_minutes  = announcement_dt.hour * 60 + announcement_dt.minute

    if ann_minutes < open_minutes:
        # Announced before market open — same day is Day 1 if it's a trading day
        if is_trading_day(ann_date):
            return ann_date
        return next_trading_day(ann_date)
    else:
        # Announced at or after market open — Day 1 is next trading day
        return next_trading_day(ann_date)


# ---------------------------------------------------------------------------
# TELEGRAM
# ---------------------------------------------------------------------------

def send_telegram(message: str) -> bool:
    """Send a Telegram message. Returns True on success."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.error("Telegram credentials missing. Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env")
        return False
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    for attempt in range(2):
        try:
            if attempt > 0:
                time.sleep(3)
            resp = requests.post(
                f"{TELEGRAM_API_URL}/sendMessage",
                json=payload,
                timeout=10,
            )
            resp.raise_for_status()
            return True
        except Exception as e:
            log.warning(f"Telegram send {'retry ' if attempt else ''}failed: {e}")
    log.error("Telegram: both attempts failed")
    return False


# ===========================================================================
# SECTION 1 — ANNOUNCEMENT MONITOR
# ===========================================================================

def _fetch_announcements(keyword: str = "", kode_emiten: str = "") -> list[dict]:
    """Fetch announcements from IDX API for a single keyword/emiten combo."""
    params = {
        "kodeEmiten": kode_emiten,
        "emitenType": IDX_EMITEN_TYPE,
        "indexFrom": 0,
        "pageSize": IDX_PAGE_SIZE,
        "dateFrom": IDX_DATE_FROM,
        "dateTo": date.today().strftime("%Y%m%d"),
        "lang": IDX_LANG,
        "keyword": keyword,
    }
    try:
        with CurlSession(impersonate="chrome124") as session:
            resp = session.get(
                IDX_ANNOUNCEMENT_URL,
                params=params,
                headers=REQUEST_HEADERS,
                timeout=REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("Replies", []) or []
    except Exception as e:
        log.warning(f"Error fetching announcements (keyword='{keyword}', emiten='{kode_emiten}'): {e}")
    return []


def _extract_post_id(post: dict) -> str:
    p = post.get("pengumuman", {})
    return p.get("Id2", "") or p.get("NoPengumuman", "") or str(hash(str(p)))


def _get_attachments(post: dict) -> tuple[str, list[dict]]:
    """Returns (main_url, additional_attachments)."""
    attachments = post.get("attachments", [])
    main_url = "https://www.idx.co.id/id/perusahaan-tercatat/keterbukaan-informasi/"
    additional = []
    for att in attachments:
        if not att.get("IsAttachment", True):
            main_url = att.get("FullSavePath", main_url)
        else:
            additional.append(att)
    return main_url, additional


def _format_announcement_notification(post: dict, matched_label: str) -> str:
    p = post.get("pengumuman", {})
    title       = p.get("JudulPengumuman", "(no title)")
    emiten_code = p.get("Kode_Emiten", "").strip()
    raw_date    = p.get("TglPengumuman", "")
    main_url, additional = _get_attachments(post)

    try:
        dt = datetime.fromisoformat(raw_date)
        post_time = dt.strftime("%d %b %Y %H:%M WIB")
    except Exception:
        post_time = raw_date

    if additional:
        att_lines = []
        for a in additional:
            url  = a.get("FullSavePath", "")
            name = a.get("OriginalFilename", a.get("PDFFilename", "attachment"))
            att_lines.append(f"  • <a href='{url}'>{name}</a>")
        att_section = "\n".join(att_lines)
    else:
        att_section = "  None"

    return (
        f"🔔 <b>IDX Alert — New Announcement</b>\n"
        f"\n"
        # f"📋 <b>Matched:</b> {matched_label}\n"
        f"🕐 {post_time}\n"
        f"🏢 <b>{emiten_code}</b>\n"
        f"📄 <a href='{main_url}'>{title}</a>\n"
        f"\n"
        f"📎 <b>Attachments:</b>\n"
        f"{att_section}"
    )


def _expand_rules(rules: list[dict]) -> list[tuple[str, str, str]]:
    """Expand WATCH_RULES preserving order, deduplicating."""
    seen = set()
    result = []
    for rule in rules:
        keywords    = rule.get("keywords") or [""]
        emiten_list = rule.get("emiten") or [""]
        for kw, em in product(keywords, emiten_list):
            if (kw, em) in seen:
                continue
            seen.add((kw, em))
            if kw and em:
                label = f"keyword='{kw}' × emiten={em}"
            elif kw:
                label = f"keyword='{kw}' (all emiten)"
            elif em:
                label = f"emiten={em} (all posts)"
            else:
                label = "all posts (no filter)"
            result.append((kw, em, label))
    return result


def _load_announcement_state() -> set[str]:
    if not ANNOUNCEMENT_STATE_FILE.exists():
        return set()
    try:
        data = json.loads(ANNOUNCEMENT_STATE_FILE.read_text(encoding="utf-8"))
        return set(data.get("seen_ids", []))
    except Exception as e:
        log.warning(f"Could not read announcement state: {e}")
        return set()


def _save_announcement_state(seen_ids: set[str]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    ids_list = list(seen_ids)
    if MAX_SEEN_IDS is not None and len(ids_list) > MAX_SEEN_IDS:
        ids_list = ids_list[-MAX_SEEN_IDS:]
    ANNOUNCEMENT_STATE_FILE.write_text(
        json.dumps({
            "seen_ids": ids_list,
            "last_updated": datetime.now(WIB).isoformat(),
            "count": len(ids_list),
        }, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def run_announcement_monitor(seen_ids: set[str], api_calls: list[tuple],
                              is_first_run: bool) -> int:
    """
    One poll cycle for the announcement monitor.
    Returns number of new posts notified (0 on first run).
    """
    newly_found: dict[str, dict]       = {}
    matched_labels: dict[str, list[str]] = {}

    for keyword, kode_emiten, label in api_calls:
        posts = _fetch_announcements(keyword=keyword, kode_emiten=kode_emiten)
        for post in posts:
            pid = _extract_post_id(post)
            if pid not in seen_ids:
                newly_found[pid] = post
                matched_labels.setdefault(pid, []).append(label)
        time.sleep(2)

    if is_first_run:
        for pid in newly_found:
            seen_ids.add(pid)
        log.info(f"[Announcement] First run seeded {len(seen_ids)} post IDs.")
        return 0

    if not newly_found:
        return 0

    for pid, post in newly_found.items():
        label_str = " | ".join(matched_labels.get(pid, []))
        msg = _format_announcement_notification(post, label_str)
        if send_telegram(msg):
            p = post.get("pengumuman", {})
            log.info(f"[Announcement] Notified: [{p.get('Kode_Emiten','').strip()}] "
                     f"{p.get('JudulPengumuman','')[:60]}")
        seen_ids.add(pid)
        time.sleep(0.3)

    return len(newly_found)


# ===========================================================================
# SECTION 2 — SUSPENSION TRACKER
# ===========================================================================

# ── Suspension state schema ──────────────────────────────────────────────────
#
# suspension_state = {
#   "tracked": {
#     "MSIE": {
#       "kode": "MSIE",
#       "judul": "...",
#       "unsuspension_date": "2026-05-05",   # date of the qualifying unsuspension
#       "day1_date": "2026-05-06",            # first trading day to count
#       "day_results": {
#         "1": {"date": "2026-05-06", "change_pct": -9.5, "passed": true},
#         "2": {"date": "2026-05-07", "change_pct": null, "passed": null},
#         "3": {"date": "2026-05-08", "change_pct": null, "passed": null}
#       },
#       "filter_passed": false,   # True once all 3 days pass
#       "notified_days": [],      # list of day numbers already notified
#       "dropped": false,
#       "drop_reason": null,
#       "pdf_url": "https://..."
#     }
#   },
#   # Full unsuspension history per ticker for gap calculation
#   "unsuspension_history": {
#     "MSIE": ["2026-03-01", "2026-05-05"]
#   },
#   # Seen suspension event IDs to avoid reprocessing
#   "seen_suspension_ids": [],
#   "last_updated": "..."
# }

def _load_suspension_state() -> dict:
    if not SUSPENSION_STATE_FILE.exists():
        return {
            "tracked": {},
            "unsuspension_history": {},
            "seen_suspension_ids": [],
            "last_updated": None,
        }
    try:
        return json.loads(SUSPENSION_STATE_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning(f"Could not read suspension state: {e}")
        return {
            "tracked": {},
            "unsuspension_history": {},
            "seen_suspension_ids": [],
            "last_updated": None,
        }


def _save_suspension_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    state["last_updated"] = datetime.now(WIB).isoformat()
    SUSPENSION_STATE_FILE.write_text(
        json.dumps(state, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )


def _suspension_event_id(entry: dict) -> str:
    """Stable unique ID for a suspension/unsuspension event."""
    return f"{entry.get('Kode', '')}_{entry.get('Date', '')}_{entry.get('Info_Type', '')}"


def _build_susp_pdf_url(entry: dict) -> str:
    path = entry.get("Data_Download", "")
    if not path:
        return "https://www.idx.co.id/id/berita/suspensi"
    if path.startswith("http"):
        return path
    return f"https://www.idx.co.id{path}"


def _fetch_suspension_events(date_from: str = "", date_to: str = "") -> list[dict]:
    """
    Fetch suspension and unsuspension events from IDX API.
    Returns combined list with Info_Type = 'SPT' or 'UPT'.
    """
    params = {
        "indexFrom": 1,
        "dateFrom": date_from,
        "dateTo": date_to,
        "pageSize": IDX_SUSP_PAGE_SIZE,
        "lang": IDX_LANG,
    }
    try:
        with CurlSession(impersonate="chrome124") as session:
            resp = session.get(
                IDX_SUSPENSION_URL,
                params=params,
                headers=REQUEST_HEADERS,
                timeout=REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("Results", []) or []
    except Exception as e:
        log.warning(f"Error fetching suspension events: {e}")
    return []


def _get_daily_change_pct(kode: str, target_date: date) -> float | None:
    """
    Fetch the daily % change for a stock on target_date.
    Returns None if data is not yet available (e.g. market not closed yet).
    Uses yfinance with .JK suffix.
    """
    ticker_symbol = kode.strip() + ".JK"
    try:
        stock = yf.Ticker(ticker_symbol)
        # Fetch enough history to get target_date close and previous close
        start = (target_date - timedelta(days=10)).isoformat()
        end   = (target_date + timedelta(days=1)).isoformat()
        hist  = stock.history(start=start, end=end)

        if hist.empty:
            return None

        # Normalize index to date
        hist.index = hist.index.date

        if target_date not in hist.index:
            return None

        idx_pos = list(hist.index).index(target_date)
        if idx_pos == 0:
            return None  # No previous close available

        target_close   = hist["Close"].iloc[idx_pos]
        previous_close = hist["Close"].iloc[idx_pos - 1]

        if previous_close == 0:
            return None

        return round(((target_close - previous_close) / previous_close) * 100, 2)
    except Exception as e:
        log.warning(f"yfinance error for {ticker_symbol} on {target_date}: {e}")
        return None


def _is_after_market_close() -> bool:
    """True if current WIB time is after MARKET_CLOSE_TIME — safe to read day's close price."""
    now = datetime.now(WIB)
    close_h, close_m = MARKET_CLOSE_TIME
    close_minutes = close_h * 60 + close_m
    now_minutes   = now.hour * 60 + now.minute
    return now_minutes > close_minutes


def _format_multi_unsuspension_alert(entry: dict) -> str:
    """Format notification for '>1 Kode' entries — manual check required."""
    raw_date = entry.get("Date", "")
    try:
        dt = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
        event_time = dt.strftime("%d %b %Y %H:%M WIB")
    except Exception:
        event_time = raw_date
    pdf_url = _build_susp_pdf_url(entry)
    return (
        f"⚠️ <b>IDX — Multiple Stocks Unsuspended</b>\n"
        f"\n"
        f"More than 1 stock was unsuspended in this announcement.\n"
        f"Please check the PDF to identify the individual stocks.\n"
        f"\n"
        f"🕐 {event_time}\n"
        f"🔗 <a href='{pdf_url}'>Open PDF (Manual Check Required)</a>"
    )


def _format_fca_day_notification(day_num: int, qualifying_stocks: list[dict]) -> str:
    """Format the grouped notification for day 6 or 7 alert."""
    lines = [
        f"📊 <b>IDX Suspension Tracker — Day {day_num} Alert</b>\n",
        f"Stocks on trading day <b>{day_num}</b> post-unsuspension, "
        f"with ≥{abs(SUSP_PRICE_DROP_THRESHOLD)}% drop on each of days 1-3:\n",
    ]
    for stock in qualifying_stocks:
        kode   = stock["kode"]
        judul  = stock.get("judul", "")
        undate = stock.get("unsuspension_date", "")
        dr     = stock.get("day_results", {})
        d1_pct = dr.get("1", {}).get("change_pct", "N/A")
        d2_pct = dr.get("2", {}).get("change_pct", "N/A")
        d3_pct = dr.get("3", {}).get("change_pct", "N/A")
        cycle  = stock.get("cycle_number", "?")
        pdf    = stock.get("pdf_url", "")

        def fmt_pct(v):
            return f"{v:+.2f}%" if isinstance(v, (int, float)) else str(v)

        lines.append(
            f"\n🔴 <b>{kode}</b> — Suspension cycle #{cycle}\n"
            f"   📅 Unsuspended: {undate}\n"
            f"   📈 Day 1: {fmt_pct(d1_pct)} | "
            f"Day 2: {fmt_pct(d2_pct)} | "
            f"Day 3: {fmt_pct(d3_pct)}\n"
            f"   📌 Today is trading day {day_num}\n"
            f"   🔗 <a href='{pdf}'>Unsuspension PDF</a>"
        )
    return "\n".join(lines)


def _backfill_suspension_history(state: dict) -> None:
    """
    On first run: fetch recent suspension history to populate
    unsuspension_history so gap calculations are accurate.
    Only fetches last SUSPENSION_BACKFILL_DAYS calendar days.
    """
    date_from = (date.today() - timedelta(days=SUSPENSION_BACKFILL_DAYS)).strftime("%Y%m%d")
    date_to   = date.today().strftime("%Y%m%d")
    log.info(f"[Suspension] Backfilling history from {date_from} to {date_to}...")

    events = _fetch_suspension_events(date_from=date_from, date_to=date_to)
    seen_ids = set(state.get("seen_suspension_ids", []))

    for entry in events:
        eid = _suspension_event_id(entry)
        seen_ids.add(eid)
        kode      = entry.get("Kode", "").strip()
        info_type = entry.get("Info_Type", "").upper()

        if kode == ">1 KODE" or not kode:
            continue

        raw_date = entry.get("Date", "")
        try:
            event_date = datetime.fromisoformat(raw_date.replace("Z", "+00:00")).date()
        except Exception:
            continue

        if info_type == "UPT":
            hist = state["unsuspension_history"].setdefault(kode, [])
            date_str = event_date.isoformat()
            if date_str not in hist:
                hist.append(date_str)
                hist.sort()

    state["seen_suspension_ids"] = list(seen_ids)
    log.info(f"[Suspension] Backfill complete. "
             f"{len(state['unsuspension_history'])} tickers in history.")


def run_suspension_tracker(state: dict) -> None:
    """
    One poll cycle for the suspension tracker.
    Mutates state in-place. Caller is responsible for saving.
    """
    now_wib   = datetime.now(WIB)
    today     = now_wib.date()
    seen_ids  = set(state.get("seen_suspension_ids", []))

    # ── Step 1: Fetch latest events (last 7 days to catch anything missed) ───
    date_from = (today - timedelta(days=7)).strftime("%Y%m%d")
    date_to   = today.strftime("%Y%m%d")
    events    = _fetch_suspension_events(date_from=date_from, date_to=date_to)

    new_unsuspensions = []
    new_suspensions   = []

    for entry in events:
        eid = _suspension_event_id(entry)
        if eid in seen_ids:
            continue
        seen_ids.add(eid)

        kode      = entry.get("Kode", "").strip()
        info_type = entry.get("Info_Type", "").upper()
        raw_date  = entry.get("Date", "")

        # ">1 Kode" — send manual check notification and skip tracking
        if kode == ">1 KODE" or not kode:
            if info_type == "UPT":
                msg = _format_multi_unsuspension_alert(entry)
                send_telegram(msg)
                log.info("[Suspension] Sent '>1 Kode' manual check notification.")
            continue

        try:
            event_dt   = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
            event_date = event_dt.date()
        except Exception:
            continue

        if info_type == "UPT":
            new_unsuspensions.append((kode, event_date, event_dt, entry))
        elif info_type == "SPT":
            new_suspensions.append((kode, event_date, entry))

    state["seen_suspension_ids"] = list(seen_ids)

    # ── Step 2: Handle new suspensions — check if tracked stock got re-suspended ─
    for kode, susp_date, _ in new_suspensions:
        if kode in state["tracked"] and not state["tracked"][kode].get("dropped"):
            state["tracked"][kode]["dropped"]     = True
            state["tracked"][kode]["drop_reason"] = "re-suspended during tracking"
            log.info(f"[Suspension] {kode} re-suspended — dropped from tracking.")

    # ── Step 3: Handle new unsuspensions — qualify for tracking ─────────────
    for kode, unsp_date, unsp_dt, entry in new_unsuspensions:
        # Record in unsuspension history
        hist = state["unsuspension_history"].setdefault(kode, [])
        date_str = unsp_date.isoformat()
        if date_str not in hist:
            hist.append(date_str)
            hist.sort()

        # Skip if already actively tracked (not dropped) for this unsuspension
        existing = state["tracked"].get(kode)
        if existing and not existing.get("dropped") and \
           existing.get("unsuspension_date") == date_str:
            continue

        # ── Qualify: is this a 2nd suspension within 20 trading days? ────────
        # Find the previous unsuspension date (before current)
        prior_dates = [d for d in hist if d < date_str]
        if not prior_dates:
            log.info(f"[Suspension] {kode} — no prior unsuspension history, skip.")
            continue

        last_unsp_date = date.fromisoformat(prior_dates[-1])

        # Find the suspension date that came AFTER last_unsp_date and BEFORE unsp_date
        # We use our seen_suspension_ids to find this, but since we may not have it,
        # we approximate: count trading days from last_unsp_date to unsp_date.
        # If ≤ SUSP_MAX_GAP_TRADING_DAYS, qualify.
        gap = count_trading_days_between(last_unsp_date, unsp_date)
        log.info(f"[Suspension] {kode} gap from last unsuspension to now: "
                 f"{gap} trading days (max allowed: {SUSP_MAX_GAP_TRADING_DAYS})")

        if gap > SUSP_MAX_GAP_TRADING_DAYS:
            log.info(f"[Suspension] {kode} — gap too large, treat as 1st suspension.")
            continue

        # Count which cycle this is (how many prior unsuspensions within threshold)
        cycle_number = len(prior_dates) + 1

        # Only track exactly the 2nd cycle (cycle_number == 2).
        # If it's already been tracked before (3rd+ cycle), skip.
        if cycle_number != 2:
            log.info(f"[Suspension] {kode} — cycle #{cycle_number}, only tracking 2nd. Skip.")
            continue

        # ── Calculate Day 1 ──────────────────────────────────────────────────
        day1 = determine_day1(unsp_dt)

        # Pre-compute the dates for days 1, 2, 3, 6, 7
        try:
            trading_days_from_day1 = get_trading_days(day1, day1 + timedelta(days=30))
            day_dates = {i + 1: trading_days_from_day1[i]
                         for i in range(min(7, len(trading_days_from_day1)))}
        except Exception as e:
            log.warning(f"[Suspension] Could not compute trading days for {kode}: {e}")
            continue

        state["tracked"][kode] = {
            "kode":               kode,
            "judul":              entry.get("Judul", ""),
            "unsuspension_date":  date_str,
            "day1_date":          day1.isoformat(),
            "day_dates":          {str(k): v.isoformat() for k, v in day_dates.items()},
            "day_results":        {
                "1": {"date": day_dates.get(1, "").isoformat() if day_dates.get(1) else "",
                      "change_pct": None, "passed": None},
                "2": {"date": day_dates.get(2, "").isoformat() if day_dates.get(2) else "",
                      "change_pct": None, "passed": None},
                "3": {"date": day_dates.get(3, "").isoformat() if day_dates.get(3) else "",
                      "change_pct": None, "passed": None},
            },
            "filter_passed":      False,
            "notified_days":      [],
            "dropped":            False,
            "drop_reason":        None,
            "cycle_number":       cycle_number,
            "pdf_url":            _build_susp_pdf_url(entry),
        }
        log.info(f"[Suspension] Started tracking {kode} (cycle #{cycle_number}), "
                 f"Day 1 = {day1}")

    # ── Step 4: Update price data for tracked stocks ─────────────────────────
    price_check_available = _is_after_market_close()

    for kode, tracker in state["tracked"].items():
        if tracker.get("dropped"):
            continue

        # Check days 1, 2, 3 for missing price data
        all_filter_days_done = True
        filter_failed        = False

        for day_num in [1, 2, 3]:
            day_key = str(day_num)
            result  = tracker["day_results"].get(day_key, {})

            if result.get("passed") is not None:
                # Already have a result for this day
                if not result["passed"]:
                    filter_failed = True
                continue

            day_date_str = result.get("date", "")
            if not day_date_str:
                all_filter_days_done = False
                continue

            try:
                day_date = date.fromisoformat(day_date_str)
            except Exception:
                all_filter_days_done = False
                continue

            if day_date > today:
                # Future day — not yet
                all_filter_days_done = False
                continue

            # Day is in the past or today — try to fetch price
            # For today, only check after price_check time
            if day_date == today and not price_check_available:
                all_filter_days_done = False
                continue

            pct = _get_daily_change_pct(kode, day_date)
            if pct is None:
                # Data not yet available
                all_filter_days_done = False
                continue

            passed = pct < SUSP_PRICE_DROP_THRESHOLD
            tracker["day_results"][day_key]["change_pct"] = pct
            tracker["day_results"][day_key]["passed"]     = passed
            log.info(f"[Suspension] {kode} Day {day_num} ({day_date}): "
                     f"{pct:+.2f}% → {'✓ pass' if passed else '✗ fail'}")

            if not passed:
                filter_failed = True

        if filter_failed:
            tracker["dropped"]     = True
            tracker["drop_reason"] = "price filter failed (day 1/2/3 did not meet -8% threshold)"
            log.info(f"[Suspension] {kode} dropped — price filter failed.")
            continue

        if all_filter_days_done:
            tracker["filter_passed"] = True

    # ── Step 5: Check for day 6 / 7 notifications ────────────────────────────
    day6_stocks = []
    day7_stocks = []

    for kode, tracker in state["tracked"].items():
        if tracker.get("dropped") or not tracker.get("filter_passed"):
            continue

        notified_days = set(tracker.get("notified_days", []))
        day_dates     = tracker.get("day_dates", {})

        for notify_day in sorted(SUSP_NOTIFY_DAYS):
            if notify_day in notified_days:
                continue
            notify_date_str = day_dates.get(str(notify_day))
            if not notify_date_str:
                continue
            notify_date = date.fromisoformat(notify_date_str)
            if notify_date == today:
                if notify_day == 6:
                    day6_stocks.append(tracker)
                elif notify_day == 7:
                    day7_stocks.append(tracker)
                tracker.setdefault("notified_days", []).append(notify_day)

    if day6_stocks:
        msg = _format_fca_day_notification(6, day6_stocks)
        if send_telegram(msg):
            log.info(f"[Suspension] Sent Day 6 alert for: "
                     f"{[s['kode'] for s in day6_stocks]}")

    if day7_stocks:
        msg = _format_fca_day_notification(7, day7_stocks)
        if send_telegram(msg):
            log.info(f"[Suspension] Sent Day 7 alert for: "
                     f"{[s['kode'] for s in day7_stocks]}")
            
    # ── Step 6: Clean up completed or expired trackers ───────────────────────
    to_remove = []
    for kode, tracker in state["tracked"].items():
        day7_date_str = tracker.get("day_dates", {}).get("7")
        if not day7_date_str:
            continue
        day7_date = date.fromisoformat(day7_date_str)

        # Remove if day 7 has been notified
        if 7 in tracker.get("notified_days", []):
            to_remove.append(kode)

        # Remove if dropped and day 7 is already past (no point keeping it)
        elif tracker.get("dropped") and today > day7_date:
            to_remove.append(kode)

        # Safety net: remove anything where day 7 is more than 2 trading days past
        elif today > day7_date and count_trading_days_between(day7_date, today) > 2:
            to_remove.append(kode)

    for kode in to_remove:
        reason = state["tracked"][kode].get("drop_reason", "completed")
        log.info(f"[Suspension] Cleaned up {kode} from tracking ({reason}).")
        del state["tracked"][kode]


# ===========================================================================
# SHARED SCHEDULING
# ===========================================================================

def is_market_hours() -> bool:
    """Return True if current WIB time is Mon–Fri within market hours."""
    now = datetime.now(WIB)
    if now.weekday() >= 5:
        return False
    open_h, open_m   = MARKET_OPEN_TIME
    close_h, close_m = MARKET_CLOSE_TIME
    open_min  = open_h * 60 + open_m
    close_min = close_h * 60 + close_m
    curr_min  = now.hour * 60 + now.minute
    return open_min <= curr_min <= close_min


def get_sleep_interval() -> int:
    return MARKET_HOURS_INTERVAL_SEC if is_market_hours() else OFF_HOURS_INTERVAL_SEC


# ===========================================================================
# ENTRY POINT
# ===========================================================================

def main():
    log.info("=" * 60)
    log.info("IDX Monitor starting up")
    log.info(f"  Announcement Monitor : {'ON' if ENABLE_ANNOUNCEMENT_MONITOR else 'OFF'}")
    log.info(f"  Suspension Tracker   : {'ON' if ENABLE_SUSPENSION_TRACKER else 'OFF'}")

    # ── Announcement monitor setup ───────────────────────────────────────────
    ann_seen_ids  = set()
    ann_api_calls = []
    ann_first_run = True

    if ENABLE_ANNOUNCEMENT_MONITOR:
        ann_api_calls = _expand_rules(WATCH_RULES)
        log.info(f"[Announcement] {len(WATCH_RULES)} rules → "
                 f"{len(ann_api_calls)} unique API calls per cycle")
        for _, _, label in ann_api_calls:
            log.info(f"  • {label}")
        ann_seen_ids  = _load_announcement_state()
        ann_first_run = len(ann_seen_ids) == 0
        log.info(f"[Announcement] Loaded {len(ann_seen_ids)} seen post IDs.")
        if ann_first_run:
            log.info("[Announcement] No state — will seed on first cycle.")

    # ── Suspension tracker setup ─────────────────────────────────────────────
    susp_state    = {}
    susp_first_run = True

    if ENABLE_SUSPENSION_TRACKER:
        susp_state    = _load_suspension_state()
        susp_first_run = len(susp_state.get("seen_suspension_ids", [])) == 0
        if susp_first_run:
            log.info("[Suspension] No state — running backfill...")
            _backfill_suspension_history(susp_state)
            _save_suspension_state(susp_state)
            susp_first_run = False
        else:
            log.info(f"[Suspension] Loaded state. "
                     f"Tracking {len(susp_state.get('tracked', {}))} stocks. "
                     f"History for {len(susp_state.get('unsuspension_history', {}))} tickers.")

    # ── Main loop ────────────────────────────────────────────────────────────
    cycle = 0
    while True:
        cycle += 1
        log.info(f"[Cycle {cycle}] Polling... (market hours: {is_market_hours()})")

        # — Announcement monitor —
        if ENABLE_ANNOUNCEMENT_MONITOR:
            try:
                new_count = run_announcement_monitor(
                    ann_seen_ids, ann_api_calls, ann_first_run
                )
                ann_first_run = False
                _save_announcement_state(ann_seen_ids)
                if new_count:
                    log.info(f"[Cycle {cycle}][Announcement] {new_count} new post(s) notified.")
                else:
                    log.info(f"[Cycle {cycle}][Announcement] No new posts.")
            except Exception as e:
                log.error(f"[Cycle {cycle}][Announcement] Error: {e}", exc_info=True)

        # — Suspension tracker —
        if ENABLE_SUSPENSION_TRACKER:
            try:
                run_suspension_tracker(susp_state)
                _save_suspension_state(susp_state)
                active = sum(
                    1 for t in susp_state.get("tracked", {}).values()
                    if not t.get("dropped")
                )
                log.info(f"[Cycle {cycle}][Suspension] {active} stock(s) actively tracked.")
            except Exception as e:
                log.error(f"[Cycle {cycle}][Suspension] Error: {e}", exc_info=True)

        interval = get_sleep_interval()
        log.info(f"[Cycle {cycle}] Next check in {interval // 60} min "
                 f"({'market hours' if is_market_hours() else 'off hours'})")
        time.sleep(interval)


# ---------------------------------------------------------------------------
# DEBUGGING / TESTING FUNCTIONS
# ---------------------------------------------------------------------------

def debug_raw_response():
    """Debug: print raw JSON response from the announcement API for PYFA."""
    import urllib.parse
    params = {
        "kodeEmiten": "PYFA",
        "emitenType": "*",
        "indexFrom": 0,
        "pageSize": 5,
        "dateFrom": "19010101",
        "dateTo": date.today().strftime("%Y%m%d"),
        "lang": "id",
        "keyword": "",
    }
    url = IDX_ANNOUNCEMENT_URL + "?" + urllib.parse.urlencode(params)
    with CurlSession(impersonate="chrome124") as session:
        resp = session.get(url, headers=REQUEST_HEADERS, timeout=15)
        print("Status:", resp.status_code)
        print("Raw response:")
        print(resp.text[:2000])

def test_telegram():
    """Send a dummy notification to verify Telegram is configured correctly."""
    dummy_post = {
        "pengumuman": {
            "Id2": "TEST-001",
            "JudulPengumuman": "TEST — Pencatatan Saham",
            "Kode_Emiten": "PYFA",
            "TglPengumuman": datetime.now(WIB).isoformat(),
        },
        "attachments": [
            {
                "PDFFilename": "017b44a725_4e66aeb0e3.pdf",
                "FullSavePath": "https://www.idx.co.id/StaticData/NewsAndAnnouncement/ANNOUNCEMENTSTOCK/From_EREP/202604/017b44a725_4e66aeb0e3.pdf",
                "IsAttachment": False,
                "OriginalFilename": "20260430_PYFA_Pencatatan Saham Tambahan dari Konversi_32075946.pdf"
            },
            {
                "PDFFilename": "63d9478558_d1068b1f48.pdf",
                "FullSavePath": "https://www.idx.co.id/StaticData/NewsAndAnnouncement/ANNOUNCEMENTSTOCK/From_EREP/202604/63d9478558_d1068b1f48.pdf",
                "IsAttachment": True,
                "OriginalFilename": "20260430_PYFA_Pencatatan Saham Tambahan dari Konversi_32075946_lamp1.pdf"
            }
        ],
    }
    message = _format_announcement_notification(dummy_post, "keyword='pengambilalihan' (all emiten)")
    success = send_telegram(message)
    if success:
        log.info("✅ Telegram test passed — check your Telegram app")
    else:
        log.error("❌ Telegram test failed — check your BOT_TOKEN and CHAT_ID in .env")

if __name__ == "__main__":
    main()
    # Uncomment the following lines to run debug functions instead of the main monitor loop:
    # debug_raw_response()
    # test_telegram()