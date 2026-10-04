from datetime import timedelta

from app import audit
from app.extensions import db
from app.messaging.service import message_service
from app.models import (
    CHANNEL_EMAIL,
    CHANNEL_WHATSAPP,
    DIRECTION_INBOUND,
    DIRECTION_OUTBOUND,
    Message,
    MessageStatus,
)
from app.utils import truncate, utcnow

WHATSAPP_SESSION_WINDOW = timedelta(hours=24)


class ReplyError(ValueError):
    pass


def last_inbound_at(conversation):
    msg = (
        conversation.messages.filter(Message.direction == DIRECTION_INBOUND)
        .order_by(Message.created_at.desc()).first()
    )
    return msg.created_at if msg else None


def whatsapp_window_open(conversation):
    """WhatsApp only allows free-form replies within 24h of the client's last message."""
    if conversation.channel != CHANNEL_WHATSAPP:
        return True
    last = last_inbound_at(conversation)
    return bool(last and utcnow() - last <= WHATSAPP_SESSION_WINDOW)


def create_reply(conversation, user, content, subject=None):
    content = (content or "").strip()
    if not content:
        raise ReplyError("Write a reply first.")
    client = conversation.client
    cc = client.channel(conversation.channel)
    if cc is None or not cc.address:
        raise ReplyError(f"{client.full_name} has no {conversation.channel_label} contact details.")
    if not cc.is_allowed:
        raise ReplyError(f"{client.full_name} has opted out of {conversation.channel_label}.")

    if conversation.channel == CHANNEL_EMAIL:
        subject = (subject or "").strip() or _reply_subject(conversation.subject)
    else:
        subject = None

    message = Message(
        conversation=conversation,
        channel=conversation.channel,
        direction=DIRECTION_OUTBOUND,
        recipient=cc.address,
        subject=subject,
        content=content,
        status=MessageStatus.QUEUED,
        sent_by_id=user.id,
    )
    db.session.add(message)
    conversation.last_message_at = utcnow()
    conversation.last_message_preview = truncate(content, 200)
    conversation.last_message_direction = DIRECTION_OUTBOUND
    conversation.unread_count = 0
    if conversation.assigned_to_id is None:
        conversation.assigned_to_id = user.id
    db.session.flush()
    audit.record("agent.replied", "conversation", conversation.id, {"message_id": message.id}, user=user)
    return message


def _reply_subject(subject):
    subject = subject or "Allied Tours & Travel"
    return subject if subject.lower().startswith("re:") else f"Re: {subject}"


def deliver_message(message_id):
    """Background job: send a single queued message (agent replies)."""
    message = db.session.get(Message, message_id)
    if message is None or message.status != MessageStatus.QUEUED:
        return
    message_service.send(message)
    db.session.commit()


def timeline(conversation):
    """Messages and internal notes merged in chronological order."""
    items = [("message", m.created_at, m) for m in conversation.messages.all()]
    items += [("note", n.created_at, n) for n in conversation.notes]
    return sorted(items, key=lambda item: item[1])
