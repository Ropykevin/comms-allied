from flask_wtf import FlaskForm
from flask_wtf.file import FileAllowed, FileField, FileRequired
from wtforms import BooleanField, EmailField, SelectField, SelectMultipleField, StringField, TextAreaField
from wtforms.validators import DataRequired, Email, Length, Optional, ValidationError

from app.models import ClientStatus
from app.utils import normalize_phone


class MultiCheckboxField(SelectMultipleField):
    pass


def valid_phone(form, field):
    if field.data and not normalize_phone(field.data):
        raise ValidationError("Enter a valid phone number, e.g. +254712345678 or 0712345678.")


class ClientForm(FlaskForm):
    full_name = StringField("Full name", validators=[DataRequired(), Length(max=150)])
    phone = StringField("Phone (SMS)", validators=[Optional(), Length(max=32), valid_phone])
    whatsapp = StringField("WhatsApp number", validators=[Optional(), Length(max=32), valid_phone])
    email = EmailField("Email", validators=[Optional(), Email(), Length(max=255)])
    company = StringField("Company", validators=[Optional(), Length(max=150)])
    location = StringField("Location", validators=[Optional(), Length(max=120)])
    status = SelectField("Status", choices=[(s, ClientStatus.LABELS[s]) for s in ClientStatus.ALL])
    notes = TextAreaField("Notes", validators=[Optional(), Length(max=5000)])
    categories = MultiCheckboxField("Categories", coerce=int, validate_choice=False)
    tags = MultiCheckboxField("Tags", coerce=int, validate_choice=False)
    new_tags = StringField("New tags", validators=[Optional(), Length(max=300)])
    sms_allowed = BooleanField("SMS allowed", default=True)
    whatsapp_allowed = BooleanField("WhatsApp allowed", default=True)
    email_allowed = BooleanField("Email allowed", default=True)

    def validate(self, extra_validators=None):
        ok = super().validate(extra_validators)
        if not (self.phone.data or self.email.data or self.whatsapp.data):
            self.phone.errors.append("Provide at least a phone number or an email address.")
            ok = False
        return ok


class ImportForm(FlaskForm):
    file = FileField("CSV file", validators=[FileRequired(), FileAllowed(["csv"], "Upload a .csv file.")])
