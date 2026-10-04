import os
from datetime import timedelta

from dotenv import load_dotenv

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
load_dotenv(os.path.join(BASE_DIR, ".env"))


def _bool(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _database_url(value):
    """Use the psycopg 3 driver for any postgres URL."""
    if not value:
        return value
    if value.startswith("postgres://"):
        value = "postgresql://" + value[len("postgres://"):]
    if value.startswith("postgresql://"):
        value = "postgresql+psycopg://" + value[len("postgresql://"):]
    return value


class Config:
    APP_NAME = "Allied Tours & Travel Agency"
    APP_TAGLINE = "Your one-stop travel shop."

    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-insecure-change-me")
    SQLALCHEMY_DATABASE_URI = _database_url(
        os.environ.get("DATABASE_URL", "postgresql://allied:allied@localhost:5432/allied")
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {"pool_pre_ping": True}

    # Public base URL used for links in outbound emails (reset links, unsubscribe, tracking pixel)
    APP_BASE_URL = os.environ.get("APP_BASE_URL", "http://localhost:5000").rstrip("/")
    DEFAULT_TIMEZONE = os.environ.get("DEFAULT_TIMEZONE", "Africa/Nairobi")
    DEFAULT_PHONE_REGION = os.environ.get("DEFAULT_PHONE_REGION", "KE")

    # Sessions & cookies
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = _bool("SESSION_COOKIE_SECURE", False)
    REMEMBER_COOKIE_HTTPONLY = True
    REMEMBER_COOKIE_SECURE = _bool("SESSION_COOKIE_SECURE", False)
    REMEMBER_COOKIE_DURATION = timedelta(days=14)
    PERMANENT_SESSION_LIFETIME = timedelta(hours=12)
    WTF_CSRF_TIME_LIMIT = None

    # Uploads
    MAX_CONTENT_LENGTH = 10 * 1024 * 1024
    UPLOAD_FOLDER = os.environ.get("UPLOAD_FOLDER", os.path.join(BASE_DIR, "instance", "uploads"))

    # Rate limiting
    RATELIMIT_STORAGE_URI = os.environ.get("RATELIMIT_STORAGE_URI", "memory://")
    RATELIMIT_HEADERS_ENABLED = True

    # Background jobs: "celery" (production), "thread" (simple dev) or "sync" (tests)
    TASK_BACKEND = os.environ.get("TASK_BACKEND", "thread")
    REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    CAMPAIGN_BATCH_SIZE = int(os.environ.get("CAMPAIGN_BATCH_SIZE", "100"))
    MESSAGE_MAX_RETRIES = int(os.environ.get("MESSAGE_MAX_RETRIES", "3"))
    RETRY_BACKOFF_SECONDS = int(os.environ.get("RETRY_BACKOFF_SECONDS", "5"))
    PASSWORD_HASH_METHOD = "scrypt"

    # Mail (SMTP) — used for campaign emails and password resets
    MAIL_PROVIDER = os.environ.get("MAIL_PROVIDER", "console")  # console | smtp
    MAIL_SERVER = os.environ.get("MAIL_SERVER", "")
    MAIL_PORT = int(os.environ.get("MAIL_PORT", "587"))
    MAIL_USE_TLS = _bool("MAIL_USE_TLS", True)
    MAIL_USE_SSL = _bool("MAIL_USE_SSL", False)
    MAIL_USERNAME = os.environ.get("MAIL_USERNAME", "")
    MAIL_PASSWORD = os.environ.get("MAIL_PASSWORD", "")
    MAIL_DEFAULT_SENDER = os.environ.get("MAIL_DEFAULT_SENDER", "Allied Tours & Travel <no-reply@example.com>")
    EMAIL_WEBHOOK_SECRET = os.environ.get("EMAIL_WEBHOOK_SECRET", "")

    # SMS
    SMS_PROVIDER = os.environ.get("SMS_PROVIDER", "console")  # console | africastalking
    SMS_API_KEY = os.environ.get("SMS_API_KEY", "")
    SMS_USERNAME = os.environ.get("SMS_USERNAME", "")
    SMS_SENDER_ID = os.environ.get("SMS_SENDER_ID", "")
    SMS_API_URL = os.environ.get("SMS_API_URL", "https://api.africastalking.com/version1/messaging")
    SMS_WEBHOOK_TOKEN = os.environ.get("SMS_WEBHOOK_TOKEN", "")

    # WhatsApp (Meta WhatsApp Business Cloud API)
    WHATSAPP_PROVIDER = os.environ.get("WHATSAPP_PROVIDER", "console")  # console | meta
    WHATSAPP_API_KEY = os.environ.get("WHATSAPP_API_KEY", "")
    WHATSAPP_PHONE_NUMBER_ID = os.environ.get("WHATSAPP_PHONE_NUMBER_ID", "")
    WHATSAPP_API_VERSION = os.environ.get("WHATSAPP_API_VERSION", "v21.0")
    WHATSAPP_APP_SECRET = os.environ.get("WHATSAPP_APP_SECRET", "")
    WHATSAPP_VERIFY_TOKEN = os.environ.get("WHATSAPP_VERIFY_TOKEN", "")

    # Initial super admin created by `flask seed`
    ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "admin@alliedtours.co.ke")
    ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")

    LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")

    # Number of reverse proxies (e.g. Nginx) in front of the app whose X-Forwarded-* headers are trusted.
    # Leave at 0 when the app is exposed directly, otherwise clients could spoof their IP address.
    TRUSTED_PROXY_COUNT = int(os.environ.get("TRUSTED_PROXY_COUNT", "0"))


class DevelopmentConfig(Config):
    DEBUG = True


class TestingConfig(Config):
    TESTING = True
    SECRET_KEY = "test-secret"
    SQLALCHEMY_DATABASE_URI = _database_url(os.environ.get("TEST_DATABASE_URL") or "sqlite://")
    SQLALCHEMY_ENGINE_OPTIONS = {}
    WTF_CSRF_ENABLED = False
    RATELIMIT_ENABLED = False
    TASK_BACKEND = "sync"
    RETRY_BACKOFF_SECONDS = 0
    PASSWORD_HASH_METHOD = "pbkdf2:sha256:1000"  # fast hashing for tests only
    MAIL_PROVIDER = "console"
    SMS_PROVIDER = "console"
    WHATSAPP_PROVIDER = "console"
    SMS_WEBHOOK_TOKEN = "sms-test-token"
    WHATSAPP_APP_SECRET = "wa-test-secret"
    WHATSAPP_VERIFY_TOKEN = "wa-verify"
    EMAIL_WEBHOOK_SECRET = "email-test-secret"
    APP_BASE_URL = "http://localhost"


class ProductionConfig(Config):
    SESSION_COOKIE_SECURE = True
    REMEMBER_COOKIE_SECURE = True
    PREFERRED_URL_SCHEME = "https"


config_by_name = {
    "development": DevelopmentConfig,
    "testing": TestingConfig,
    "production": ProductionConfig,
}
