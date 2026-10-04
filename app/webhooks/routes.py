"""Provider webhooks: inbound messages and delivery status updates.

Each request is (1) verified, (2) parsed by the channel's provider into
provider-neutral InboundMessage/StatusUpdate objects, then (3) recorded via
MessageService.record_webhook, which stores a WebhookEvent keyed by the
provider's event id so retried deliveries are processed only once.
"""
import json
import logging

from flask import Blueprint, abort, jsonify, request

from app.messaging.base import WebhookVerificationError
from app.messaging.service import message_service
from app.models import CHANNEL_EMAIL, CHANNEL_SMS, CHANNEL_WHATSAPP

log = logging.getLogger(__name__)

bp = Blueprint("webhooks", __name__, url_prefix="/webhooks")


def _payload_for_log():
    if request.form:
        return request.form.to_dict()
    try:
        return json.loads(request.get_data() or b"{}")
    except ValueError:
        return {"raw": request.get_data(as_text=True)[:2000]}


def _verify(provider):
    try:
        provider.verify_webhook(request)
    except WebhookVerificationError as exc:
        log.warning("Rejected %s webhook from %s: %s", provider.name, request.remote_addr, exc)
        abort(403)


def _respond(event, duplicate):
    return jsonify(status="duplicate" if duplicate else event.status, event_id=event.id if event else None), 200


@bp.route("/sms", methods=["POST"])
def sms():
    provider = message_service.provider_for(CHANNEL_SMS)
    _verify(provider)
    result = provider.process_incoming_message(request)
    event, duplicate = message_service.record_webhook(CHANNEL_SMS, provider.name, result, _payload_for_log())
    return _respond(event, duplicate)


@bp.route("/whatsapp", methods=["GET"])
def whatsapp_verify():
    provider = message_service.provider_for(CHANNEL_WHATSAPP)
    challenge = provider.verify_subscription(request.args)
    if challenge is None:
        abort(403)
    return challenge, 200, {"Content-Type": "text/plain"}


@bp.route("/whatsapp", methods=["POST"])
def whatsapp():
    provider = message_service.provider_for(CHANNEL_WHATSAPP)
    _verify(provider)
    result = provider.process_webhook(request)
    event, duplicate = message_service.record_webhook(CHANNEL_WHATSAPP, provider.name, result, _payload_for_log())
    return _respond(event, duplicate)


@bp.route("/email", methods=["POST"])
def email():
    provider = message_service.provider_for(CHANNEL_EMAIL)
    _verify(provider)
    result = provider.parse_webhook(request)
    event, duplicate = message_service.record_webhook(CHANNEL_EMAIL, provider.name, result, _payload_for_log())
    return _respond(event, duplicate)
