"""Unauthenticated endpoints: health check, email unsubscribe and open tracking."""
import base64

from flask import Blueprint, Response, abort, jsonify, render_template
from sqlalchemy import text

from app import audit
from app.extensions import db, limiter
from app.messaging.service import TRACKING_SALT, UNSUBSCRIBE_SALT, load_token, message_service
from app.models import CHANNEL_EMAIL, Client, ContactChannel, Message, MessageStatus

bp = Blueprint("public", __name__)

PIXEL = base64.b64decode("R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7")


@bp.route("/healthz")
@limiter.exempt
def healthz():
    try:
        db.session.execute(text("SELECT 1"))
        return jsonify(status="ok", database="ok")
    except Exception:  # noqa: BLE001
        return jsonify(status="degraded", database="unavailable"), 503


@bp.route("/unsubscribe/<token>", methods=["GET", "POST"])
@limiter.limit("30/minute")
def unsubscribe(token):
    from flask import request

    data = load_token(token, UNSUBSCRIBE_SALT)
    client = db.session.get(Client, data.get("c")) if data else None
    if client is None:
        abort(404)
    done = False
    # Unsubscribing requires a POST so that link scanners in mail clients don't opt people out.
    if request.method == "POST":
        cc = client.channel(CHANNEL_EMAIL)
        if cc is None:
            cc = ContactChannel(client=client, channel=CHANNEL_EMAIL, address=client.email)
            db.session.add(cc)
        if cc.is_allowed:
            cc.opt_out("unsubscribe_link")
            audit.record("client.opted_out", "client", client.id, {"channel": "email", "source": "unsubscribe_link"})
        db.session.commit()
        done = True
    return render_template("public/unsubscribe.html", client=client, done=done)


@bp.route("/t/o/<token>.gif")
@limiter.exempt
def track_open(token):
    data = load_token(token, TRACKING_SALT)
    if data:
        message = db.session.get(Message, data.get("m"))
        if message is not None and message.channel == CHANNEL_EMAIL:
            if message_service.apply_status(message, MessageStatus.READ):
                db.session.commit()
    resp = Response(PIXEL, mimetype="image/gif")
    resp.headers["Cache-Control"] = "no-store, max-age=0"
    return resp
