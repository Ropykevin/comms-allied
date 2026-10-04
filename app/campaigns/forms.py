from flask_wtf import FlaskForm
from flask_wtf.file import FileField
from wtforms import BooleanField, DateField, RadioField, SelectField, StringField, TextAreaField, TimeField
from wtforms.validators import DataRequired, Length, Optional

from app.models import CHANNEL_LABELS, CHANNELS, MessageTemplate

TIMEZONES = [
    "Africa/Nairobi", "Africa/Kampala", "Africa/Dar_es_Salaam", "Africa/Kigali", "Africa/Addis_Ababa",
    "Africa/Johannesburg", "Africa/Lagos", "Africa/Cairo", "Europe/London", "Europe/Paris", "Asia/Dubai",
    "Asia/Kolkata", "America/New_York", "UTC",
]


class CampaignForm(FlaskForm):
    name = StringField("Campaign name", validators=[DataRequired(), Length(max=150)])
    description = TextAreaField("Description", validators=[Optional(), Length(max=2000)])
    channel = RadioField("Channel", choices=[(c, CHANNEL_LABELS[c]) for c in CHANNELS], validators=[Optional()])
    template_id = SelectField("Start from a template", coerce=int, validators=[Optional()], validate_choice=False)
    subject = StringField("Subject", validators=[Optional(), Length(max=255)])
    content = TextAreaField("Message", validators=[Optional(), Length(max=20000)])
    is_html = BooleanField("Content is HTML")
    attachment = FileField("Attachment")
    remove_attachment = BooleanField("Remove attachment")
    schedule_date = DateField("Date", validators=[Optional()])
    schedule_time = TimeField("Time", validators=[Optional()])
    timezone = SelectField("Timezone", choices=[(tz, tz.replace("_", " ")) for tz in TIMEZONES],
                           default="Africa/Nairobi")


class TemplateForm(FlaskForm):
    name = StringField("Template name", validators=[DataRequired(), Length(max=120)])
    channel = SelectField("Channel", choices=[(c, CHANNEL_LABELS[c]) for c in CHANNELS])
    subject = StringField("Email subject", validators=[Optional(), Length(max=255)])
    content = TextAreaField("Content", validators=[DataRequired(), Length(max=20000)])
    is_html = BooleanField("Content is HTML (email only)")
    provider_template_name = StringField(
        "Approved WhatsApp template name", validators=[Optional(), Length(max=120)],
        description="Business-initiated WhatsApp messages must use a template approved by Meta. "
                    "Variables are passed as {{1}}, {{2}}… in the order they appear.",
    )
    status = SelectField("Status", choices=[(MessageTemplate.STATUS_ACTIVE, "Active"),
                                            (MessageTemplate.STATUS_ARCHIVED, "Archived")])
