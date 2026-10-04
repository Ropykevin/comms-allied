from datetime import timedelta

from app.messaging.base import InboundMessage
from app.messaging.service import message_service
from app.models import AuditLog, Conversation, ConversationNote, Message
from app.utils import utcnow
from tests.helpers import make_client


def _incoming(db, client, channel="whatsapp", text="Can I pay on Monday?", pid="in-1", subject=None):
    msg = message_service.handle_inbound(InboundMessage(
        channel=channel, sender=client.phone if channel != "email" else client.email, content=text,
        provider_message_id=pid, subject=subject,
    ))
    db.session.commit()
    return msg.conversation


def test_inbox_lists_incoming_messages(login_as, db):
    john = make_client("John Kamau", "+254712345678")
    mary = make_client("Mary Wanjiku", "+254722222222", "mary@example.com")
    _incoming(db, john, "whatsapp", "Can I pay on Monday?", "w1")
    _incoming(db, mary, "sms", "Thank you.", "s1")
    page = login_as("support").get("/inbox/").get_data(as_text=True)
    assert "John Kamau" in page and "Can I pay on Monday?" in page
    assert "Mary Wanjiku" in page and "Thank you." in page


def test_open_conversation_marks_read(login_as, db):
    john = make_client()
    conv = _incoming(db, john)
    assert conv.unread_count == 1
    resp = login_as("support").get(f"/inbox/c/{conv.id}")
    assert resp.status_code == 200 and b"Can I pay on Monday?" in resp.data
    db.session.refresh(conv)
    assert conv.unread_count == 0


def test_agent_reply_sends_on_same_channel(login_as, db, users):
    john = make_client()
    conv = _incoming(db, john)
    resp = login_as("support").post(f"/inbox/c/{conv.id}/reply", data={"content": "Yes, Monday is fine."})
    assert resp.status_code == 302
    reply = Message.query.filter_by(direction="outbound").one()
    assert reply.channel == "whatsapp" and reply.recipient == "+254712345678"
    assert reply.status == "sent" and reply.sent_by_id == users["support"].id
    db.session.refresh(conv)
    assert conv.assigned_to_id == users["support"].id  # auto-assigned to whoever replied
    assert AuditLog.query.filter_by(action="agent.replied").count() == 1


def test_email_reply_gets_re_subject(login_as, db):
    abc = make_client("ABC Ltd", "+254700111222", "accounts@abc.co.ke")
    conv = _incoming(db, abc, "email", "Please send the invoice.", "<e1@abc>", subject="Invoice")
    login_as("support").post(f"/inbox/c/{conv.id}/reply", data={"content": "Attached."})
    assert Message.query.filter_by(direction="outbound").one().subject == "Re: Invoice"


def test_reply_and_resolve(login_as, db):
    conv = _incoming(db, make_client())
    login_as("support").post(f"/inbox/c/{conv.id}/reply", data={"content": "Done", "then_status": "resolved"})
    db.session.refresh(conv)
    assert conv.status == "resolved"


def test_resolved_conversation_reopens_on_new_message(db):
    john = make_client()
    conv = _incoming(db, john, pid="a")
    conv.status = "resolved"
    db.session.commit()
    _incoming(db, john, text="One more question", pid="b")
    db.session.refresh(conv)
    assert conv.status == "open"


def test_cannot_reply_to_opted_out_client(login_as, db):
    john = make_client()
    conv = _incoming(db, john, "sms", "STOP", "stop-1")
    login_as("support").post(f"/inbox/c/{conv.id}/reply", data={"content": "Sorry to see you go"})
    assert Message.query.filter_by(direction="outbound").count() == 0


def test_conversation_status_changes(login_as, db):
    conv = _incoming(db, make_client())
    c = login_as("support")
    for status in ("pending", "resolved", "closed", "open"):
        c.post(f"/inbox/c/{conv.id}/status", data={"status": status})
        db.session.refresh(conv)
        assert conv.status == status
    assert c.post(f"/inbox/c/{conv.id}/status", data={"status": "bogus"}).status_code == 400


def test_agent_assignment(login_as, db, users):
    conv = _incoming(db, make_client())
    c = login_as("admin")
    c.post(f"/inbox/c/{conv.id}/assign", data={"user_id": users["support"].id})
    db.session.refresh(conv)
    assert conv.assigned_to_id == users["support"].id
    # Viewers can't reply, so they can't be assigned conversations.
    assert c.post(f"/inbox/c/{conv.id}/assign", data={"user_id": users["viewer"].id}).status_code == 400
    c.post(f"/inbox/c/{conv.id}/assign", data={"user_id": ""})
    db.session.refresh(conv)
    assert conv.assigned_to_id is None
    mine = login_as("support")
    mine.post(f"/inbox/c/{conv.id}/assign", data={"user_id": users["support"].id})
    assert b"John Kamau" in mine.get("/inbox/?view=mine").data


def test_internal_note_never_sent(login_as, db):
    conv = _incoming(db, make_client())
    login_as("support").post(f"/inbox/c/{conv.id}/note", data={"content": "Client prefers M-Pesa."})
    assert ConversationNote.query.one().content == "Client prefers M-Pesa."
    assert Message.query.filter_by(direction="outbound").count() == 0


def test_viewer_is_read_only(login_as, db):
    conv = _incoming(db, make_client())
    c = login_as("viewer")
    assert c.get(f"/inbox/c/{conv.id}").status_code == 200
    assert c.post(f"/inbox/c/{conv.id}/reply", data={"content": "x"}).status_code == 403
    assert c.post(f"/inbox/c/{conv.id}/status", data={"status": "closed"}).status_code == 403
    assert c.post(f"/inbox/c/{conv.id}/note", data={"content": "x"}).status_code == 403


def test_whatsapp_24h_window_warning(login_as, db):
    conv = _incoming(db, make_client())
    msg = conv.messages.one()
    msg.created_at = utcnow() - timedelta(hours=30)
    db.session.commit()
    assert b"More than 24 hours" in login_as("support").get(f"/inbox/c/{conv.id}").data


def test_client_profile_shows_all_channel_conversations(login_as, db):
    john = make_client()
    _incoming(db, john, "whatsapp", "WA hello", "w1")
    _incoming(db, john, "sms", "SMS hello", "s1")
    _incoming(db, john, "email", "Email hello", "e1", subject="Hello")
    assert Conversation.query.filter_by(client_id=john.id).count() == 3
    page = login_as("viewer").get(f"/clients/{john.id}").get_data(as_text=True)
    assert "WA hello" in page and "SMS hello" in page and "Hello" in page


def test_start_conversation_from_profile(login_as, db):
    john = make_client()
    resp = login_as("support").post(f"/inbox/start/{john.id}/email")
    assert resp.status_code == 302
    assert Conversation.query.filter_by(client_id=john.id, channel="email").count() == 1


def test_dashboard_shows_recent_activity(login_as, db):
    john = make_client(categories=["VIP Clients"])
    _incoming(db, john)
    page = login_as("viewer").get("/dashboard/").get_data(as_text=True)
    assert "John Kamau" in page and "VIP Clients" in page and "Recent conversations" in page
