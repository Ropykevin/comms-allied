"""Provider abstraction, MessageService and delivery tracking."""
from unittest.mock import MagicMock, patch

from app.campaigns.services import queue_campaign
from app.messaging.base import SendResult, StatusUpdate
from app.messaging.email import ConsoleEmailProvider, SMTPEmailProvider
from app.messaging.service import message_service
from app.messaging.sms import AfricasTalkingSMSProvider, ConsoleSMSProvider
from app.messaging.whatsapp import ConsoleWhatsAppProvider, MetaWhatsAppProvider
from app.models import Campaign, CampaignStatus, Message, MessageStatus
from tests.helpers import category, make_client


class FakeProvider:
    """Records sends and returns scripted results; stands in for any channel."""

    name = "fake"

    def __init__(self, results=None):
        self.results = list(results or [])
        self.sent = []

    def _next(self, payload):
        self.sent.append(payload)
        if self.results:
            return self.results.pop(0)
        return SendResult(success=True, provider_message_id=f"fake-{len(self.sent)}")

    def send_message(self, to, body):
        return self._next((to, body))

    def send_template(self, to, name, language="en", params=None):
        return self._next((to, name, params))

    def send_email(self, email):
        return self._next(email)


def _campaign(db, channel, content="Hello {{first_name}}", subject=None, **kw):
    campaign = Campaign(name="Test", channel=channel, content=content, subject=subject,
                        audience={"category_ids": [category("Safari Clients").id]}, status=CampaignStatus.DRAFT, **kw)
    db.session.add(campaign)
    db.session.commit()
    return campaign


def test_provider_selection_from_config(app):
    assert isinstance(message_service.provider_for("sms"), ConsoleSMSProvider)
    assert isinstance(message_service.provider_for("whatsapp"), ConsoleWhatsAppProvider)
    assert isinstance(message_service.provider_for("email"), ConsoleEmailProvider)
    app.config.update(SMS_PROVIDER="africastalking", WHATSAPP_PROVIDER="meta", MAIL_PROVIDER="smtp")
    try:
        assert isinstance(message_service.provider_for("sms"), AfricasTalkingSMSProvider)
        assert isinstance(message_service.provider_for("whatsapp"), MetaWhatsAppProvider)
        assert isinstance(message_service.provider_for("email"), SMTPEmailProvider)
    finally:
        app.config.update(SMS_PROVIDER="console", WHATSAPP_PROVIDER="console", MAIL_PROVIDER="console")


def test_sms_campaign_through_message_service(db):
    make_client(categories=["Safari Clients"])
    fake = FakeProvider()
    message_service.set_provider("sms", fake)
    campaign = _campaign(db, "sms")
    assert queue_campaign(campaign)
    assert fake.sent == [("+254712345678", "Hello John")]
    msg = Message.query.one()
    assert msg.status == MessageStatus.SENT and msg.provider_message_id == "fake-1" and msg.provider == "fake"


def test_email_campaign_renders_branded_html_with_unsubscribe(db):
    make_client(categories=["Safari Clients"])
    fake = FakeProvider()
    message_service.set_provider("email", fake)
    campaign = _campaign(db, "email", content="Hi {{first_name}}\nSee you!", subject="Offer for {{first_name}}")
    queue_campaign(campaign)
    email = fake.sent[0]
    assert email.to == "john@example.com"
    assert email.subject == "Offer for John"
    assert "Hi John<br>See you!" in email.html
    assert "/unsubscribe/" in email.html and "/t/o/" in email.html
    assert "Unsubscribe:" in email.text


def test_email_attachment_passed_to_provider(db, app, tmp_path):
    make_client(categories=["Safari Clients"])
    (tmp_path / "attachments").mkdir()
    (tmp_path / "attachments" / "brochure.pdf").write_bytes(b"%PDF-1.4")
    fake = FakeProvider()
    message_service.set_provider("email", fake)
    campaign = _campaign(db, "email", subject="Brochure", attachment_path="attachments/brochure.pdf",
                         attachment_name="Safari Brochure.pdf")
    queue_campaign(campaign)
    assert [a.filename for a in fake.sent[0].attachments] == ["Safari Brochure.pdf"]


def test_whatsapp_uses_approved_template_when_configured(db):
    from app.models import MessageTemplate
    make_client(categories=["Safari Clients"])
    template = MessageTemplate(name="Approved", channel="whatsapp", content="Hi {{first_name}} from {{company}}",
                               provider_template_name="safari_promo_v1")
    db.session.add(template)
    db.session.commit()
    fake = FakeProvider()
    message_service.set_provider("whatsapp", fake)
    campaign = _campaign(db, "whatsapp", content=template.content, template_id=template.id)
    queue_campaign(campaign)
    assert fake.sent == [("+254712345678", "safari_promo_v1", ["John", "ABC Ltd"])]


def test_permanent_failure_marks_failed_and_audits(db):
    from app.models import AuditLog
    make_client(categories=["Safari Clients"])
    message_service.set_provider("sms", FakeProvider([SendResult(success=False, error="Invalid number")]))
    campaign = _campaign(db, "sms")
    queue_campaign(campaign)
    msg = Message.query.one()
    assert msg.status == MessageStatus.FAILED and msg.error_message == "Invalid number" and msg.failed_at
    db.session.refresh(campaign)
    assert campaign.status == CampaignStatus.FAILED  # every message failed
    assert AuditLog.query.filter_by(action="message.failed").count() == 1


def test_transient_failure_is_retried(db):
    make_client(categories=["Safari Clients"])
    fake = FakeProvider([SendResult(success=False, error="timeout", retryable=True),
                         SendResult(success=True, provider_message_id="ok-2")])
    message_service.set_provider("sms", fake)
    campaign = _campaign(db, "sms")
    queue_campaign(campaign)
    msg = Message.query.one()
    assert len(fake.sent) == 2
    assert msg.status == MessageStatus.SENT and msg.retry_count == 1 and msg.provider_message_id == "ok-2"
    db.session.refresh(campaign)
    assert campaign.status == CampaignStatus.COMPLETED


def test_manual_retry_of_failed_messages(login_as, db):
    make_client(categories=["Safari Clients"])
    message_service.set_provider("sms", FakeProvider([SendResult(success=False, error="No credit")]))
    campaign = _campaign(db, "sms")
    queue_campaign(campaign)
    assert Message.query.one().status == MessageStatus.FAILED
    message_service.set_provider("sms", FakeProvider())
    login_as("marketing").post(f"/campaigns/{campaign.id}/retry")
    msg = Message.query.one()
    assert msg.status == MessageStatus.SENT
    db.session.refresh(campaign)
    assert campaign.status == CampaignStatus.COMPLETED


def test_status_lifecycle_moves_forward_only(db):
    msg = _sent_message(db)
    apply = message_service.apply_status
    assert apply(msg, "delivered")
    assert msg.status == "delivered" and msg.delivered_at
    assert apply(msg, "read") and msg.status == "read" and msg.read_at
    assert apply(msg, "sent") is None and msg.status == "read"  # out-of-order webhook ignored
    assert apply(msg, "failed") is None and msg.status == "read"  # stale failure ignored


def test_read_before_delivered_backfills_delivered_at(db):
    msg = _sent_message(db)
    message_service.apply_status(msg, "read")
    assert msg.status == "read" and msg.delivered_at and msg.read_at


def test_failed_after_sent(db):
    msg = _sent_message(db)
    message_service.handle_status(StatusUpdate("sms", msg.provider_message_id, "failed", error="Rejected"))
    assert msg.status == "failed" and msg.error_message == "Rejected"


def _sent_message(db):
    make_client(categories=["Safari Clients"])
    message_service.set_provider("sms", FakeProvider())
    queue_campaign(_campaign(db, "sms"))
    return Message.query.one()


def test_africastalking_send_parses_response(app):
    provider = AfricasTalkingSMSProvider({**app.config, "SMS_API_KEY": "k", "SMS_USERNAME": "allied"})
    resp = MagicMock(status_code=201)
    resp.json.return_value = {"SMSMessageData": {"Recipients": [
        {"statusCode": 101, "number": "+254712345678", "status": "Success", "messageId": "ATXid_1"}]}}
    with patch("app.messaging.sms.requests.post", return_value=resp) as post:
        result = provider.send_message("+254712345678", "Hello")
    assert result.success and result.provider_message_id == "ATXid_1"
    assert post.call_args.kwargs["headers"]["apiKey"] == "k"


def test_africastalking_missing_credentials(app):
    result = AfricasTalkingSMSProvider({**app.config, "SMS_API_KEY": "", "SMS_USERNAME": ""}).send_message("+1", "x")
    assert not result.success and "credentials" in result.error


def test_meta_whatsapp_send_and_retryable_errors(app):
    cfg = {**app.config, "WHATSAPP_API_KEY": "token", "WHATSAPP_PHONE_NUMBER_ID": "123"}
    provider = MetaWhatsAppProvider(cfg)
    ok = MagicMock(ok=True, status_code=200)
    ok.json.return_value = {"messages": [{"id": "wamid.ABC"}]}
    with patch("app.messaging.whatsapp.requests.post", return_value=ok) as post:
        result = provider.send_message("+254712345678", "Hi")
    assert result.success and result.provider_message_id == "wamid.ABC"
    assert post.call_args.kwargs["json"]["to"] == "254712345678"
    assert "graph.facebook.com" in post.call_args.args[0] and "/123/messages" in post.call_args.args[0]

    throttled = MagicMock(ok=False, status_code=400)
    throttled.json.return_value = {"error": {"message": "Rate limit", "code": 130429}}
    with patch("app.messaging.whatsapp.requests.post", return_value=throttled):
        result = provider.send_message("+254712345678", "Hi")
    assert not result.success and result.retryable


def test_no_credentials_in_source():
    import pathlib
    for path in pathlib.Path("app").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "EAAG" not in text and "sk_live" not in text, path
