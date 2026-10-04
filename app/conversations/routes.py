from flask import Blueprint, abort, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user
from sqlalchemy import or_
from sqlalchemy.orm import joinedload

from app import audit
from app.conversations.services import ReplyError, create_reply, timeline, whatsapp_window_open
from app.extensions import db
from app.messaging.service import message_service
from app.models import (
    CHANNELS,
    Client,
    Conversation,
    ConversationNote,
    ConversationStatus,
    Message,
    User,
)
from app.permissions import Permission, permission_required
from app.tasks import enqueue

bp = Blueprint("conversations", __name__, url_prefix="/inbox")

VIEW_FILTERS = {
    "active": "Open & pending",
    "mine": "Assigned to me",
    "unassigned": "Unassigned",
    "resolved": "Resolved",
    "closed": "Closed",
    "all": "Everything",
}


def _inbox_query(args):
    view = args.get("view", "active")
    channel = args.get("channel", "")
    term = (args.get("q") or "").strip()
    q = Conversation.query.options(joinedload(Conversation.client), joinedload(Conversation.assigned_to)).filter(
        Conversation.last_message_at.isnot(None)
    )
    if view == "active":
        q = q.filter(Conversation.status.in_([ConversationStatus.OPEN, ConversationStatus.PENDING]))
    elif view == "mine":
        q = q.filter(Conversation.assigned_to_id == current_user.id,
                     Conversation.status.in_([ConversationStatus.OPEN, ConversationStatus.PENDING]))
    elif view == "unassigned":
        q = q.filter(Conversation.assigned_to_id.is_(None),
                     Conversation.status.in_([ConversationStatus.OPEN, ConversationStatus.PENDING]))
    elif view in (ConversationStatus.RESOLVED, ConversationStatus.CLOSED):
        q = q.filter(Conversation.status == view)
    if channel in CHANNELS:
        q = q.filter(Conversation.channel == channel)
    if term:
        like = f"%{term}%"
        q = q.join(Client).filter(or_(Client.full_name.ilike(like), Client.company.ilike(like),
                                      Client.phone.ilike(like), Client.email.ilike(like)))
    unread_first = (Conversation.unread_count > 0).desc()
    return q.order_by(unread_first, Conversation.last_message_at.desc()), view, channel, term


def _list_context(selected=None):
    q, view, channel, term = _inbox_query(request.args)
    page = request.args.get("page", 1, type=int)
    conversations = q.paginate(page=page, per_page=25, error_out=False)
    return {
        "conversations": conversations,
        "view": view,
        "channel": channel,
        "term": term,
        "views": VIEW_FILTERS,
        "selected": selected,
        "list_params": {k: v for k, v in {"view": view, "channel": channel, "q": term}.items() if v},
    }


@bp.route("/")
@permission_required(Permission.CONVERSATIONS_VIEW)
def inbox():
    return render_template("conversations/inbox.html", **_list_context())


@bp.route("/c/<int:conversation_id>")
@permission_required(Permission.CONVERSATIONS_VIEW)
def detail(conversation_id):
    conversation = db.session.get(Conversation, conversation_id) or abort(404)
    if conversation.unread_count:
        conversation.unread_count = 0
        db.session.commit()
    agents = (
        User.query.filter(User.is_active_user.is_(True)).order_by(User.full_name).all()
    )
    agents = [a for a in agents if a.can(Permission.CONVERSATIONS_REPLY)]
    cc = conversation.client.channel(conversation.channel)
    latest = conversation.messages.order_by(Message.id.desc()).first()
    return render_template(
        "conversations/detail.html",
        conversation=conversation,
        items=timeline(conversation),
        agents=agents,
        statuses=ConversationStatus,
        contact=cc,
        window_open=whatsapp_window_open(conversation),
        latest_id=latest.id if latest else 0,
        **_list_context(selected=conversation),
    )


@bp.route("/c/<int:conversation_id>/poll")
@permission_required(Permission.CONVERSATIONS_VIEW)
def poll(conversation_id):
    conversation = db.session.get(Conversation, conversation_id) or abort(404)
    latest = conversation.messages.order_by(Message.id.desc()).first()
    statuses = {m.id: m.status for m in conversation.messages.order_by(Message.id.desc()).limit(20)}
    return jsonify(latest_id=latest.id if latest else 0, statuses=statuses)


@bp.route("/c/<int:conversation_id>/reply", methods=["POST"])
@permission_required(Permission.CONVERSATIONS_REPLY)
def reply(conversation_id):
    conversation = db.session.get(Conversation, conversation_id) or abort(404)
    try:
        message = create_reply(conversation, current_user, request.form.get("content"), request.form.get("subject"))
    except ReplyError as exc:
        flash(str(exc), "error")
        return redirect(url_for("conversations.detail", conversation_id=conversation.id))
    new_status = request.form.get("then_status")
    if new_status in ConversationStatus.ALL and new_status != conversation.status:
        conversation.status = new_status
    db.session.commit()
    enqueue("send_message", message.id)
    flash("Reply sent." if new_status not in ConversationStatus.ALL else
          f"Reply sent and conversation marked {ConversationStatus.LABELS[new_status].lower()}.", "success")
    return redirect(url_for("conversations.detail", conversation_id=conversation.id))


@bp.route("/c/<int:conversation_id>/assign", methods=["POST"])
@permission_required(Permission.CONVERSATIONS_ASSIGN)
def assign(conversation_id):
    conversation = db.session.get(Conversation, conversation_id) or abort(404)
    user_id = request.form.get("user_id", type=int)
    if user_id:
        agent = db.session.get(User, user_id)
        if agent is None or not agent.can(Permission.CONVERSATIONS_REPLY):
            abort(400)
        conversation.assigned_to_id = agent.id
        message = f"Assigned to {agent.full_name}."
    else:
        conversation.assigned_to_id = None
        message = "Conversation unassigned."
    audit.record("conversation.assigned", "conversation", conversation.id, {"user_id": user_id or None})
    db.session.commit()
    flash(message, "success")
    return redirect(url_for("conversations.detail", conversation_id=conversation.id))


@bp.route("/c/<int:conversation_id>/status", methods=["POST"])
@permission_required(Permission.CONVERSATIONS_REPLY)
def set_status(conversation_id):
    conversation = db.session.get(Conversation, conversation_id) or abort(404)
    status = request.form.get("status")
    if status not in ConversationStatus.ALL:
        abort(400)
    old = conversation.status
    conversation.status = status
    audit.record("conversation.status_changed", "conversation", conversation.id, {"from": old, "to": status})
    db.session.commit()
    flash(f"Conversation marked {ConversationStatus.LABELS[status].lower()}.", "success")
    return redirect(url_for("conversations.detail", conversation_id=conversation.id))


@bp.route("/c/<int:conversation_id>/note", methods=["POST"])
@permission_required(Permission.CONVERSATIONS_REPLY)
def add_note(conversation_id):
    conversation = db.session.get(Conversation, conversation_id) or abort(404)
    content = (request.form.get("content") or "").strip()
    if not content:
        flash("Write a note first.", "error")
    else:
        note = ConversationNote(conversation=conversation, user_id=current_user.id, content=content[:5000])
        db.session.add(note)
        db.session.flush()
        audit.record("conversation.note_added", "conversation", conversation.id, {"note_id": note.id})
        db.session.commit()
        flash("Internal note added.", "success")
    return redirect(url_for("conversations.detail", conversation_id=conversation.id))


@bp.route("/start/<int:client_id>/<channel>", methods=["POST"])
@permission_required(Permission.CONVERSATIONS_REPLY)
def start(client_id, channel):
    client = db.session.get(Client, client_id) or abort(404)
    if channel not in CHANNELS:
        abort(404)
    conversation = message_service.get_or_create_conversation(client, channel)
    db.session.commit()
    return redirect(url_for("conversations.detail", conversation_id=conversation.id))
