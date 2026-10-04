"""Time-based one-time passwords (RFC 6238) for authenticator apps."""
import hmac
import time

import pyotp
import segno
from flask import current_app

STEP_SECONDS = 30


def new_secret():
    return pyotp.random_base32()


def provisioning_uri(secret, email):
    return pyotp.TOTP(secret).provisioning_uri(name=email, issuer_name=current_app.config["MFA_ISSUER"])


def qr_svg_data_uri(uri):
    return segno.make(uri, error="m").svg_data_uri(scale=5, border=2)


def verify(secret, code, last_step=None, now=None):
    """Return the matched time-step, or None. Accepts one step of clock drift and rejects replays."""
    code = (code or "").replace(" ", "").strip()
    if not secret or len(code) != 6 or not code.isdigit():
        return None
    totp = pyotp.TOTP(secret)
    current = int((now if now is not None else time.time()) // STEP_SECONDS)
    for step in (current - 1, current, current + 1):
        if last_step is not None and step <= last_step:
            continue
        if hmac.compare_digest(totp.generate_otp(step), code):
            return step
    return None
