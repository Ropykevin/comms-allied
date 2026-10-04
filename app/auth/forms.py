from flask_wtf import FlaskForm
from wtforms import BooleanField, EmailField, PasswordField, StringField, SubmitField
from wtforms.validators import DataRequired, Email, EqualTo, Length, Optional, ValidationError


COMMON_PASSWORDS = {
    "password123", "password1234", "qwerty12345", "qwertyuiop1", "123456789a", "1234567890a", "iloveyou123",
    "welcome123", "welcome1234", "admin12345", "letmein1234", "nairobi123", "nairobi2024", "nairobi2025",
    "nairobi2026", "kenya12345", "safari2024", "safari2025", "safari2026", "allied2024", "allied2025",
    "allied2026", "alliedtours1", "changeme123", "p@ssw0rd123", "passw0rd123",
}


def password_problem(value):
    """Return why a password is too weak, or None. Shared by forms and the CLI."""
    value = value or ""
    if len(value) < 12:
        return "Use at least 12 characters."
    if value.isdigit() or value.isalpha():
        return "Use a mix of letters and numbers or symbols."
    if value.lower() in COMMON_PASSWORDS or len(set(value)) < 5:
        return "This password is too common or repetitive. Choose something harder to guess."
    return None


def strong_password(form, field):
    problem = password_problem(field.data)
    if problem:
        raise ValidationError(problem)


class LoginForm(FlaskForm):
    email = EmailField("Email", validators=[DataRequired(), Email()])
    password = PasswordField("Password", validators=[DataRequired()])
    remember = BooleanField("Keep me signed in")
    submit = SubmitField("Sign in")


class ForgotPasswordForm(FlaskForm):
    email = EmailField("Email", validators=[DataRequired(), Email()])
    submit = SubmitField("Send reset link")


class ResetPasswordForm(FlaskForm):
    password = PasswordField("New password", validators=[DataRequired(), strong_password])
    confirm = PasswordField("Confirm new password", validators=[DataRequired(), EqualTo("password", "Passwords must match.")])
    submit = SubmitField("Reset password")


class ChangePasswordForm(FlaskForm):
    current_password = PasswordField("Current password", validators=[DataRequired()])
    password = PasswordField("New password", validators=[DataRequired(), strong_password])
    confirm = PasswordField("Confirm new password", validators=[DataRequired(), EqualTo("password", "Passwords must match.")])
    submit = SubmitField("Update password")


class TwoFactorForm(FlaskForm):
    code = StringField("6-digit code", validators=[DataRequired(), Length(min=6, max=8)])
    submit = SubmitField("Verify")


class DisableTwoFactorForm(FlaskForm):
    password = PasswordField("Current password", validators=[DataRequired()])
    code = StringField("6-digit code", validators=[DataRequired(), Length(min=6, max=8)])
    submit = SubmitField("Turn off")


class ProfileForm(FlaskForm):
    full_name = StringField("Full name", validators=[DataRequired(), Length(max=120)])
    phone = StringField("Phone", validators=[Optional(), Length(max=32)])
    submit = SubmitField("Save profile")
