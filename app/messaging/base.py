"""Provider-neutral data types shared by every messaging provider.

Campaign and conversation code only ever deals with these types (through
MessageService), so swapping a provider never touches business logic.
"""
import hashlib
import hmac
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


class WebhookVerificationError(Exception):
    """Raised when a webhook request fails signature/token verification."""


@dataclass
class Attachment:
    filename: str
    path: str
    mimetype: str = "application/octet-stream"


@dataclass
class SendResult:
    success: bool
    provider_message_id: Optional[str] = None
    status: str = "sent"
    error: Optional[str] = None
    retryable: bool = False
    raw: Optional[dict] = None


@dataclass
class InboundMessage:
    channel: str
    sender: str
    content: str
    provider_message_id: str
    recipient: Optional[str] = None
    subject: Optional[str] = None
    sender_name: Optional[str] = None
    received_at: Optional[datetime] = None


@dataclass
class StatusUpdate:
    channel: str
    provider_message_id: str
    status: str  # one of MessageStatus: sent, delivered, read, failed
    occurred_at: Optional[datetime] = None
    error: Optional[str] = None


@dataclass
class WebhookResult:
    """What a provider extracted from one webhook call."""

    event_key: str
    event_type: str
    inbound: list = field(default_factory=list)
    statuses: list = field(default_factory=list)


def body_digest(raw: bytes) -> str:
    return hashlib.sha256(raw or b"").hexdigest()


def constant_time_equals(a, b) -> bool:
    if not a or not b:
        return False
    return hmac.compare_digest(str(a).encode(), str(b).encode())


def hmac_sha256_hex(secret: str, raw: bytes) -> str:
    return hmac.new(secret.encode(), raw or b"", hashlib.sha256).hexdigest()
