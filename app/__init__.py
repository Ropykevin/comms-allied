import logging
import os

from flask import Flask, jsonify, render_template, request
from markupsafe import Markup, escape

from app.config import config_by_name
from app.extensions import csrf, db, limiter, login_manager, migrate


def create_app(config_name=None):
    config_name = config_name or os.environ.get("APP_ENV", "development")
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_object(config_by_name[config_name])

    if config_name == "production" and app.config["SECRET_KEY"] == "dev-insecure-change-me":
        raise RuntimeError("SECRET_KEY must be set in production.")

    proxies = app.config["TRUSTED_PROXY_COUNT"]
    if proxies:
        from werkzeug.middleware.proxy_fix import ProxyFix

        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=proxies, x_proto=proxies, x_host=proxies)

    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
    _configure_logging(app)

    db.init_app(app)
    migrate.init_app(app, db, render_as_batch=app.config["SQLALCHEMY_DATABASE_URI"].startswith("sqlite"))
    login_manager.init_app(app)
    login_manager.login_view = "auth.login"
    login_manager.login_message = "Please sign in to continue."
    login_manager.login_message_category = "info"
    login_manager.session_protection = "strong"
    csrf.init_app(app)
    limiter.init_app(app)

    from app import models  # noqa: F401  (register models with SQLAlchemy)

    _register_blueprints(app)
    _register_template_helpers(app)
    _register_error_handlers(app)
    _register_security_headers(app)

    from app.cli import register_cli
    register_cli(app)

    from app.tasks import init_tasks
    init_tasks(app)

    return app


def _configure_logging(app):
    level = getattr(logging, app.config.get("LOG_LEVEL", "INFO").upper(), logging.INFO)
    if not app.testing:
        logging.basicConfig(level=level, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    app.logger.setLevel(level)


def _register_blueprints(app):
    from app.auth.routes import bp as auth_bp
    from app.campaigns.routes import bp as campaigns_bp
    from app.campaigns.templates_routes import bp as templates_bp
    from app.categories.routes import bp as categories_bp
    from app.clients.routes import bp as clients_bp
    from app.conversations.routes import bp as conversations_bp
    from app.dashboard.routes import bp as dashboard_bp
    from app.public.routes import bp as public_bp
    from app.settings.routes import bp as settings_bp
    from app.webhooks.routes import bp as webhooks_bp

    for bp in (auth_bp, dashboard_bp, clients_bp, categories_bp, campaigns_bp, templates_bp,
               conversations_bp, settings_bp, webhooks_bp, public_bp):
        app.register_blueprint(bp)

    # Providers authenticate webhooks with signatures/tokens, not browser CSRF tokens.
    csrf.exempt(webhooks_bp)
    limiter.limit("600/minute")(webhooks_bp)


def _register_template_helpers(app):
    from app.models import CHANNEL_LABELS
    from app.permissions import Permission
    from app.utils import to_local, truncate, utcnow

    @app.template_filter("localtime")
    def localtime_filter(dt, fmt="%d %b %Y, %H:%M"):
        local = to_local(dt)
        return local.strftime(fmt) if local else "—"

    @app.template_filter("timeago")
    def timeago_filter(dt):
        if not dt:
            return "—"
        seconds = int((utcnow() - dt).total_seconds())
        if seconds < 0:
            local = to_local(dt)
            return local.strftime("%d %b %Y, %H:%M")
        if seconds < 60:
            return "just now"
        for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
            if seconds >= size:
                value = seconds // size
                if unit == "d" and value > 30:
                    return to_local(dt).strftime("%d %b %Y")
                return f"{value}{unit} ago"
        return "just now"

    @app.template_filter("nl2br")
    def nl2br_filter(text):
        if not text:
            return ""
        return Markup("<br>".join(escape(line) for line in str(text).splitlines()))

    @app.template_filter("truncate_text")
    def truncate_filter(text, length=80):
        return truncate(text, length)

    @app.template_filter("channel_label")
    def channel_label_filter(value):
        return CHANNEL_LABELS.get(value, value or "—")

    @app.template_filter("number")
    def number_filter(value):
        try:
            return f"{int(value):,}"
        except (TypeError, ValueError):
            return value

    @app.template_filter("percent")
    def percent_filter(part, whole):
        if not whole:
            return 0
        return round(100.0 * (part or 0) / whole, 1)

    @app.context_processor
    def inject_globals():
        from flask import has_request_context
        from flask_login import current_user

        open_conversations = 0
        if (
            has_request_context()
            and current_user
            and current_user.is_authenticated
            and current_user.can(Permission.CONVERSATIONS_VIEW)
        ):
            from app.models import Conversation, ConversationStatus

            open_conversations = (
                db.session.query(db.func.count(Conversation.id))
                .filter(Conversation.status == ConversationStatus.OPEN, Conversation.unread_count > 0)
                .scalar()
            )
        return {
            "P": Permission,
            "APP_NAME": app.config["APP_NAME"],
            "APP_TAGLINE": app.config["APP_TAGLINE"],
            "CHANNEL_LABELS": CHANNEL_LABELS,
            "nav_unread_conversations": open_conversations,
        }


def _register_error_handlers(app):
    def _wants_json():
        return request.path.startswith("/webhooks") or request.accept_mimetypes.best == "application/json"

    def _handler(code, title, message):
        def handle(error):
            if _wants_json():
                return jsonify(error=title), code
            return render_template("errors/error.html", code=code, title=title, message=message), code

        return handle

    app.register_error_handler(400, _handler(400, "Bad request", "The request could not be understood."))
    app.register_error_handler(403, _handler(403, "Access denied", "Your role does not have permission to do that."))
    app.register_error_handler(404, _handler(404, "Page not found", "We couldn't find what you were looking for."))
    app.register_error_handler(413, _handler(413, "File too large", "The uploaded file exceeds the 10 MB limit."))
    app.register_error_handler(429, _handler(429, "Too many requests", "Please wait a moment and try again."))

    @app.errorhandler(500)
    def server_error(error):
        db.session.rollback()
        if _wants_json():
            return jsonify(error="Server error"), 500
        return render_template(
            "errors/error.html", code=500, title="Something went wrong",
            message="An unexpected error occurred. The team has been notified.",
        ), 500


def _register_security_headers(app):
    csp = (
        "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
        "script-src 'self'; font-src 'self' data:; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    )

    @app.after_request
    def set_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        response.headers.setdefault("Content-Security-Policy", csp)
        if app.config.get("SESSION_COOKIE_SECURE"):
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        return response
