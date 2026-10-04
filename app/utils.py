from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import phonenumbers
from flask import current_app, request


def utcnow():
    """Naive UTC timestamp. All datetimes are stored as naive UTC."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def app_timezone():
    return ZoneInfo(current_app.config.get("DEFAULT_TIMEZONE", "Africa/Nairobi"))


def to_local(dt, tz_name=None):
    if dt is None:
        return None
    tz = ZoneInfo(tz_name) if tz_name else app_timezone()
    return dt.replace(tzinfo=timezone.utc).astimezone(tz)


def local_to_utc(dt, tz_name):
    """Convert a naive local datetime in tz_name to naive UTC."""
    aware = dt.replace(tzinfo=ZoneInfo(tz_name))
    return aware.astimezone(timezone.utc).replace(tzinfo=None)


def normalize_phone(value, region=None):
    """Return the phone number in E.164 format, or None if it is not valid."""
    if not value:
        return None
    value = str(value).strip()
    if not value:
        return None
    region = region or current_app.config.get("DEFAULT_PHONE_REGION", "KE")
    try:
        parsed = phonenumbers.parse(value, region)
    except phonenumbers.NumberParseException:
        return None
    if not phonenumbers.is_valid_number(parsed):
        return None
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


def normalize_email(value):
    if not value:
        return None
    value = str(value).strip().lower()
    return value or None


def client_ip():
    if request:
        return request.remote_addr
    return None


def truncate(text, length=80):
    if not text:
        return ""
    text = " ".join(str(text).split())
    return text if len(text) <= length else text[: length - 1] + "…"
