# trippa-bot

A personal reservation bot for [Trippa](https://www.trippamilano.it/), the
trattoria in Milan where tables famously get booked in seconds after
reservations unlock. Trippa uses a rolling window: every night at midnight,
exactly one new date becomes bookable — the day `target_offset_days` (28)
days ahead. The bot wakes up right before each midnight, and the instant the
window rolls over, works through a priority list of times for that one new
date until one is booked, then emails you the result.

## Interactive mode

You don't have to edit `config.yaml` for every booking. Just run it with no
arguments (or `python bot.py interactive`) and it asks you questions in the
terminal instead:

```bash
python bot.py
```

```
=== Trippa Booking Bot - modalità interattiva ===
1) Controlla la disponibilità reale (sola lettura)
2) Iscriviti alla lista d'attesa (azione REALE)
3) Prova/esegui una prenotazione per una data già aperta (via browser)
4) Aspetta la mezzanotte di stanotte e prova a prenotare (come 'run')
```

Option 2 (the one that actually works end-to-end today, see below) asks
for party size, date, time, and **who the booking is for** - name,
surname, mobile, email - pre-filled from `config.yaml` as defaults but
fully overridable, so booking for a friend or relative is just typing
different answers, no file editing needed. It shows a summary and asks for
explicit confirmation before actually joining the waitlist.

`config.yaml` is still used as the source of the *stable* settings
(SMTP, channel_code, closed days, offset, `run`'s schedule) - interactive
mode only overrides the per-booking fields (who/when/how many) for that
one session, it doesn't write anything back to the file.

## Every run keeps a full log

Every command (`run`, `attempt`, `standby`, `interactive`, ...) mirrors
everything it prints - including full tracebacks on errors - into
`logs/<command>_<timestamp>.log`. If something fails and you're not sure
why, just grab that file and send it over for debugging; you don't need to
copy-paste terminal output or remember exactly what happened.

`run`/`attempt`/`standby` also attach their log automatically to the
result email, alongside the screenshot and HAR capture, so you get it even
without going to look at the `logs/` folder yourself.

## The real booking API

Thanks to a HAR capture, `resdiary.py` now talks directly to Trippa's actual
booking API (`booking.resdiary.com`, restaurant code `TRATTORIATRIPPA`) - no
login needed, it's the same public JSON API the browser widget itself calls.
This means checking availability no longer depends on clicking through the
DOM at all:

```bash
python bot.py availability --date 2026-07-27              # a single date
python bot.py availability --date-to 2026-08-20            # today .. that date, range
python bot.py availability --date 2026-07-20 --standby     # the waitlist instead
```

`bot.py` uses this internally too: before touching the browser at all, it
asks the API which of your `preferred_times` are actually open for the
target date, and only drives Playwright to click through that one specific,
already-confirmed-available slot - instead of blindly clicking each
preferred time in turn and hoping.

**Caveat on `channel_code`:** the capture was made with English as the
browser's preferred language, and the channel came back as `"INGLESE"`
(Italian for "English"). That suggests Trippa may split availability by
site language. If you normally book in Italian, capture one more HAR with
Italian as the primary `Accept-Language` and check whether `ChannelCode`
differs there - if so, set `booking.channel_code` in `config.yaml` to match,
otherwise you may be checking (or booking into) the wrong pool of tables.

**The waitlist call is now real and working**, confirmed from a live
capture: `POST {BASE_URL}/AddToStandbyList` with a `Customer` object (see
`resdiary.build_customer()`). This is wired up as its own command:

```bash
python bot.py standby --date 2026-07-27 --time 19:15 --yes
```

Note the `--yes` flag: unlike `availability`, this **is not read-only** -
it creates a real waitlist entry the restaurant will see, the same as
clicking through the site by hand. Only your `contact` details in
`config.yaml` are used (no separate confirmation step), so double check
`--date`/`--time` before adding `--yes`.

There's still no captured request for creating a *confirmed* reservation
(only for the waitlist and for checking availability), since nothing was
open under the "Reservation" availability type at capture time - and it
only ever opens for a few seconds right at midnight, which isn't always a
time you can be at the computer with DevTools open.

### Capturing it without being there: automatic HAR recording

You don't need to manually run DevTools for this last capture. `run`,
`attempt --har`, and `inspect` all launch the browser with Playwright's own
network recorder turned on (`record_har_path`), writing a full HAR of
everything that happens to `captures/`. So the plan for tonight is:

1. Schedule `python bot.py run` as usual (see "Running it unattended"
   below) and go to bed - you don't need to be at the keyboard.
2. Whether it books successfully, fails, or only gets partway, the
   complete network capture for that run is saved to `captures/` **and
   emailed to you as an attachment** alongside the result.
3. Send that HAR back over. If tonight's attempt reached the real submit
   step (even if it then failed for an unrelated reason, e.g. a selector
   being off), it'll contain the real booking-creation request, and the
   bot can be switched to call it directly - no more Playwright/DOM
   dependency for future nights, and much faster/more reliable at the
   critical instant.

This only works if the DOM automation gets far enough to actually click
"submit" tonight, which depends on the CSS selectors below being right.
Calibrating them in advance against the *waitlist* flow (which does have
real open dates right now) won't guarantee the reservation flow's exact
buttons match, but it's the best trial run available before midnight - see
below.

## Before you rely on this: calibrate the browser step

The CSS/text selectors in `SELECTORS` at the top of `bot.py` are best-effort
guesses at a typical booking-widget layout — **not verified against the real
page**. Treat the first run as a calibration step, not a real booking
attempt:

```bash
python bot.py inspect
```

This opens a visible browser on the real booking page and pauses with the
Playwright Inspector (and records a HAR of everything you do, see above).
Click through an actual booking by hand — right now that means the
*waitlist* flow, since it's the one with real open dates — note the real
selectors (right-click → Inspect on each date, time slot, and form field),
and update the `SELECTORS` dict in `bot.py` to match.

Then verify the automated flow gets all the way to the final step without
actually submitting:

```bash
python bot.py attempt --dry-run --headed --har
```

Repeat until this reliably reaches "ready to submit" for a slot that's
actually available on the site right now. Only then trust `python bot.py run`
for the real thing.

## Unlock rule

Confirmed: it's the rolling 28-day window, not a once-a-month release. Every
midnight, the day 28 days out flips from unbookable to bookable; that's the
only date worth trying on any given night, since everything closer in has
already been fought over on previous nights. `booking.target_offset_days`
in the config controls this — bump it if it ever turns out to be off by one,
or if the site changes the window length.

## Closed days

Trippa is currently closed Saturday and Sunday (`booking.closed_weekdays` in
the config). When a night's rollover would unlock a date that falls on a
closed day, `run` skips the wait entirely — no point sitting up for a date
nothing will ever be bookable on — and just emails you a heads-up instead.

To see this in advance for the next several nights (e.g. to know today
whether the date unlocking in 28 days will be a Saturday) run:

```bash
python bot.py check --nights 7
```

This lists, for each of the next 7 midnights, which date it would unlock
and whether Trippa will be open that day.

## Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
playwright install chromium

cp config.example.yaml config.yaml
# edit config.yaml with your real details and preferred times
```

For email notifications, create a [Gmail app password](https://myaccount.google.com/apppasswords)
and export it as the environment variable named in
`notifications.smtp_password_env` (default `TRIPPA_BOT_SMTP_PASSWORD`):

```bash
export TRIPPA_BOT_SMTP_PASSWORD="your-16-char-app-password"
```

## Usage

```bash
python bot.py                                  # interactive mode, asks questions (see above)
python bot.py inspect                          # find real selectors, HAR always recorded
python bot.py availability --date 2026-07-27   # check real availability, no browser
python bot.py standby --date ... --time ... --yes  # really join the waitlist
python bot.py attempt --dry-run --har          # test against an already-open date, without submitting
python bot.py attempt --date 2026-08-20        # test/attempt against a specific date
python bot.py run                              # wait for tonight's rollover, then attempt (always records a HAR)
```

`run` computes tonight's target date (today + `target_offset_days`), sleeps
until `schedule.prewarm_seconds_before` seconds before midnight, loads the
page so it's warm, then busy-waits with sub-second precision until the exact
target instant before trying anything. If none of the preferred times are
available yet, it retries every `schedule.retry_interval_seconds` for up to
`schedule.retry_window_seconds`. The whole session's network traffic is
recorded to `captures/*.har` and emailed to you along with the result -
see "Capturing it without being there" above.

## Running it unattended

Since this needs to fire every single night, schedule `run` once and let it
repeat — don't run it continuously in a loop. Options on your own
machine/server:

**cron:**

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
Description=Run trippa-bot a bit before every midnight rollover

[Timer]
OnCalendar=*-*-* 23:55:00
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
- HAR files in `captures/` and logs in `logs/` (and the emails they get
  attached to) contain your real name, email, and phone number once a
  booking form gets filled in - they're already gitignored, but don't
  paste their contents anywhere public either.
