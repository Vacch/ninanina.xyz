# trippa-bot

A personal reservation bot for [Trippa](https://www.trippamilano.it/), the
trattoria in Milan where tables famously get booked in seconds after
reservations unlock. It waits for the unlock moment, then works through a
priority list of date/time slots you configure until one is booked, and
emails you the result.

## Before you rely on this: calibrate it

This was built without live access to trippamilano.it's booking widget, so
the CSS/text selectors in `SELECTORS` at the top of `bot.py` are best-effort
guesses at a typical booking-widget layout — **not verified against the real
page**. Treat the first run as a calibration step, not a real booking
attempt:

```bash
python bot.py inspect
```

This opens a visible browser on the real booking page and pauses with the
Playwright Inspector. Click through an actual booking by hand, note the real
selectors (right-click → Inspect on each date, time slot, and form field),
and update the `SELECTORS` dict in `bot.py` to match.

Then verify the automated flow gets all the way to the final step without
actually submitting:

```bash
python bot.py attempt --dry-run --headed
```

Repeat until this reliably reaches "ready to submit" for a slot that's
actually available on the site right now. Only then trust `python bot.py run`
for the real thing.

## Also confirm the unlock rule

You said reservations unlock at midnight on the 1st of the month
(`schedule.mode: monthly_first_day` in the config, which is the default).
Search results turned up conflicting descriptions of Trippa's current
system — some describing that monthly unlock, others describing a rolling
28-day window that opens one new day every night. Check the current rule
directly on [trippamilano.it/prenota-un-tavolo](https://www.trippamilano.it/prenota-un-tavolo/)
before the first real run, and switch `schedule.mode` to `daily` in
`config.yaml` if it turns out to be the rolling-window model instead.

## Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
playwright install chromium

cp config.example.yaml config.yaml
# edit config.yaml with your real details and preferred slots
```

For email notifications, create a [Gmail app password](https://myaccount.google.com/apppasswords)
and export it as the environment variable named in
`notifications.smtp_password_env` (default `TRIPPA_BOT_SMTP_PASSWORD`):

```bash
export TRIPPA_BOT_SMTP_PASSWORD="your-16-char-app-password"
```

## Usage

```bash
python bot.py inspect             # find real selectors (see above)
python bot.py attempt --dry-run   # test the flow without submitting
python bot.py attempt             # attempt a real booking right now
python bot.py run                 # wait for the unlock moment, then attempt
```

`run` sleeps until `schedule.prewarm_seconds_before` seconds before the
target time, loads the page so it's warm, then busy-waits with sub-second
precision until the exact target instant before trying anything. If the
preferred slots aren't available yet, it retries every
`schedule.retry_interval_seconds` for up to `schedule.retry_window_seconds`.

## Running it unattended

You chose to run this locally rather than on a schedule you don't control
(e.g. GitHub Actions), since precise timing matters here and cron-based CI
schedulers can lag by several minutes — especially right at midnight on the
1st, when lots of other scheduled jobs fire at once. Options on your own
machine/server:

**cron** (add a couple of minutes before the target time so `run` is
already prewarming when the clock hits the target):

```
55 23 * * * cd /path/to/trippa-bot && /path/to/venv/bin/python bot.py run >> run.log 2>&1
```

**systemd timer** — more reliable than cron if your machine sleeps/wakes,
since systemd can catch up on missed timers:

```ini
# /etc/systemd/system/trippa-bot.service
[Unit]
Description=Trippa reservation bot

[Service]
Type=oneshot
WorkingDirectory=/path/to/trippa-bot
Environment=TRIPPA_BOT_SMTP_PASSWORD=your-app-password
ExecStart=/path/to/venv/bin/python bot.py run
```

```ini
# /etc/systemd/system/trippa-bot.timer
[Unit]
Description=Run trippa-bot a bit before each month's unlock

[Timer]
OnCalendar=*-*-01 23:55:00
Persistent=true

[Install]
WantedBy=timers.target
```

```bash
sudo systemctl enable --now trippa-bot.timer
```

Make sure the machine's clock is NTP-synced (`timedatectl` should show
"System clock synchronized: yes") — a few seconds of drift is the
difference between a table and nothing.

## Being a decent user of Trippa's booking system

- This is for booking a table for yourself, not for resale or running many
  instances at once.
- Keep `retry_window_seconds`/`retry_interval_seconds` modest — the goal is
  to not miss the exact unlock moment, not to hammer the site.
- Trippa requires reconfirming via email or WhatsApp 24 hours before your
  reservation — set a reminder, this bot doesn't handle that step.
- Automated booking may not be something the restaurant's booking platform
  explicitly endorses; use your own judgment about whether that's fine for
  an occasional personal booking.
