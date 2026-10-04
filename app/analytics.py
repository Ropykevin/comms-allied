from sqlalchemy import func

from app.extensions import db
from app.models import (
    CHANNELS,
    DIRECTION_INBOUND,
    DIRECTION_OUTBOUND,
    CampaignRecipient,
    Message,
    MessageStatus,
)


def message_stats(campaign_id=None):
    """Outbound message counts by delivery stage (optionally for one campaign).

    Stages are cumulative: a read message also counts as delivered and sent.
    """
    q = db.session.query(Message.status, func.count(Message.id)).filter(Message.direction == DIRECTION_OUTBOUND)
    if campaign_id is not None:
        q = q.filter(Message.campaign_id == campaign_id)
    counts = dict(q.group_by(Message.status).all())
    read = counts.get(MessageStatus.READ, 0)
    delivered = counts.get(MessageStatus.DELIVERED, 0) + read
    sent = delivered + counts.get(MessageStatus.SENT, 0)
    return {
        "total": sum(counts.values()),
        "sent": sent,
        "delivered": delivered,
        "read": read,
        "failed": counts.get(MessageStatus.FAILED, 0),
        "pending": counts.get(MessageStatus.QUEUED, 0) + counts.get(MessageStatus.SENDING, 0),
    }


def channel_stats():
    out = {ch: {"sent": 0, "delivered": 0, "failed": 0, "inbound": 0} for ch in CHANNELS}
    rows = (
        db.session.query(Message.channel, Message.direction, Message.status, func.count(Message.id))
        .group_by(Message.channel, Message.direction, Message.status)
        .all()
    )
    for channel, direction, status, n in rows:
        bucket = out.setdefault(channel, {"sent": 0, "delivered": 0, "failed": 0, "inbound": 0})
        if direction != DIRECTION_OUTBOUND:
            bucket["inbound"] += n
            continue
        if status in (MessageStatus.SENT, MessageStatus.DELIVERED, MessageStatus.READ):
            bucket["sent"] += n
        if status in (MessageStatus.DELIVERED, MessageStatus.READ):
            bucket["delivered"] += n
        if status == MessageStatus.FAILED:
            bucket["failed"] += n
    return out


def campaign_analytics(campaign):
    stats = message_stats(campaign.id)
    replies = (
        db.session.query(func.count(Message.id), func.count(func.distinct(Message.conversation_id)))
        .filter(Message.campaign_id == campaign.id, Message.direction == DIRECTION_INBOUND)
        .one()
    )
    recipient_counts = dict(
        db.session.query(CampaignRecipient.status, func.count(CampaignRecipient.id))
        .filter(CampaignRecipient.campaign_id == campaign.id)
        .group_by(CampaignRecipient.status)
        .all()
    )
    bounced = (
        db.session.query(func.count(Message.id))
        .filter(Message.campaign_id == campaign.id, Message.status == MessageStatus.FAILED,
                Message.error_message.ilike("%bounce%"))
        .scalar()
    )
    stats.update(
        recipients=campaign.total_recipients,
        replies=replies[0],
        repliers=replies[1],
        skipped=recipient_counts.get(CampaignRecipient.STATUS_SKIPPED, 0),
        waiting=recipient_counts.get(CampaignRecipient.STATUS_PENDING, 0),
        bounced=bounced,
    )
    stats["pending"] += stats["waiting"]
    return stats
