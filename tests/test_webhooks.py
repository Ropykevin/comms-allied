import hashlib
import hmac
import json

from app.campaigns.services import queue_campaign
from app.models import Campaign, CampaignStatus, Client, Conversation, Message, WebhookEvent
from tests.helpers import category, make_client

SMS_URL = "/webhooks/sms?token=sms-test-token"


def _wa_post(client, payload, secret="wa-test-secret"):
    body = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return client.post("/webhooks/whatsapp", data=body, content_type="application/json",
                       headers={"X-Hub-Signature-256": sig})


def _email_post(client, payload, secret="email-test-secret"):
    body = json.dumps(payload).encode()
    sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return client.post("/webhooks/email", data=body, content_type="application/json",
                       headers={"X-Allied-Signature": sig})


def _wa_message(wamid, sender="254712345678", text="Can I pay on Monday?"):
    return {"entry": [{"changes": [{"value": {
        "metadata": {"display_phone_number": "254700000000"},
        "contacts": [{"wa_id": sender, "profile": {"name": "John"}}],
        "messages": [{"from": sender, "id": wamid, "timestamp": "1790000000", "type": "text", "text": {"body": text}}],
    }}]}]}


def _wa_status(wamid, status, errors=None):
    st = {"id": wamid, "status": status, "timestamp": "1790000100", "recipient_id": "254712345678"}
    if errors:
        st["errors"] = errors
    return {"entry": [{"changes": [{"value": {"statuses": [st]}}]}]}


# ---- verification ------------------------------------------------------------------

def test_sms_webhook_rejects_bad_token(client, app):
    assert client.post("/webhooks/sms", data={"from": "+254712345678", "text": "hi", "id": "1"}).status_code == 403
    assert client.post("/webhooks/sms?token=wrong", data={"from": "+1", "text": "x", "id": "1"}).status_code == 403
    assert Message.query.count() == 0


def test_whatsapp_webhook_rejects_bad_signature(client, app):
    assert _wa_post(client, _wa_message("wamid.1"), secret="wrong").status_code == 403
    assert client.post("/webhooks/whatsapp", json=_wa_message("wamid.1")).status_code == 403
    assert Message.query.count() == 0


def test_whatsapp_subscription_handshake(client, app):
    ok = client.get("/webhooks/whatsapp?hub.mode=subscribe&hub.verify_token=wa-verify&hub.challenge=12345")
    assert ok.status_code == 200 and ok.data == b"12345"
    bad = client.get("/webhooks/whatsapp?hub.mode=subscribe&hub.verify_token=nope&hub.challenge=1")
    assert bad.status_code == 403


def test_email_webhook_rejects_bad_signature(client, app):
    assert _email_post(client, {"type": "inbound", "from": "a@example.com"}, secret="x").status_code == 403


def test_webhooks_do_not_require_csrf_or_login(client, app):
    # CSRF is enabled in this check to prove webhooks are exempt.
    app.config["WTF_CSRF_ENABLED"] = True
    try:
        resp = client.post(SMS_URL, data={"from": "+254712345678", "text": "Thank you.", "id": "ATXin_1"})
    finally:
        app.config["WTF_CSRF_ENABLED"] = False
    assert resp.status_code == 200


# ---- inbound messages --------------------------------------------------------------

def test_inbound_sms_creates_message_in_sms_conversation(client, db):
    mary = make_client("Mary Wanjiku", "+254722222222", "mary@example.com")
    resp = client.post(SMS_URL, data={"from": "+254722222222", "to": "20880", "text": "Thank you.", "id": "ATXin_9",
                                      "date": "2026-10-03T10:00:00Z"})
    assert resp.json["status"] == "processed"
    conv = Conversation.query.filter_by(client_id=mary.id, channel="sms").one()
    msg = conv.messages.one()
    assert msg.direction == "inbound" and msg.content == "Thank you." and msg.provider_message_id == "ATXin_9"
    assert conv.unread_count == 1 and conv.last_message_preview == "Thank you."


def test_inbound_whatsapp_is_idempotent(client, db):
    make_client("John Kamau", "+254712345678")
    payload = _wa_message("wamid.HBgM")
    assert _wa_post(client, payload).json["status"] == "processed"
    assert _wa_post(client, payload).json["status"] == "duplicate"
    assert Message.query.count() == 1
    assert WebhookEvent.query.count() == 1
    # Same message id inside a different payload (provider re-batching) is still stored only once.
    payload["entry"][0]["changes"][0]["value"]["messages"][0]["timestamp"] = "1790000999"
    _wa_post(client, payload)
    assert Message.query.count() == 1


def test_whatsapp_and_sms_from_same_client_are_separate_threads(client, db):
    john = make_client("John Kamau", "+254712345678")
    _wa_post(client, _wa_message("wamid.1"))
    client.post(SMS_URL, data={"from": "+254712345678", "text": "Hello via SMS", "id": "ATXin_2"})
    channels = sorted(c.channel for c in Conversation.query.filter_by(client_id=john.id))
    assert channels == ["sms", "whatsapp"]


def test_unknown_sender_becomes_prospect(client, db):
    _wa_post(client, _wa_message("wamid.new", sender="254799999999", text="Hi, I want a safari quote"))
    prospect = Client.query.filter_by(phone="+254799999999").one()
    assert prospect.status == "prospect" and prospect.full_name == "John"
    assert prospect.channel("whatsapp").address == "+254799999999"


def test_inbound_email(client, db):
    abc = make_client("ABC Ltd Accounts", "+254700111222", "accounts@abc.co.ke")
    resp = _email_post(client, {"type": "inbound", "id": "<msg-1@abc.co.ke>", "from": "ABC <Accounts@ABC.co.ke>",
                                "subject": "Invoice", "text": "Please send the invoice."})
    assert resp.status_code == 200
    conv = Conversation.query.filter_by(client_id=abc.id, channel="email").one()
    assert conv.subject == "Invoice"
    assert conv.messages.one().content == "Please send the invoice."


def test_stop_keyword_opts_client_out(client, db):
    john = make_client("John Kamau", "+254712345678")
    client.post(SMS_URL, data={"from": "+254712345678", "text": "STOP", "id": "ATXin_stop"})
    db.session.refresh(john)
    assert john.channel("sms").status == "opted_out"
    assert john.channel("whatsapp").status == "allowed"


def test_reply_is_attributed_to_campaign(client, db):
    john = make_client("John Kamau", "+254712345678", categories=["Safari Clients"])
    campaign = Campaign(name="Safari", channel="whatsapp", content="Hi {{first_name}}",
                        audience={"category_ids": [category("Safari Clients").id]}, status=CampaignStatus.DRAFT)
    db.session.add(campaign)
    db.session.commit()
    queue_campaign(campaign)
    _wa_post(client, _wa_message("wamid.reply", text="Interested!"))
    reply = Message.query.filter_by(direction="inbound").one()
    assert reply.campaign_id == campaign.id
    from app.analytics import campaign_analytics
    assert campaign_analytics(campaign)["replies"] == 1
    assert john.id


# ---- delivery status ---------------------------------------------------------------

def _sent_whatsapp(db):
    make_client("John Kamau", "+254712345678", categories=["Safari Clients"])
    campaign = Campaign(name="Safari", channel="whatsapp", content="Hi",
                        audience={"category_ids": [category("Safari Clients").id]}, status=CampaignStatus.DRAFT)
    db.session.add(campaign)
    db.session.commit()
    queue_campaign(campaign)
    return Message.query.one(), campaign


def test_whatsapp_delivery_and_read_status(client, db):
    msg, campaign = _sent_whatsapp(db)
    _wa_post(client, _wa_status(msg.provider_message_id, "delivered"))
    _wa_post(client, _wa_status(msg.provider_message_id, "read"))
    db.session.refresh(msg)
    assert msg.status == "read" and msg.delivered_at and msg.read_at
    # A retried "delivered" webhook must not move it backwards.
    _wa_post(client, _wa_status(msg.provider_message_id, "delivered"))
    db.session.refresh(msg)
    assert msg.status == "read"
    from app.analytics import campaign_analytics
    stats = campaign_analytics(campaign)
    assert stats["delivered"] == 1 and stats["read"] == 1


def test_whatsapp_failed_status(client, db):
    msg, _ = _sent_whatsapp(db)
    _wa_post(client, _wa_status(msg.provider_message_id, "failed", [{"code": 131026, "title": "Undeliverable"}]))
    db.session.refresh(msg)
    assert msg.status == "failed" and msg.error_message == "Undeliverable"


def test_sms_delivery_report(client, db):
    make_client("John Kamau", "+254712345678", categories=["Safari Clients"])
    campaign = Campaign(name="SMS", channel="sms", content="Hi",
                        audience={"category_ids": [category("Safari Clients").id]}, status=CampaignStatus.DRAFT)
    db.session.add(campaign)
    db.session.commit()
    queue_campaign(campaign)
    msg = Message.query.one()
    resp = client.post(SMS_URL, data={"id": msg.provider_message_id, "status": "Success", "phoneNumber": msg.recipient})
    assert resp.json["status"] == "processed"
    db.session.refresh(msg)
    assert msg.status == "delivered"
    assert client.post(SMS_URL, data={"id": msg.provider_message_id, "status": "Success"}).json["status"] == "duplicate"


def test_email_open_and_bounce_events(client, db):
    make_client("John Kamau", "+254712345678", "john@example.com", categories=["Safari Clients"])
    make_client("Jane Doe", "+254711000000", "jane@example.com", categories=["Safari Clients"])
    campaign = Campaign(name="Email", channel="email", subject="Hi", content="Hello",
                        audience={"category_ids": [category("Safari Clients").id]}, status=CampaignStatus.DRAFT)
    db.session.add(campaign)
    db.session.commit()
    queue_campaign(campaign)
    john_msg = Message.query.filter_by(recipient="john@example.com").one()
    jane_msg = Message.query.filter_by(recipient="jane@example.com").one()
    _email_post(client, {"events": [
        {"type": "delivered", "message_id": john_msg.provider_message_id},
        {"type": "opened", "message_id": john_msg.provider_message_id},
        {"type": "bounced", "message_id": jane_msg.provider_message_id, "reason": "Mailbox does not exist (bounce)"},
    ]})
    db.session.refresh(john_msg)
    db.session.refresh(jane_msg)
    assert john_msg.status == "read"
    assert jane_msg.status == "failed"
    from app.analytics import campaign_analytics
    stats = campaign_analytics(campaign)
    assert stats["read"] == 1 and stats["bounced"] == 1


def test_email_tracking_pixel_marks_opened(client, db):
    make_client("John Kamau", "+254712345678", "john@example.com", categories=["Safari Clients"])
    campaign = Campaign(name="Email", channel="email", subject="Hi", content="Hello",
                        audience={"category_ids": [category("Safari Clients").id]}, status=CampaignStatus.DRAFT)
    db.session.add(campaign)
    db.session.commit()
    queue_campaign(campaign)
    msg = Message.query.one()
    from app.messaging.service import tracking_token
    resp = client.get(f"/t/o/{tracking_token(msg.id)}.gif")
    assert resp.status_code == 200 and resp.mimetype == "image/gif"
    db.session.refresh(msg)
    assert msg.status == "read"
    assert client.get("/t/o/forged.gif").status_code == 200  # never errors, just ignored


def test_unsubscribe_link(client, db):
    john = make_client("John Kamau", "+254712345678", "john@example.com")
    from app.messaging.service import unsubscribe_token
    token = unsubscribe_token(john.id)
    assert client.get(f"/unsubscribe/{token}").status_code == 200
    db.session.refresh(john)
    assert john.channel("email").status == "allowed"  # GET alone never unsubscribes
    client.post(f"/unsubscribe/{token}")
    db.session.refresh(john)
    assert john.channel("email").status == "opted_out"
    assert client.get("/unsubscribe/forged").status_code == 404


def test_webhook_events_logged(client, db):
    make_client("John Kamau", "+254712345678")
    client.post(SMS_URL, data={"from": "+254712345678", "text": "hi", "id": "ATXin_log"})
    event = WebhookEvent.query.one()
    assert event.channel == "sms" and event.status == "processed" and event.payload["text"] == "hi"
