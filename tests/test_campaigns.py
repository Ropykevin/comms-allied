from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.campaigns.audience import eligible_recipients, estimate, normalize_audience
from app.campaigns.services import dispatch_due_campaigns, personalize, sms_segments, variables_in
from app.models import (
    AuditLog,
    Campaign,
    CampaignRecipient,
    CampaignStatus,
    Conversation,
    Message,
    MessageStatus,
)
from app.utils import utcnow
from tests.helpers import category, make_client, tag

NAIROBI = ZoneInfo("Africa/Nairobi")


def _seed_audience():
    """3 Safari clients: one fully reachable, one without email, one opted out of WhatsApp."""
    a = make_client("John Kamau", "+254712345678", "john@example.com", categories=["Safari Clients", "VIP Clients"],
                    tags=["Interested"])
    b = make_client("Mary Wanjiku", "+254722222222", None, categories=["Safari Clients"], tags=["Interested"],
                    location="Mombasa")
    c = make_client("Peter Otieno", "+254733333333", "peter@example.com", categories=["Safari Clients"],
                    whatsapp_allowed=False, status="inactive")
    other = make_client("Grace Njeri", "+254744444444", "grace@example.com", categories=["Corporate Clients"])
    archived = make_client("Old Client", "+254755555555", "old@example.com", categories=["Safari Clients"],
                           status="archived")
    return a, b, c, other, archived


# ---- audience ----------------------------------------------------------------------

def test_recipient_calculation_by_category_and_channel(app):
    _seed_audience()
    safari = category("Safari Clients").id
    est = estimate({"category_ids": [safari]})
    assert est["total"] == 3  # archived client excluded
    assert est["channels"]["sms"]["available"] == 3
    assert est["channels"]["whatsapp"] == {"available": 2, "excluded": 1, "opted_out": 1, "missing_contact": 0}
    assert est["channels"]["email"] == {"available": 2, "excluded": 1, "opted_out": 0, "missing_contact": 1}


def test_audience_filters_are_anded_across_types(app):
    _seed_audience()
    aud = {"category_ids": [category("Safari Clients").id], "tag_ids": [tag("Interested").id], "statuses": ["active"]}
    assert estimate(aud)["total"] == 2
    aud["location"] = "mombasa"
    assert estimate(aud)["total"] == 1
    # categories are OR-ed
    both = {"category_ids": [category("Safari Clients").id, category("Corporate Clients").id]}
    assert estimate(both)["total"] == 4


def test_no_categories_targets_everyone_except_archived(app):
    _seed_audience()
    assert estimate({})["total"] == 4


def test_normalize_audience_drops_archived_status_and_junk(app):
    aud = normalize_audience({"category_ids": ["3", "x", 3], "statuses": ["archived", "active", "bogus"]})
    assert aud == {"category_ids": [3], "tag_ids": [], "statuses": ["active"], "location": ""}


def test_opt_out_respected_in_recipients(app):
    _, _, peter, _, _ = _seed_audience()
    rows = eligible_recipients({"category_ids": [category("Safari Clients").id]}, "whatsapp")
    assert peter.id not in {r[0] for r in rows}


def test_estimate_endpoint(login_as):
    _seed_audience()
    c = login_as("marketing")
    resp = c.post("/campaigns/audience-estimate", json={"category_ids": [category("Safari Clients").id]})
    assert resp.status_code == 200
    assert resp.json["total"] == 3
    assert login_as("support").post("/campaigns/audience-estimate", json={}).status_code == 403


# ---- personalisation ---------------------------------------------------------------

def test_personalization(app):
    client = make_client("John Kamau", company="ABC Ltd")
    text = "Hello {{first_name}} {{ last_name }} of {{company}}! {{unknown}}"
    assert personalize(text, client) == "Hello John Kamau of ABC Ltd! {{unknown}}"
    assert variables_in("{{company}} {{first_name}} {{company}}") == ["company", "first_name"]


def test_personalization_escapes_html_and_is_not_jinja(app):
    client = make_client("<script>x</script> Doe")
    assert personalize("Hi {{first_name}}", client, html=True) == "Hi &lt;script&gt;x&lt;/script&gt;"
    assert personalize("{{ 7*7 }} {% if 1 %}x{% endif %}", client) == "{{ 7*7 }} {% if 1 %}x{% endif %}"


def test_sms_segments():
    assert sms_segments("a" * 160) == 1
    assert sms_segments("a" * 161) == 2
    assert sms_segments("é" * 71) == 2


def test_preview_endpoint(login_as):
    make_client("John Kamau", categories=["Safari Clients"])
    c = login_as("marketing")
    resp = c.post("/campaigns/preview", json={
        "channel": "email", "subject": "Hi {{first_name}}", "content": "Hello {{first_name}},\nSafari time!",
        "audience": {"category_ids": [category("Safari Clients").id]},
    })
    data = resp.json
    assert data["subject"] == "Hi John"
    assert data["content"].startswith("Hello John,")
    assert "Hello John,<br>Safari time!" in data["email_html"]


# ---- campaign creation, scheduling, sending -----------------------------------------

def _wizard(**overrides):
    data = {
        "name": "October Safari Campaign", "description": "", "channel": "whatsapp",
        "template_id": "0", "subject": "", "content": "Hello {{first_name}}, safari packages are here!",
        "timezone": "Africa/Nairobi", "action": "save_draft",
    }
    data.update(overrides)
    return data


def test_create_draft(login_as):
    _seed_audience()
    c = login_as("marketing")
    resp = c.post("/campaigns/create", data=_wizard(category_ids=[category("Safari Clients").id]))
    assert resp.status_code == 302
    campaign = Campaign.query.one()
    assert campaign.status == CampaignStatus.DRAFT
    assert campaign.audience["category_ids"] == [category("Safari Clients").id]
    assert AuditLog.query.filter_by(action="campaign.created").count() == 1
    assert c.get(f"/campaigns/{campaign.id}").status_code == 200
    assert c.get(f"/campaigns/{campaign.id}/edit").status_code == 200


def test_wizard_pages_render(login_as):
    c = login_as("marketing")
    resp = c.get(f"/campaigns/create?category={category('Safari Clients').id}")
    assert resp.status_code == 200
    assert b"Estimated recipients" in resp.data
    assert c.get("/campaigns/").status_code == 200


def test_send_now_delivers_to_eligible_recipients(login_as, db):
    john, mary, peter, _, archived = _seed_audience()
    c = login_as("marketing")
    resp = c.post("/campaigns/create", data=_wizard(action="send_now", category_ids=[category("Safari Clients").id]))
    assert resp.status_code == 302
    campaign = Campaign.query.one()
    assert campaign.status == CampaignStatus.COMPLETED
    assert campaign.total_recipients == 2
    assert campaign.excluded_count == 1
    messages = Message.query.filter_by(campaign_id=campaign.id).all()
    assert {m.recipient for m in messages} == {"+254712345678", "+254722222222"}
    assert all(m.status == MessageStatus.SENT and m.provider_message_id for m in messages)
    john_msg = next(m for m in messages if m.recipient == "+254712345678")
    assert john_msg.content == "Hello John, safari packages are here!"
    # Each message lives in a per-channel conversation
    assert Conversation.query.filter_by(client_id=john.id, channel="whatsapp").count() == 1
    assert AuditLog.query.filter_by(action="campaign.sent").count() == 1
    db.session.refresh(john)
    assert john.last_contacted_at is not None


def test_send_requires_send_permission(login_as):
    _seed_audience()
    # Support agents can't even open the wizard; marketing can send; viewer cannot.
    assert login_as("support").get("/campaigns/create").status_code == 403
    assert login_as("viewer").post("/campaigns/create", data=_wizard(action="send_now")).status_code == 403


def test_send_blocked_without_reachable_recipients(login_as):
    make_client("No Email", "+254712345678", None, categories=["VIP Clients"])
    c = login_as("marketing")
    resp = c.post("/campaigns/create", data=_wizard(action="send_now", channel="email", subject="Hi",
                                                     category_ids=[category("VIP Clients").id]))
    assert resp.status_code == 400
    assert b"No clients in this audience can be reached" in resp.data
    assert Campaign.query.count() == 0


def test_email_requires_subject(login_as):
    _seed_audience()
    resp = login_as("marketing").post("/campaigns/create", data=_wizard(action="send_now", channel="email"))
    assert resp.status_code == 400
    assert b"subject" in resp.data


def test_unknown_variables_rejected(login_as):
    _seed_audience()
    resp = login_as("marketing").post("/campaigns/create", data=_wizard(action="send_now", content="Hi {{nickname}}"))
    assert resp.status_code == 400
    assert b"Unknown variable" in resp.data


def test_schedule_then_dispatch(login_as, db):
    _seed_audience()
    c = login_as("marketing")
    future = datetime.now(NAIROBI) + timedelta(days=2)
    resp = c.post("/campaigns/create", data=_wizard(
        action="schedule", category_ids=[category("Safari Clients").id],
        schedule_date=future.strftime("%Y-%m-%d"), schedule_time="09:30", timezone="Africa/Nairobi",
    ))
    assert resp.status_code == 302
    campaign = Campaign.query.one()
    assert campaign.status == CampaignStatus.SCHEDULED
    # 09:30 in Nairobi (UTC+3) is 06:30 UTC
    assert campaign.scheduled_at.hour == 6 and campaign.scheduled_at.minute == 30
    assert dispatch_due_campaigns() == 0

    campaign.scheduled_at = utcnow() - timedelta(minutes=1)
    db.session.commit()
    assert dispatch_due_campaigns() == 1
    db.session.refresh(campaign)
    assert campaign.status == CampaignStatus.COMPLETED
    assert Message.query.filter_by(campaign_id=campaign.id).count() == 2
    assert dispatch_due_campaigns() == 0  # never dispatched twice


def test_schedule_in_past_rejected(login_as):
    _seed_audience()
    resp = login_as("marketing").post("/campaigns/create", data=_wizard(
        action="schedule", schedule_date="2020-01-01", schedule_time="09:00"))
    assert resp.status_code == 400
    assert b"must be in the future" in resp.data


def test_unschedule_and_double_send_guard(login_as, db):
    _seed_audience()
    c = login_as("marketing")
    future = datetime.now(NAIROBI) + timedelta(days=1)
    c.post("/campaigns/create", data=_wizard(action="schedule", schedule_date=future.strftime("%Y-%m-%d"),
                                             schedule_time="10:00"))
    campaign = Campaign.query.one()
    c.post(f"/campaigns/{campaign.id}/cancel")
    db.session.refresh(campaign)
    assert campaign.status == CampaignStatus.DRAFT and campaign.scheduled_at is None
    c.post(f"/campaigns/{campaign.id}/send")
    c.post(f"/campaigns/{campaign.id}/send")
    assert Message.query.filter_by(campaign_id=campaign.id).count() == 3  # every WhatsApp-reachable client, once each
    assert CampaignRecipient.query.filter_by(campaign_id=campaign.id).count() == 3


def test_opt_out_after_scheduling_is_skipped(login_as, db):
    john, mary, *_ = _seed_audience()
    c = login_as("marketing")
    c.post("/campaigns/create", data=_wizard(category_ids=[category("Safari Clients").id]))
    campaign = Campaign.query.one()
    from app.campaigns.services import queue_campaign, snapshot_recipients
    # Freeze recipients, then John opts out before the worker runs.
    snapshot_recipients(campaign)
    db.session.commit()
    john.channel("whatsapp").opt_out("keyword")
    db.session.commit()
    campaign.recipients.delete()
    db.session.commit()
    from app.campaigns import services
    services._claim(campaign, (CampaignStatus.DRAFT,), CampaignStatus.QUEUED)
    db.session.add_all([CampaignRecipient(campaign_id=campaign.id, client_id=john.id, address=john.phone),
                        CampaignRecipient(campaign_id=campaign.id, client_id=mary.id, address=mary.phone)])
    db.session.commit()
    services.process_campaign(campaign.id)
    statuses = {r.client_id: r.status for r in campaign.recipients}
    assert statuses[john.id] == CampaignRecipient.STATUS_SKIPPED
    assert statuses[mary.id] == CampaignRecipient.STATUS_PROCESSED
    assert queue_campaign  # imported for clarity


def test_duplicate_and_delete_draft(login_as):
    _seed_audience()
    c = login_as("marketing")
    c.post("/campaigns/create", data=_wizard())
    original = Campaign.query.one()
    c.post(f"/campaigns/{original.id}/duplicate")
    assert Campaign.query.count() == 2
    copy = Campaign.query.filter(Campaign.id != original.id).one()
    assert copy.name.endswith("(copy)") and copy.status == CampaignStatus.DRAFT
    c.post(f"/campaigns/{copy.id}/delete")
    assert Campaign.query.count() == 1


def test_campaign_from_template(login_as):
    from app.models import MessageTemplate
    t = MessageTemplate.query.filter_by(name="Safari Promotion").one()
    c = login_as("marketing")
    resp = c.get(f"/campaigns/create?template={t.id}")
    assert b"latest safari packages" in resp.data
    data = c.get(f"/campaigns/templates/{t.id}.json").json
    assert data["channel"] == "whatsapp"


def test_templates_crud(login_as):
    from app.models import MessageTemplate
    c = login_as("marketing")
    resp = c.post("/templates/new", data={"name": "Visa Reminder", "channel": "sms",
                                          "content": "Hi {{first_name}}, renew your visa.", "status": "active"})
    assert resp.status_code == 302
    t = MessageTemplate.query.filter_by(name="Visa Reminder").one()
    resp = c.post("/templates/new", data={"name": "Bad", "channel": "sms", "content": "Hi {{nick}}", "status": "active"})
    assert resp.status_code == 200 and b"Unknown variable" in resp.data
    resp = c.post("/templates/new", data={"name": "No subject", "channel": "email", "content": "x", "status": "active"})
    assert b"need a subject" in resp.data
    c.post(f"/templates/{t.id}/edit", data={"name": "Visa Reminder", "channel": "sms", "content": "Updated",
                                            "status": "archived"})
    assert MessageTemplate.query.filter_by(id=t.id).one().status == "archived"
    assert login_as("support").post("/templates/new", data={}).status_code == 403
    assert login_as("support").get("/templates/").status_code == 200
