"""Direct client for Trippa's booking API on booking.resdiary.com.

Reverse-engineered from a HAR capture of the real widget - no login/token
needed, it's the same public JSON API the browser widget itself calls.
Confirmed against a real capture on 2026-07-19:

    POST {BASE_URL}/AvailabilityForDateRange
        {"DateFrom": "...", "DateTo": "...", "PartySize": N,
         "ChannelCode": "INGLESE", "PromotionId": None, "AreaId": None,
         "AvailabilityType": "Reservation"}
        -> {"AvailableDates": [{"Date": "...", "AvailableTimes": [{"TimeSlot": "..."}]}]}
        Empty beyond ~28 days ahead, confirming the rolling window.

    GET {BASE_URL}/AvailabilitySearch
        ?date=YYYY-MM-DD&covers=N&channelCode=INGLESE&areaId=0&availabilityType=Reservation
        -> {"TimeSlots": [{"TimeSlot": "..."}], "Promotions": [...], ...}

    POST {BASE_URL}/StandbyAvailabilityForDateRange
        {"DateFrom": "...", "DateTo": "...", "PartySize": N,
         "ChannelCode": "INGLESE", "PromotionID": None}
        -> {"AvailableDates": [...]}  (the waitlist, separate from Reservation)

CAVEAT - ChannelCode: this was captured with English as the browser's
preferred language ("INGLESE" is Italian for "English"), which suggests
Trippa may split availability by site language/channel. If you normally
book in Italian, capture a HAR with Italian as the primary Accept-Language
and check whether ChannelCode differs - if so, set booking.channel_code in
config.yaml to match.

Booking creation itself (the actual "confirm" call) hasn't been captured
yet, so this client only covers checking availability - bot.py still drives
a real browser via Playwright for the final submit step.
"""
import requests

BASE_URL = "https://booking.resdiary.com/api/Restaurant/TRATTORIATRIPPA"
HEADERS = {
    "accept": "*/*",
    "content-type": "application/json",
    "origin": "https://www.trippamilano.it",
    "referer": "https://www.trippamilano.it/book-a-table-2/",
}
REQUEST_TIMEOUT = 10


def available_times(date_str: str, party_size: int, channel_code: str) -> list:
    """Real available time slots (HH:MM) for a single date and party size."""
    resp = requests.get(
        f"{BASE_URL}/AvailabilitySearch",
        params={
            "date": date_str,
            "covers": party_size,
            "channelCode": channel_code,
            "areaId": 0,
            "availabilityType": "Reservation",
        },
        headers=HEADERS,
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    slots = resp.json().get("TimeSlots", [])
    return [s["TimeSlot"][:5] for s in slots]


def available_dates_in_range(date_from_str: str, date_to_str: str, party_size: int, channel_code: str) -> dict:
    """Dict of {date: [times]} for every bookable date in the range."""
    resp = requests.post(
        f"{BASE_URL}/AvailabilityForDateRange",
        json={
            "DateFrom": f"{date_from_str}T00:00:00",
            "DateTo": f"{date_to_str}T00:00:00",
            "PartySize": party_size,
            "ChannelCode": channel_code,
            "PromotionId": None,
            "AreaId": None,
            "AvailabilityType": "Reservation",
        },
        headers=HEADERS,
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    return {
        d["Date"][:10]: [t["TimeSlot"][:5] for t in d["AvailableTimes"]]
        for d in resp.json().get("AvailableDates", [])
    }


def standby_dates_in_range(date_from_str: str, date_to_str: str, party_size: int, channel_code: str) -> dict:
    """Same shape as available_dates_in_range, but for the waitlist."""
    resp = requests.post(
        f"{BASE_URL}/StandbyAvailabilityForDateRange",
        json={
            "DateFrom": f"{date_from_str}T00:00:00",
            "DateTo": f"{date_to_str}T00:00:00",
            "PartySize": party_size,
            "ChannelCode": channel_code,
            "PromotionID": None,
        },
        headers=HEADERS,
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    return {
        d["Date"][:10]: [t["TimeSlot"][:5] for t in d["AvailableTimes"]]
        for d in resp.json().get("AvailableDates", [])
    }
