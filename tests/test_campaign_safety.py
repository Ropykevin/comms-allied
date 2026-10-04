"""Campaign delivery safety: one worker per campaign, crash recovery, consent on retry."""
from datetime import timedelta

from app.campaigns.services import process_campaign, queue_campaign, retry_failed_messages
from app.messaging.base import SendResult
from app.messaging.service import message_service
from app.models import Campaign, CampaignRecipient, CampaignStatus, Message, MessageStatus
from app.utils import utcnow
from tests.helpers import category, make_client
from tests.test_messaging import FakeProvider


def _campaign(db, status=CampaignStatus.DRAFT, **kw):
    campaign = Campaign(name="Safety", channel="sms", content="Hello {{first_name}}",
                        audience={"category_ids": [category("Safari Clients").id]}, status=status, **kw)
    db.session.add(campaign)
    db.session.commit()
    return campaign


def test_second_worker_cannot_process_a_campaign_another_worker_holds(db):
    make_client(categories=["Safari Clients"])
    fake = FakeProvider()
    message_service.set_provider("sms", fake)
    campaign = _campaign(db)
    # Simulate worker A mid-campaign: lease held and unexpired.
    campaign.lease_owner, campaign.lease_expires_at = "worker-a", utcnow() + timedelta(minutes=5)
    db.session.commit()

    assert queue_campaign(campaign)  # enqueues worker B (sync in tests)
    assert fake.sent == []
    assert Message.query.count() == 0

    # Worker A died: once its lease expires, a new worker resumes the campaign.
    campaign.lease_expires_at = utcnow() - timedelta(seconds=1)
    db.session.commit()
    process_campaign(campaign.id)
    assert len(fake.sent) == 1
    db.session.refresh(campaign)
    assert campaign.status == CampaignStatus.COMPLETED and campaign.lease_owner is None


def test_crash_mid_send_is_not_resent(db):
    john = make_client("John Kamau", "+254712345678", categories=["Safari Clients"])
    mary = make_client("Mary Wanjiku", "+254722222222", categories=["Safari Clients"])
    campaign = _campaign(db, status=CampaignStatus.SENDING)
    from app.messaging.service import message_service as ms
    # Worker died after recording John's message but before the provider call finished.
    conv = ms.get_or_create_conversation(john, "sms")
    orphan = Message(conversation=conv, campaign_id=campaign.id, channel="sms", direction="outbound",
                     recipient=john.phone, content="Hello John", status=MessageStatus.SENDING)
    db.session.add(orphan)
    db.session.flush()
    db.session.add_all([
        CampaignRecipient(campaign_id=campaign.id, client_id=john.id, address=john.phone, message_id=orphan.id),
        CampaignRecipient(campaign_id=campaign.id, client_id=mary.id, address=mary.phone),
    ])
    db.session.commit()

    fake = FakeProvider()
    message_service.set_provider("sms", fake)
    process_campaign(campaign.id)

    assert fake.sent == [("+254722222222", "Hello Mary")]  # John is not messaged a second time
    recipients = {r.client_id: r for r in campaign.recipients}
    assert recipients[john.id].status == CampaignRecipient.STATUS_FAILED
    assert "interrupted" in recipients[john.id].error_message
    assert recipients[mary.id].status == CampaignRecipient.STATUS_PROCESSED
    db.session.refresh(orphan)
    assert orphan.status == MessageStatus.FAILED


def test_manual_retry_skips_clients_who_opted_out(db):
    john = make_client(categories=["Safari Clients"])
    message_service.set_provider("sms", FakeProvider([SendResult(success=False, error="No credit")]))
    campaign = _campaign(db)
    queue_campaign(campaign)
    assert Message.query.one().status == MessageStatus.FAILED

    john.channel("sms").opt_out("keyword")
    db.session.commit()
    fake = FakeProvider()
    message_service.set_provider("sms", fake)
    assert retry_failed_messages(campaign.id) == 0
    assert fake.sent == []
    msg = Message.query.one()
    assert msg.status == MessageStatus.FAILED and "opted out" in msg.error_message


def test_automatic_retry_skips_clients_who_opted_out(db):
    john = make_client(categories=["Safari Clients"])

    class OptOutDuringBackoff(FakeProvider):
        def send_message(self, to, body):
            john.channel("sms").opt_out("keyword")  # STOP arrives between attempts
            return self._next((to, body))

    fake = OptOutDuringBackoff([SendResult(success=False, error="timeout", retryable=True)])
    message_service.set_provider("sms", fake)
    queue_campaign(_campaign(db))
    assert len(fake.sent) == 1  # the retry was not attempted
    assert Message.query.one().status == MessageStatus.FAILED


def test_double_clicked_retry_runs_once(db):
    make_client(categories=["Safari Clients"])
    message_service.set_provider("sms", FakeProvider([SendResult(success=False, error="No credit")]))
    campaign = _campaign(db)
    queue_campaign(campaign)
    campaign.lease_owner, campaign.lease_expires_at = "retry-1", utcnow() + timedelta(minutes=5)
    db.session.commit()
    fake = FakeProvider()
    message_service.set_provider("sms", fake)
    assert retry_failed_messages(campaign.id) == 0  # the first retry still holds the lease
    assert fake.sent == []
