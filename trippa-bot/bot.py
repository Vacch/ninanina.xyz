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
HAR_DIR = Path(__file__).parent / "captures"
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
    page.locator(SELECTORS["last_name_field"]).first.fill(contact["surname"])
    page.locator(SELECTORS["email_field"]).first.fill(contact["email"])
    page.locator(SELECTORS["phone_field"]).first.fill(contact["mobile"])
    if contact.get("special_requests"):
        try:
            page.locator(SELECTORS["notes_field"]).first.fill(contact["special_requests"])
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


def send_email(config: dict, subject: str, body: str, attachments: list = None):
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

    for attachment in attachments or []:
        if not attachment or not attachment.exists():
            continue
        maintype, subtype = ("image", "png") if attachment.suffix == ".png" else ("application", "octet-stream")
        msg.add_attachment(attachment.read_bytes(), maintype=maintype, subtype=subtype, filename=attachment.name)

    with smtplib.SMTP(notif["smtp_host"], notif["smtp_port"]) as server:
        server.starttls()
        server.login(notif["smtp_user"], password)
        server.send_message(msg)


def run_once(
    config: dict, target_date: date, dry_run: bool, headless: bool, har_path: Path | None = None
) -> tuple[bool, str]:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context(record_har_path=str(har_path) if har_path else None)
        page = context.new_page()
        try:
            success, message = attempt_booking(page, config, target_date, dry_run)
        finally:
            context.close()  # flushes the HAR file, if recording
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
        send_email(config, "Trippa booking: closed that night, skipped", message)
        return

    prewarm_at = target - timedelta(seconds=schedule["prewarm_seconds_before"])
    print(f"Sleeping until prewarm time {prewarm_at.isoformat()}")
    sleep_until(prewarm_at)

    HAR_DIR.mkdir(exist_ok=True)
    har_path = HAR_DIR / f"{target_date.isoformat()}_{datetime.now(tz):%H%M%S}.har"
    print(f"Prewarming page (recording full network traffic to {har_path}) ...")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(record_har_path=str(har_path))
        page = context.new_page()
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

        context.close()  # flushes har_path to disk
        browser.close()
    print(f"Full network capture saved to {har_path} - send it over even if the attempt failed.")

    print(message)
    subject = "Trippa booking: SUCCESS" if success else "Trippa booking: FAILED"
    send_email(
        config,
        subject,
        f"{message}\n\nFull network capture attached ({har_path.name}) - send it back over "
        "if the booking step still needs work, it has everything needed to finish wiring up "
        "the direct API call.",
        [SCREENSHOT_PATH, har_path],
    )


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

    har_path = None
    if args.har:
        HAR_DIR.mkdir(exist_ok=True)
        har_path = HAR_DIR / f"attempt_{target_date.isoformat()}_{datetime.now(tz):%H%M%S}.har"

    success, message = run_once(config, target_date, dry_run=args.dry_run, headless=not args.headed, har_path=har_path)
    print(message)
    if har_path:
        print(f"Network capture saved to {har_path}")
    if not args.dry_run:
        subject = "Trippa booking: SUCCESS" if success else "Trippa booking: FAILED"
        send_email(config, subject, message, [SCREENSHOT_PATH, har_path])


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


def cmd_standby(args):
    """Join Trippa's real waitlist for a specific date/time via the
    confirmed AddToStandbyList API - no browser needed. This creates a
    real entry the restaurant will see, not a test."""
    config = load_config()
    channel_code = config["booking"].get("channel_code", "INGLESE")
    party_size = config["booking"]["party_size"]
    customer = resdiary.build_customer(config["contact"])

    if not args.yes:
        sys.exit(
            f"This will really join the waitlist for {args.date} {args.time}, "
            f"party of {party_size}. Re-run with --yes to confirm."
        )

    result = resdiary.add_to_standby_list(
        args.date, args.time, party_size, channel_code, customer,
        special_requests=config["contact"].get("special_requests", ""),
    )
    booking = result.get("Booking", {})
    print(f"Status: {result.get('Status')}, reference: {booking.get('Reference')}, errors: {result.get('Errors')}")


def ask(prompt_text: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default not in (None, "") else ""
    while True:
        value = input(f"{prompt_text}{suffix}: ").strip()
        if value:
            return value
        if default is not None:
            return default
        print("  Campo obbligatorio.")


def ask_int(prompt_text: str, default: int) -> int:
    while True:
        value = ask(prompt_text, str(default))
        try:
            return int(value)
        except ValueError:
            print("  Inserisci un numero.")


def ask_date(prompt_text: str, default: str | None = None) -> date:
    while True:
        value = ask(prompt_text, default)
        try:
            return date.fromisoformat(value)
        except ValueError:
            print("  Formato data non valido, usa YYYY-MM-DD.")


def ask_yes_no(prompt_text: str, default: bool) -> bool:
    hint = "S/n" if default else "s/N"
    value = input(f"{prompt_text} [{hint}]: ").strip().lower()
    if not value:
        return default
    return value in ("s", "si", "sì", "y", "yes")


def ask_contact(config: dict) -> dict:
    """Ask who the booking is for - defaults to whatever's in config.yaml,
    but every field can be overridden, so you can book on someone else's
    behalf without touching any file."""
    existing = config.get("contact", {})
    print("\nA nome di chi vuoi prenotare? (invio per confermare il valore tra parentesi)")
    return {
        "first_name": ask("Nome", existing.get("first_name")),
        "surname": ask("Cognome", existing.get("surname")),
        "mobile_country_code": ask("Prefisso cellulare (senza +)", existing.get("mobile_country_code", "39")),
        "mobile": ask("Cellulare (senza prefisso)", existing.get("mobile")),
        "email": ask("Email", existing.get("email")),
        "special_requests": ask("Richieste speciali", existing.get("special_requests", "")),
    }


def ask_booking_basics(config: dict) -> tuple:
    party_size = ask_int("Numero di persone", config["booking"]["party_size"])
    target_date = ask_date("Data (YYYY-MM-DD)")
    return party_size, target_date


def build_session_config(config: dict, party_size: int, preferred_times: list, contact: dict) -> dict:
    """A copy of config with just this session's party size/times/contact
    swapped in, so run_once()/resdiary calls can be reused unchanged."""
    session = dict(config)
    session["booking"] = {**config["booking"], "party_size": party_size, "preferred_times": preferred_times}
    session["contact"] = contact
    return session


def cmd_interactive(args):
    config = load_config()
    channel_code = config["booking"].get("channel_code", "INGLESE")

    print("=== Trippa Booking Bot - modalità interattiva ===")
    print("1) Controlla la disponibilità reale (sola lettura)")
    print("2) Iscriviti alla lista d'attesa (azione REALE)")
    print("3) Prova/esegui una prenotazione per una data già aperta (via browser)")
    print("4) Aspetta la mezzanotte di stanotte e prova a prenotare (come 'run')")
    choice = ask("\nScegli un'opzione", "1")

    if choice == "1":
        party_size, target_date = ask_booking_basics(config)
        times = resdiary.available_times(target_date.isoformat(), party_size, channel_code)
        print(f"\nPrenotazione diretta - {target_date.isoformat()}: {', '.join(times) or 'nessuno slot libero'}")
        standby = resdiary.standby_dates_in_range(
            target_date.isoformat(), target_date.isoformat(), party_size, channel_code
        ).get(target_date.isoformat(), [])
        print(f"Lista d'attesa - {target_date.isoformat()}: {', '.join(standby) or 'nessuno slot libero'}")
        return

    if choice == "2":
        party_size, target_date = ask_booking_basics(config)
        time_str = ask("Orario (HH:MM)")
        contact = ask_contact(config)
        print("\nRiepilogo:")
        print(f"  {target_date.isoformat()} {time_str}, {party_size} persone")
        print(f"  {contact['first_name']} {contact['surname']} - {contact['email']} - "
              f"+{contact['mobile_country_code']}{contact['mobile']}")
        if not ask_yes_no("\nConfermi l'iscrizione REALE alla lista d'attesa?", False):
            print("Annullato, nessuna richiesta inviata.")
            return
        customer = resdiary.build_customer(contact)
        result = resdiary.add_to_standby_list(
            target_date.isoformat(), time_str, party_size, channel_code, customer,
            special_requests=contact.get("special_requests", ""),
        )
        booking = result.get("Booking", {})
        print(f"\nEsito: {result.get('Status')} - riferimento {booking.get('Reference')} - errori: {result.get('Errors')}")
        return

    if choice == "3":
        party_size, target_date = ask_booking_basics(config)
        contact = ask_contact(config)
        default_times = ",".join(config["booking"]["preferred_times"])
        raw_times = ask("Orari preferiti in ordine, separati da virgola", default_times)
        preferred_times = [t.strip() for t in raw_times.split(",") if t.strip()]
        dry_run = ask_yes_no("\nModalità prova (non invia la conferma finale)?", True)
        headed = ask_yes_no("Mostrare il browser mentre lavora?", True)

        session_config = build_session_config(config, party_size, preferred_times, contact)
        success, message = run_once(session_config, target_date, dry_run=dry_run, headless=not headed)
        print(f"\nEsito: {message}")
        return

    if choice == "4":
        print("\nAvvio l'attesa della mezzanotte di stanotte (equivalente a 'python bot.py run') ...")
        cmd_run(args)
        return

    print("Scelta non valida.")


def cmd_inspect(args):
    config = load_config()
    HAR_DIR.mkdir(exist_ok=True)
    har_path = HAR_DIR / f"inspect_{datetime.now():%Y%m%d_%H%M%S}.har"
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(record_har_path=str(har_path))
        page = context.new_page()
        page.goto(config["booking"]["url"], wait_until="domcontentloaded")
        print("Playwright Inspector paused - click through a booking by hand,")
        print("note the real selectors, then update SELECTORS in bot.py.")
        print(f"Everything you do is also being recorded to {har_path} - no need for manual DevTools HAR capture.")
        page.pause()
        context.close()
        browser.close()
    print(f"Network capture saved to {har_path}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=False)

    sub.add_parser(
        "interactive",
        help="Ask questions in the terminal instead of editing files - the default with no arguments.",
    )

    sub.add_parser("run", help="Wait for the unlock moment, then attempt the booking for real.")

    p_attempt = sub.add_parser("attempt", help="Attempt right now (for testing/calibration).")
    p_attempt.add_argument("--dry-run", action="store_true", help="Stop before the final submit click.")
    p_attempt.add_argument("--headed", action="store_true", help="Show the browser window.")
    p_attempt.add_argument(
        "--date",
        help="Target date YYYY-MM-DD to test against (defaults to the last date already open "
        "in the current window, so you can test without waiting for midnight).",
    )
    p_attempt.add_argument("--har", action="store_true", help="Record the full network traffic to a HAR file.")

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

    p_standby = sub.add_parser(
        "standby", help="Join the real waitlist for a specific date/time (creates a real entry)."
    )
    p_standby.add_argument("--date", required=True, help="Date YYYY-MM-DD.")
    p_standby.add_argument("--time", required=True, help="Time HH:MM.")
    p_standby.add_argument("--yes", action="store_true", help="Confirm you want to really do this.")

    args = parser.parse_args()
    command = args.command or "interactive"
    {
        "interactive": cmd_interactive,
        "run": cmd_run,
        "attempt": cmd_attempt,
        "inspect": cmd_inspect,
        "check": cmd_check,
        "availability": cmd_availability,
        "standby": cmd_standby,
    }[command](args)


if __name__ == "__main__":
    main()
