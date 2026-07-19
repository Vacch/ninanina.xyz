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

    GET {BASE_URL}/Setup?date=YYYY-MM-DD&channelCode=INGLESE
        -> venue config: MinOnlinePartySize/MaxOnlinePartySize, Services
        (opening hours per service), ReservationPhoneNumber, T&Cs, etc.

    POST {BASE_URL}/AddToStandbyList  -- CONFIRMED WORKING, joins the real
    waitlist (this is not a dry endpoint - it creates an actual entry):
        {"VisitDate": "2026-07-30T00:00:00", "VisitTime": "19:15:00",
         "PartySize": 2, "ChannelCode": "INGLESE", "SpecialRequests": "",
         "Customer": {"FirstName": ..., "Surname": ..., "Mobile": ...,
                       "Email": ..., "ReceiveEmailMarketing": False, ...
                       marketing opt-in flags/texts, see build_customer()},
         "IsLeaveTimeConfirmed": true, "PaymentMethodId": None,
         "PaymentIntentId": None, "SetupIntentId": None,
         "ConfirmationTokenId": None, "Households": None}
        -> {"Booking": {..., "BookingStatus": "Wait", "Reference": "..."},
            "Status": "Success", "Errors": None}

CAVEAT - ChannelCode: this was captured with English as the browser's
preferred language ("INGLESE" is Italian for "English"), which suggests
Trippa may split availability by site language/channel. If you normally
book in Italian, capture a HAR with Italian as the primary Accept-Language
and check whether ChannelCode differs - if so, set booking.channel_code in
config.yaml to match.

CAVEAT - real bookings: the endpoint for creating a *confirmed* reservation
(as opposed to a standby/waitlist entry) still hasn't been captured, since
nothing was open in the "Reservation" availability type at capture time.
The payload shape is almost certainly the same Customer object seen in
AddToStandbyList, just posted to a different, not-yet-observed endpoint -
next time AvailabilityForDateRange/AvailabilitySearch show a real
Reservation-type slot, capture one more HAR of completing that booking so
bot.py can stop depending on Playwright for the final submit entirely.
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

# Static boilerplate consent text the widget sends in every Customer
# object, captured verbatim from a real request (not user data - the
# server just echoes this back if marketing opt-in were ever true).
_GROUP_EMAIL_OPT_IN_TEXT = "I would like to receive news and offers from <strong>TRIPPA srl</strong> by:"
_GROUP_SMS_OPT_IN_TEXT = "I would like to receive news and offers from <strong>TRIPPA srl</strong> by:"
_RESTAURANT_EMAIL_OPT_IN_TEXT = (
    "I would like to receive news and offers from <strong>TRATTORIA TRIPPA</strong> by:"
)
_RESTAURANT_SMS_OPT_IN_TEXT = (
    "I would like to receive news and offers from <strong>TRATTORIA TRIPPA</strong> by:"
)


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


def get_setup(date_str: str, channel_code: str) -> dict:
    """Venue config for a given date: party size limits, service hours,
    reservation phone number, T&Cs, etc."""
    resp = requests.get(
        f"{BASE_URL}/Setup",
        params={"date": date_str, "channelCode": channel_code},
        headers=HEADERS,
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def build_customer(contact: dict) -> dict:
    """Build the Customer object AddToStandbyList (and presumably a real
    booking endpoint) expects, from the simplified `contact` section of
    config.yaml."""
    return {
        "FirstName": contact["first_name"],
        "Surname": contact["surname"],
        "Mobile": contact["mobile"],
        "MobileCountryCode": contact.get("mobile_country_code", "39"),
        "PhoneCountryCode": contact.get("mobile_country_code", "39"),
        "Email": contact["email"],
        "ReceiveEmailMarketing": False,
        "ReceiveSmsMarketing": False,
        "ReceiveResDiaryEmailMarketing": False,
        "ReceiveResDiarySmsMarketing": False,
        "ReceiveRestaurantEmailMarketing": False,
        "ReceiveRestaurantSmsMarketing": False,
        "GroupEmailMarketingOptInText": _GROUP_EMAIL_OPT_IN_TEXT,
        "GroupSmsMarketingOptInText": _GROUP_SMS_OPT_IN_TEXT,
        "RestaurantEmailMarketingOptInText": _RESTAURANT_EMAIL_OPT_IN_TEXT,
        "RestaurantSmsMarketingOptInText": _RESTAURANT_SMS_OPT_IN_TEXT,
    }


def add_to_standby_list(
    visit_date_str: str, visit_time_str: str, party_size: int, channel_code: str, customer: dict,
    special_requests: str = "",
) -> dict:
    """CONFIRMED WORKING (2026-07-19): joins Trippa's real waitlist for a
    date/time that already showed up in standby_dates_in_range(). This is
    not a dry run - it creates an actual entry the restaurant will see."""
    resp = requests.post(
        f"{BASE_URL}/AddToStandbyList",
        json={
            "VisitDate": f"{visit_date_str}T00:00:00",
            "VisitTime": f"{visit_time_str}:00" if visit_time_str.count(":") == 1 else visit_time_str,
            "PartySize": party_size,
            "ChannelCode": channel_code,
            "SpecialRequests": special_requests,
            "Customer": customer,
            "IsLeaveTimeConfirmed": True,
            "PaymentMethodId": None,
            "PaymentIntentId": None,
            "SetupIntentId": None,
            "ConfirmationTokenId": None,
            "Households": None,
        },
        headers=HEADERS,
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()
