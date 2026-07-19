#!/usr/bin/env python3
"""Reservation bot for Trippa (Milano).

Waits for the moment reservations unlock, then tries a prioritized list of
date/time slots until one is booked, and emails the outcome.

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
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError, sync_playwright

CONFIG_PATH = Path(__file__).parent / "config.yaml"
SCREENSHOT_PATH = Path(__file__).parent / "last_attempt.png"

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


@dataclass
class Slot:
    date: str  # YYYY-MM-DD
    time: str  # HH:MM


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        sys.exit(
            f"Missing {CONFIG_PATH}. Copy config.example.yaml to config.yaml "
            "and fill in your details first."
        )
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def compute_target_datetime(schedule: dict, now: datetime) -> datetime:
    tz = now.tzinfo
    hour, minute, second = schedule["hour"], schedule["minute"], schedule["second"]

    if schedule["mode"] == "daily":
        target = now.replace(hour=hour, minute=minute, second=second, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return target

    if schedule["mode"] == "monthly_first_day":
        candidate = now.replace(day=1, hour=hour, minute=minute, second=second, microsecond=0)
        if candidate <= now:
            # move to the 1st of next month
            if now.month == 12:
                candidate = candidate.replace(year=now.year + 1, month=1)
            else:
                candidate = candidate.replace(month=now.month + 1)
        return candidate

    raise ValueError(f"Unknown schedule mode: {schedule['mode']}")


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


def try_slot(page: Page, slot: Slot, party_size: int) -> bool:
    """Attempt to select one candidate slot. Returns True if it looks selected."""
    day = str(int(slot.date.split("-")[2]))  # e.g. "15" -> "15", strip leading zero

    try:
        page.select_option(SELECTORS["party_size_select"], value=str(party_size), timeout=3000)
    except Exception:
        pass  # some widgets default party size before showing dates; not fatal

    try:
        page.locator(SELECTORS["date_button"].format(day=day)).first.click(timeout=4000)
    except PlaywrightTimeoutError:
        print(f"  date {slot.date} not clickable/found")
        return False

    try:
        page.locator(SELECTORS["time_slot_button"].format(time=slot.time)).first.click(timeout=4000)
    except PlaywrightTimeoutError:
        print(f"  time {slot.time} not available for {slot.date}")
        return False

    return True


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


def attempt_booking(page: Page, config: dict, dry_run: bool) -> tuple[bool, str]:
    page.goto(config["booking"]["url"], wait_until="domcontentloaded")
    dismiss_cookie_banner(page)

    slots = [Slot(s["date"], s["time"]) for s in config["booking"]["preferred_slots"]]
    party_size = config["booking"]["party_size"]

    for slot in slots:
        print(f"Trying {slot.date} {slot.time} ...")
        if try_slot(page, slot, party_size):
            fill_contact_details(page, config["contact"])

            if dry_run:
                page.screenshot(path=str(SCREENSHOT_PATH))
                return True, f"DRY RUN: reached submit step for {slot.date} {slot.time}, did not submit."

            try:
                page.locator(SELECTORS["submit_button"]).first.click(timeout=5000)
                page.locator(SELECTORS["confirmation_text"]).first.wait_for(timeout=8000)
                page.screenshot(path=str(SCREENSHOT_PATH))
                return True, f"Booked {slot.date} {slot.time} for {party_size} people."
            except PlaywrightTimeoutError:
                page.screenshot(path=str(SCREENSHOT_PATH))
                return False, f"Submitted for {slot.date} {slot.time} but no confirmation seen - check manually."

    page.screenshot(path=str(SCREENSHOT_PATH))
    return False, "None of the preferred slots were available."


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


def run_once(config: dict, dry_run: bool, headless: bool) -> tuple[bool, str]:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        page = browser.new_page()
        try:
            success, message = attempt_booking(page, config, dry_run)
        finally:
            browser.close()
    return success, message


def cmd_run(args):
    config = load_config()
    tz = ZoneInfo(config["timezone"])
    schedule = config["schedule"]

    target = compute_target_datetime(schedule, datetime.now(tz))
    prewarm_at = target - timedelta(seconds=schedule["prewarm_seconds_before"])
    print(f"Next target: {target.isoformat()}")

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
            success, message = attempt_booking(page, config, dry_run=False)
            if success:
                break
            print(f"Retry: {message}")
            time.sleep(schedule["retry_interval_seconds"])
            page.goto(config["booking"]["url"], wait_until="domcontentloaded")
            dismiss_cookie_banner(page)

        browser.close()

    print(message)
    subject = "Trippa booking: SUCCESS" if success else "Trippa booking: FAILED"
    send_email(config, subject, message, SCREENSHOT_PATH)


def cmd_attempt(args):
    config = load_config()
    success, message = run_once(config, dry_run=args.dry_run, headless=not args.headed)
    print(message)
    if not args.dry_run:
        subject = "Trippa booking: SUCCESS" if success else "Trippa booking: FAILED"
        send_email(config, subject, message, SCREENSHOT_PATH)


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

    sub.add_parser("inspect", help="Open a headed browser + Playwright Inspector to find real selectors.")

    args = parser.parse_args()
    {"run": cmd_run, "attempt": cmd_attempt, "inspect": cmd_inspect}[args.command](args)


if __name__ == "__main__":
    main()
