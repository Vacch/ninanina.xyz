#!/usr/bin/env python3
"""Reservation bot for Trippa (Milano).

Trippa uses a rolling booking window: every night at midnight, exactly one
new date becomes bookable - the day that is `target_offset_days` (28) days
ahead. This bot wakes up right before each midnight, and the instant the
window rolls over, tries a prioritized list of times for that single new
date until one is booked, then emails the outcome.

IMPORTANT - calibration required before real use:
This script was written without live access to trippamilano.it's booking
widget, so the selectors in `SELECTORS` below are best-effort guesses at a
typical ResDiary widget layout, not verified against the real page. Before
relying on this for an actual booking:

    1. Run `python bot.py inspect` - this opens a real (headed) browser on
       the booking page and pauses with the Playwright Inspector, so you can
       click through a real booking by hand and see the actual selectors.
    2. Update SELECTORS below to match what you find.
    3. Run `python bot.py attempt --dry-run` a few times until it reliably
       gets all the way to "ready to submit" without touching the final
       submit button.
    4. Only then use `python bot.py run` (or schedule it) for real.

Use responsibly: this is meant for booking a table for yourself, not for
reselling or running at a scale that hammers the restaurant's booking
system. Keep retry_window_seconds/retry_interval_seconds modest.
"""
import argparse
import os
import smtplib
import sys
import time
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError, sync_playwright

import resdiary

CONFIG_PATH = Path(__file__).parent / "config.yaml"
SCREENSHOT_PATH = Path(__file__).parent / "last_attempt.png"
WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

# --- CALIBRATE ME -----------------------------------------------------
# Best-effort guesses at the widget's structure. Confirm/fix these with
# `python bot.py inspect` before trusting the bot with a real attempt.
SELECTORS = {
    "cookie_accept_texts": ["Accetta", "Accetta tutti", "Accept", "Accept all"],
    "party_size_select": "select[id*='cover' i], select[name*='cover' i], select[id*='guest' i]",
    "date_button": "button[aria-label*='{day} ' i], button:has-text('{day}')",
    "time_slot_button": "button:has-text('{time}'), a:has-text('{time}')",
    "continue_button": "button:has-text('Continua'), button:has-text('Continue'), button:has-text('Next')",
    "first_name_field": "input[name*='first' i], input[id*='first' i]",
    "last_name_field": "input[name*='last' i], input[id*='last' i], input[name*='surname' i]",
    "email_field": "input[type='email'], input[name*='email' i]",
    "phone_field": "input[type='tel'], input[name*='phone' i], input[name*='mobile' i]",
    "notes_field": "textarea[name*='note' i], textarea[id*='note' i]",
    "submit_button": "button:has-text('Conferma'), button:has-text('Confirm'), button:has-text('Prenota'), button:has-text('Book')",
    "confirmation_text": "text=/confermat|confirmed|grazie|thank you/i",
}
# ------------------------------------------------------------------------


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        sys.exit(
            f"Missing {CONFIG_PATH}. Copy config.example.yaml to config.yaml "
            "and fill in your details first."
        )
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def compute_target_datetime(schedule: dict, now: datetime) -> datetime:
    """The next midnight (or configured hour/minute/second) at which the
    window rolls over and a new date unlocks."""
    target = now.replace(
        hour=schedule["hour"], minute=schedule["minute"], second=schedule["second"], microsecond=0
    )
    if target <= now:
        target += timedelta(days=1)
    return target


def compute_unlock_date(target: datetime, offset_days: int) -> date:
    """The single date that becomes bookable at `target` (the midnight it
    rolls over to, plus the rolling window length)."""
    return target.date() + timedelta(days=offset_days)


def closed_reason(target_date: date, closed_weekdays: list) -> str | None:
    """None if the restaurant is open that day, otherwise why it's closed."""
    weekday = WEEKDAY_NAMES[target_date.weekday()]
    if weekday in closed_weekdays:
        return f"{target_date.isoformat()} is a {weekday} - Trippa is closed that day."
    return None


def sleep_until(target: datetime):
    """Sleep until target, coarsely at first then spinning the last second
    for sub-second precision."""
    remaining = (target - datetime.now(target.tzinfo)).total_seconds()
    if remaining > 1:
        time.sleep(remaining - 1)
    while datetime.now(target.tzinfo) < target:
        pass


def dismiss_cookie_banner(page: Page):
    for text in SELECTORS["cookie_accept_texts"]:
        try:
            page.get_by_text(text, exact=False).first.click(timeout=1500)
            return
        except PlaywrightTimeoutError:
            continue


def select_date(page: Page, target_date: date, party_size: int) -> bool:
    """Select party size and the single newly-unlocked date. Returns True if
    the date looks selected."""
    day = str(target_date.day)  # e.g. 15, no leading zero

    try:
        page.select_option(SELECTORS["party_size_select"], value=str(party_size), timeout=3000)
    except Exception:
        pass  # some widgets default party size before showing dates; not fatal

    try:
        page.locator(SELECTORS["date_button"].format(day=day)).first.click(timeout=4000)
        return True
    except PlaywrightTimeoutError:
        print(f"  date {target_date.isoformat()} not clickable/found - window may not have rolled over yet")
        return False


def try_time(page: Page, time_str: str) -> bool:
    try:
        page.locator(SELECTORS["time_slot_button"].format(time=time_str)).first.click(timeout=4000)
        return True
    except PlaywrightTimeoutError:
        print(f"  time {time_str} not available in the widget")
        return False


def pick_available_time(config: dict, target_date: date) -> str | None:
    """Ask Trippa's real booking API (see resdiary.py) which of our
    preferred times are actually open for target_date, instead of
    click-testing each one blindly in the browser."""
    booking = config["booking"]
    channel_code = booking.get("channel_code", "INGLESE")
    live_times = resdiary.available_times(target_date.isoformat(), booking["party_size"], channel_code)
    print(f"  API live availability for {target_date.isoformat()}: {live_times or 'none'}")
    for time_str in booking["preferred_times"]:
        if time_str in live_times:
            return time_str
    return None


def fill_contact_details(page: Page, contact: dict):
    try:
        page.locator(SELECTORS["continue_button"]).first.click(timeout=4000)
    except PlaywrightTimeoutError:
        pass  # some widgets go straight to the details form

    page.locator(SELECTORS["first_name_field"]).first.fill(contact["first_name"])
    page.locator(SELECTORS["last_name_field"]).first.fill(contact["last_name"])
    page.locator(SELECTORS["email_field"]).first.fill(contact["email"])
    page.locator(SELECTORS["phone_field"]).first.fill(contact["phone"])
    if contact.get("notes"):
        try:
            page.locator(SELECTORS["notes_field"]).first.fill(contact["notes"])
        except Exception:
            pass


def attempt_booking(page: Page, config: dict, target_date: date, dry_run: bool) -> tuple[bool, str]:
    party_size = config["booking"]["party_size"]

    time_str = pick_available_time(config, target_date)
    if time_str is None:
        return False, f"None of the preferred times were available on {target_date.isoformat()} (checked via API)."

    page.goto(config["booking"]["url"], wait_until="domcontentloaded")
    dismiss_cookie_banner(page)

    if not select_date(page, target_date, party_size):
        page.screenshot(path=str(SCREENSHOT_PATH))
        return False, f"API said {target_date.isoformat()} was open but the widget wouldn't select that date."

    print(f"Trying {target_date.isoformat()} {time_str} ...")
    if not try_time(page, time_str):
        page.screenshot(path=str(SCREENSHOT_PATH))
        return False, (
            f"API said {time_str} was open on {target_date.isoformat()} but the widget wouldn't select "
            "it - selectors may need recalibration, or someone else took it first."
        )

    fill_contact_details(page, config["contact"])

    if dry_run:
        page.screenshot(path=str(SCREENSHOT_PATH))
        return True, f"DRY RUN: reached submit step for {target_date.isoformat()} {time_str}, did not submit."

    try:
        page.locator(SELECTORS["submit_button"]).first.click(timeout=5000)
        page.locator(SELECTORS["confirmation_text"]).first.wait_for(timeout=8000)
        page.screenshot(path=str(SCREENSHOT_PATH))
        return True, f"Booked {target_date.isoformat()} {time_str} for {party_size} people."
    except PlaywrightTimeoutError:
        page.screenshot(path=str(SCREENSHOT_PATH))
        return False, f"Submitted for {target_date.isoformat()} {time_str} but no confirmation seen - check manually."


def send_email(config: dict, subject: str, body: str, attachment: Path | None):
    notif = config["notifications"]
    if not notif.get("enabled", True):
        return

    password = os.environ.get(notif["smtp_password_env"])
    if not password:
        print(f"Warning: {notif['smtp_password_env']} not set, skipping email.")
        return

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = notif["smtp_user"]
    msg["To"] = notif["to"]
    msg.set_content(body)

    if attachment and attachment.exists():
        msg.add_attachment(
            attachment.read_bytes(), maintype="image", subtype="png", filename=attachment.name
        )

    with smtplib.SMTP(notif["smtp_host"], notif["smtp_port"]) as server:
        server.starttls()
        server.login(notif["smtp_user"], password)
        server.send_message(msg)


def run_once(config: dict, target_date: date, dry_run: bool, headless: bool) -> tuple[bool, str]:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        page = browser.new_page()
        try:
            success, message = attempt_booking(page, config, target_date, dry_run)
        finally:
            browser.close()
    return success, message


def cmd_run(args):
    config = load_config()
    tz = ZoneInfo(config["timezone"])
    schedule = config["schedule"]

    target = compute_target_datetime(schedule, datetime.now(tz))
    target_date = compute_unlock_date(target, config["booking"]["target_offset_days"])
    print(f"Next rollover: {target.isoformat()}, unlocking {target_date.isoformat()}")

    reason = closed_reason(target_date, config["booking"].get("closed_weekdays", []))
    if reason:
        message = f"{reason} Nothing to book tonight - skipping."
        print(message)
        send_email(config, "Trippa booking: closed that night, skipped", message, None)
        return

    prewarm_at = target - timedelta(seconds=schedule["prewarm_seconds_before"])
    print(f"Sleeping until prewarm time {prewarm_at.isoformat()}")
    sleep_until(prewarm_at)

    print("Prewarming page ...")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(config["booking"]["url"], wait_until="domcontentloaded")
        dismiss_cookie_banner(page)

        sleep_until(target)

        deadline = time.monotonic() + schedule["retry_window_seconds"]
        success, message = False, "No attempt made."
        while time.monotonic() < deadline:
            success, message = attempt_booking(page, config, target_date, dry_run=False)
            if success:
                break
            print(f"Retry: {message}")
            time.sleep(schedule["retry_interval_seconds"])

        browser.close()

    print(message)
    subject = "Trippa booking: SUCCESS" if success else "Trippa booking: FAILED"
    send_email(config, subject, message, SCREENSHOT_PATH)


def cmd_attempt(args):
    config = load_config()
    tz = ZoneInfo(config["timezone"])

    if args.date:
        target_date = date.fromisoformat(args.date)
    else:
        # An already-open date (yesterday's rollover), handy for testing the
        # flow without waiting for an actual midnight.
        offset = config["booking"]["target_offset_days"]
        target_date = datetime.now(tz).date() + timedelta(days=offset - 1)

    success, message = run_once(config, target_date, dry_run=args.dry_run, headless=not args.headed)
    print(message)
    if not args.dry_run:
        subject = "Trippa booking: SUCCESS" if success else "Trippa booking: FAILED"
        send_email(config, subject, message, SCREENSHOT_PATH)


def cmd_check(args):
    """Look ahead at which upcoming rollovers will unlock a date the
    restaurant is actually open on, without waiting for anything."""
    config = load_config()
    tz = ZoneInfo(config["timezone"])
    schedule = config["schedule"]
    offset = config["booking"]["target_offset_days"]
    closed_weekdays = config["booking"].get("closed_weekdays", [])

    target = compute_target_datetime(schedule, datetime.now(tz))
    for i in range(args.nights):
        rollover = target + timedelta(days=i)
        target_date = compute_unlock_date(rollover, offset)
        reason = closed_reason(target_date, closed_weekdays)
        weekday = WEEKDAY_NAMES[target_date.weekday()]
        status = f"CLOSED ({reason})" if reason else "open - will be attempted"
        print(f"{rollover.date().isoformat()} midnight -> unlocks {target_date.isoformat()} ({weekday}): {status}")


def cmd_availability(args):
    """Query Trippa's real booking API directly - no browser needed. Handy
    to sanity-check resdiary.py, or to peek at the standby/waitlist dates
    that are open right now."""
    config = load_config()
    party_size = config["booking"]["party_size"]
    channel_code = config["booking"].get("channel_code", "INGLESE")

    if args.standby:
        date_from = args.date or date.today().isoformat()
        date_to = args.date_to or (date.fromisoformat(date_from) + timedelta(days=30)).isoformat()
        results = resdiary.standby_dates_in_range(date_from, date_to, party_size, channel_code)
        label = "standby/waitlist"
    elif args.date_to:
        date_from = args.date or date.today().isoformat()
        results = resdiary.available_dates_in_range(date_from, args.date_to, party_size, channel_code)
        label = "reservation"
    else:
        target = args.date or date.today().isoformat()
        times = resdiary.available_times(target, party_size, channel_code)
        results = {target: times}
        label = "reservation"

    if not any(results.values()):
        print(f"No {label} availability found for the given range.")
        return
    for d, times in sorted(results.items()):
        if times:
            print(f"{d} ({label}): {', '.join(times)}")


def cmd_inspect(args):
    config = load_config()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        page = browser.new_page()
        page.goto(config["booking"]["url"], wait_until="domcontentloaded")
        print("Playwright Inspector paused - click through a booking by hand,")
        print("note the real selectors, then update SELECTORS in bot.py.")
        page.pause()
        browser.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("run", help="Wait for the unlock moment, then attempt the booking for real.")

    p_attempt = sub.add_parser("attempt", help="Attempt right now (for testing/calibration).")
    p_attempt.add_argument("--dry-run", action="store_true", help="Stop before the final submit click.")
    p_attempt.add_argument("--headed", action="store_true", help="Show the browser window.")
    p_attempt.add_argument(
        "--date",
        help="Target date YYYY-MM-DD to test against (defaults to the last date already open "
        "in the current window, so you can test without waiting for midnight).",
    )

    sub.add_parser("inspect", help="Open a headed browser + Playwright Inspector to find real selectors.")

    p_check = sub.add_parser(
        "check", help="Look ahead: which of the next N rollovers will land on a closed day."
    )
    p_check.add_argument("--nights", type=int, default=7, help="How many upcoming nights to preview.")

    p_avail = sub.add_parser(
        "availability", help="Query the real booking API directly (no browser) for open dates/times."
    )
    p_avail.add_argument("--date", help="Date YYYY-MM-DD to check (defaults to today).")
    p_avail.add_argument("--date-to", help="End of range YYYY-MM-DD, for a multi-day look.")
    p_avail.add_argument("--standby", action="store_true", help="Check the waitlist instead of direct reservations.")

    args = parser.parse_args()
    {
        "run": cmd_run,
        "attempt": cmd_attempt,
        "inspect": cmd_inspect,
        "check": cmd_check,
        "availability": cmd_availability,
    }[args.command](args)


if __name__ == "__main__":
    main()
