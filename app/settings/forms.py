from flask_wtf import FlaskForm
from wtforms import BooleanField, EmailField, PasswordField, SelectField, StringField
from wtforms.validators import DataRequired, Email, Length, Optional

from app.auth.forms import strong_password


class UserForm(FlaskForm):
    full_name = StringField("Full name", validators=[DataRequired(), Length(max=120)])
    email = EmailField("Email", validators=[DataRequired(), Email(), Length(max=255)])
    phone = StringField("Phone", validators=[Optional(), Length(max=32)])
    role_id = SelectField("Role", coerce=int, validators=[DataRequired()])
    is_active = BooleanField("Account is active", default=True)
    password = PasswordField("Password", validators=[Optional(), strong_password])
