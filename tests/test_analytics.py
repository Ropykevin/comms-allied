from app.analytics import campaign_analytics, channel_stats, message_stats
from app.dashboard.routes import client_stats
from app.extensions import db as _db
from app.messaging.service import message_service
from app.models import DIRECTION_INBOUND, DIRECTION_OUTBOUND, Campaign, Message, MessageStatus
from tests.helpers import category, make_client


def _message(client, channel, status, direction=DIRECTION_OUTBOUND, campaign_id=None, error=None):
    conversation = message_service.get_or_create_conversation(client, channel)
    msg = Message(conversation=conversation, channel=channel, direction=direction, status=status,
                  campaign_id=campaign_id, error_message=error)
    _db.session.add(msg)
    _db.session.flush()
    return msg


def test_message_stats_are_cumulative(db):
    john = make_client("John Kamau", "+254712345678", "john@example.com")
    for status in (MessageStatus.SENT, MessageStatus.DELIVERED, MessageStatus.READ, MessageStatus.FAILED,
                   MessageStatus.QUEUED):
        _message(john, "sms", status)
    _message(john, "sms", MessageStatus.RECEIVED, direction=DIRECTION_INBOUND)
    db.session.commit()

    stats = message_stats()
    assert stats == {"total": 5, "sent": 3, "delivered": 2, "read": 1, "failed": 1, "pending": 1}


def test_channel_stats_split_by_channel_and_direction(db):
    john = make_client("John Kamau", "+254712345678", "john@example.com")
    _message(john, "whatsapp", MessageStatus.READ)
    _message(john, "whatsapp", MessageStatus.RECEIVED, direction=DIRECTION_INBOUND)
    _message(john, "email", MessageStatus.FAILED)
    db.session.commit()

    stats = channel_stats()
    assert stats["whatsapp"] == {"sent": 1, "delivered": 1, "failed": 0, "inbound": 1}
    assert stats["email"] == {"sent": 0, "delivered": 0, "failed": 1, "inbound": 0}
    assert stats["sms"] == {"sent": 0, "delivered": 0, "failed": 0, "inbound": 0}


def test_client_stats_exclude_archived(db):
    make_client("John Kamau", "+254712345678", None)
    make_client("Grace Njeri", "+254744444444", None, status="prospect")
    make_client("Old Client", "+254755555555", None, status="archived")
    stats = client_stats()
    assert stats["total"] == 2 and stats["active"] == 1 and stats["prospect"] == 1 and stats["new"] == 2


def test_campaign_analytics_and_stats_endpoint(login_as, db):
    make_client("John Kamau", "+254712345678", "john@example.com", categories=["Safari Clients"])
    make_client("Mary Wanjiku", "+254722222222", "mary@example.com", categories=["Safari Clients"])
    c = login_as("marketing")
    c.post("/campaigns/create", data={
        "name": "Safari", "channel": "email", "template_id": "0", "subject": "Hi {{first_name}}",
        "content": "Safari time", "timezone": "Africa/Nairobi", "action": "send_now",
        "category_ids": [category("Safari Clients").id],
    })
    campaign = Campaign.query.one()
    first, second = Message.query.filter_by(campaign_id=campaign.id).order_by(Message.id).all()
    message_service.apply_status(first, MessageStatus.READ)
    message_service.apply_status(second, MessageStatus.FAILED, error="Hard bounce: mailbox unavailable")
    _message(first.conversation.client, "email", MessageStatus.RECEIVED, direction=DIRECTION_INBOUND,
             campaign_id=campaign.id)
    db.session.commit()

    stats = campaign_analytics(campaign)
    assert stats["recipients"] == 2
    assert stats["read"] == 1 and stats["delivered"] == 1 and stats["failed"] == 1 and stats["bounced"] == 1
    assert stats["replies"] == 1 and stats["repliers"] == 1

    resp = login_as("viewer").get(f"/campaigns/{campaign.id}/stats.json")
    assert resp.status_code == 200
    assert resp.json["stats"]["read"] == 1
    assert login_as("support").get(f"/campaigns/{campaign.id}/stats.json").status_code == 403


def test_dashboard_renders_for_every_role(login_as):
    make_client("John Kamau", "+254712345678", "john@example.com", categories=["VIP Clients"])
    for role in ("super", "admin", "marketing", "support", "viewer"):
        resp = login_as(role).get("/dashboard/")
        assert resp.status_code == 200, role
        assert b"VIP" in resp.data
