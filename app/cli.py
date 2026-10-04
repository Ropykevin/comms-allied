import random
import re
import time

import click
from flask import current_app

from app.extensions import db
from app.permissions import ROLE_DEFINITIONS, ROLE_SUPER_ADMIN


DEFAULT_CATEGORIES = [
    ("VIP Clients", "amber", True), ("Safari Clients", "emerald", True), ("Corporate Clients", "indigo", True),
    ("Prospects", "violet", True), ("New Clients", "sky", False), ("Existing Clients", "teal", False),
    ("Inactive Clients", "slate", False), ("Flight Clients", "sky", False), ("Hotel Clients", "rose", False),
    ("Tour Clients", "orange", False), ("Follow-Up Required", "lime", False),
]
DEFAULT_TAGS = ["Nairobi", "Mombasa", "Premium", "2026", "Interested", "Follow-Up", "Paid", "Pending-Payment", "Safari"]
DEFAULT_TEMPLATES = [
    ("Safari Promotion", "whatsapp", None,
     "Hello {{first_name}},\n\nWe are excited to share our latest safari packages with you — Maasai Mara, "
     "Amboseli and Tsavo departures are now open.\n\nContact Allied Tours & Travel Agency today.\n\nThank you."),
    ("Flight Reminder", "sms", None,
     "Hi {{first_name}}, a reminder that your flight is coming up. Please arrive at the airport 3 hours early. "
     "Safe travels! - Allied Tours & Travel"),
    ("Hotel Promotion", "email", "Exclusive hotel deals for you, {{first_name}}",
     "Hello {{first_name}},\n\nEnjoy special rates at our partner hotels in Mombasa, Diani and Naivasha this "
     "season.\n\nReply to this email to reserve your stay.\n\nAllied Tours & Travel Agency"),
    ("Travel Follow-Up", "whatsapp", None,
     "Hello {{first_name}}, welcome back! We hope you enjoyed your trip. We'd love to hear your feedback."),
    ("Payment Reminder", "sms", None,
     "Dear {{first_name}}, this is a friendly reminder that your travel package balance is due. "
     "Contact us for payment options. - Allied Tours & Travel"),
    ("Holiday Promotion", "email", "Your holiday getaway starts here",
     "Dear {{first_name}},\n\nThe holidays are around the corner! Discover our festive season packages "
     "for families and groups.\n\nAllied Tours & Travel Agency - Your one-stop travel shop."),
    ("Booking Confirmation", "email", "Your booking is confirmed",
     "Dear {{first_name}},\n\nThank you for booking with Allied Tours & Travel Agency. Your reservation "
     "for {{company}} has been confirmed.\n\nWe look forward to serving you."),
]


def slugify(value):
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def seed_reference_data():
    from app.models import Category, MessageTemplate, Role, Tag

    for name, (label, description) in ROLE_DEFINITIONS.items():
        role = Role.query.filter_by(name=name).first()
        if role is None:
            db.session.add(Role(name=name, label=label, description=description))
        else:
            role.label, role.description = label, description
    for name, color, featured in DEFAULT_CATEGORIES:
        if not Category.query.filter_by(name=name).first():
            db.session.add(Category(name=name, slug=slugify(name), color=color, is_featured=featured))
    for name in DEFAULT_TAGS:
        if not Tag.query.filter_by(name=name).first():
            db.session.add(Tag(name=name))
    for name, channel, subject, content in DEFAULT_TEMPLATES:
        if not MessageTemplate.query.filter_by(name=name, channel=channel).first():
            db.session.add(MessageTemplate(name=name, channel=channel, subject=subject, content=content))
    db.session.commit()


def create_user(email, password, full_name, role_name):
    from app.models import Role, User

    role = Role.query.filter_by(name=role_name).first()
    if role is None:
        raise click.ClickException(f"Unknown role {role_name}. Run `flask seed` first.")
    user = User(email=email.strip().lower(), full_name=full_name, role=role)
    user.set_password(password)
    db.session.add(user)
    db.session.commit()
    return user


def register_cli(app):
    @app.cli.command("seed")
    def seed():
        """Create roles, default categories, tags, templates and the first super admin."""
        from app.models import User

        seed_reference_data()
        click.echo("Roles, categories, tags and templates are in place.")
        email = current_app.config["ADMIN_EMAIL"]
        if User.query.filter_by(email=email.lower()).first():
            click.echo(f"Super admin {email} already exists.")
            return
        password = current_app.config["ADMIN_PASSWORD"]
        if not password:
            password = click.prompt("Password for the super admin", hide_input=True, confirmation_prompt=True)
        create_user(email, password, "System Administrator", ROLE_SUPER_ADMIN)
        click.echo(f"Created super admin {email}.")

    @app.cli.command("create-user")
    @click.option("--email", prompt=True)
    @click.option("--name", prompt="Full name")
    @click.option("--role", type=click.Choice(list(ROLE_DEFINITIONS)), prompt=True)
    @click.password_option()
    def create_user_cmd(email, name, role, password):
        """Create a staff user."""
        create_user(email, password, name, role)
        click.echo(f"Created {role} {email}.")

    @app.cli.command("seed-demo")
    @click.option("--count", default=120, show_default=True)
    def seed_demo(count):
        """Add realistic demo clients for trying out the platform."""
        seed_reference_data()
        n = seed_demo_clients(count)
        click.echo(f"Added {n} demo clients.")

    @app.cli.command("dispatch-due")
    def dispatch_due():
        """Queue scheduled campaigns whose send time has passed."""
        from app.campaigns.services import dispatch_due_campaigns
        click.echo(f"Dispatched {dispatch_due_campaigns()} campaign(s).")

    @app.cli.command("run-scheduler")
    @click.option("--interval", default=60, show_default=True)
    def run_scheduler(interval):
        """Development scheduler loop (production uses Celery beat)."""
        from app.campaigns.services import dispatch_due_campaigns
        click.echo(f"Checking for due campaigns every {interval}s. Ctrl+C to stop.")
        while True:
            try:
                count = dispatch_due_campaigns()
                if count:
                    click.echo(f"Dispatched {count} campaign(s).")
            except Exception as exc:  # noqa: BLE001
                db.session.rollback()
                click.echo(f"Scheduler error: {exc}", err=True)
            db.session.remove()
            time.sleep(interval)


FIRST = ["John", "Mary", "Peter", "Grace", "David", "Faith", "James", "Joyce", "Daniel", "Esther", "Brian", "Mercy",
         "Kevin", "Ann", "Samuel", "Lucy", "Paul", "Wanjiru", "Otieno", "Achieng", "Mohamed", "Amina", "Ali", "Fatma"]
LAST = ["Kamau", "Wanjiku", "Otieno", "Mwangi", "Njoroge", "Ochieng", "Kiprop", "Chebet", "Mutua", "Wambui",
        "Omondi", "Kariuki", "Njeri", "Hassan", "Abdalla", "Barasa", "Wekesa", "Muthoni"]
COMPANIES = [None, None, "Safaricom PLC", "KCB Group", "Equity Bank", "Bamburi Cement", "Twiga Foods", "ABC Ltd",
             "XYZ Ltd", "Nation Media", "Kenya Airways", "Java House"]
LOCATIONS = ["Nairobi", "Nairobi", "Nairobi", "Mombasa", "Mombasa", "Kisumu", "Nakuru", "Eldoret", "Malindi", "Thika"]


def seed_demo_clients(count):
    from app.clients.services import save_client
    from app.models import Category, Client, Tag

    rng = random.Random(2026)
    categories = Category.query.all()
    tags = Tag.query.all()
    created = 0
    for _ in range(count):
        first, last = rng.choice(FIRST), rng.choice(LAST)
        phone = f"+2547{rng.randint(10000000, 99999999)}"
        if Client.query.filter_by(phone=phone).first():
            continue
        email = f"{first}.{last}{rng.randint(1, 999)}@example.com".lower() if rng.random() > 0.15 else None
        data = {
            "full_name": f"{first} {last}",
            "phone": phone,
            "whatsapp": phone if rng.random() > 0.1 else None,
            "email": email,
            "company": rng.choice(COMPANIES),
            "location": rng.choice(LOCATIONS),
            "status": rng.choices(["active", "inactive", "prospect"], weights=[70, 15, 15])[0],
            "notes": None,
            "sms_allowed": rng.random() > 0.05,
            "whatsapp_allowed": rng.random() > 0.05,
            "email_allowed": rng.random() > 0.1,
        }
        client = Client()
        save_client(client, data, rng.sample(categories, k=rng.randint(1, 3)), rng.sample(tags, k=rng.randint(0, 3)))
        created += 1
    db.session.commit()
    return created
