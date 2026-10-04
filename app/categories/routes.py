from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_wtf import FlaskForm
from wtforms import BooleanField, SelectField, StringField
from wtforms.validators import DataRequired, Length, Optional

from app import audit
from app.cli import slugify
from app.clients.services import category_counts, tag_counts
from app.extensions import db
from app.models import Category, Tag
from app.permissions import Permission, permission_required

bp = Blueprint("categories", __name__, url_prefix="/categories")


class CategoryForm(FlaskForm):
    name = StringField("Name", validators=[DataRequired(), Length(max=80)])
    description = StringField("Description", validators=[Optional(), Length(max=255)])
    color = SelectField("Colour", choices=[(c, c.title()) for c in Category.COLORS])
    is_featured = BooleanField("Show on dashboard")


class TagForm(FlaskForm):
    name = StringField("Tag", validators=[DataRequired(), Length(max=60)])


@bp.route("/")
@permission_required(Permission.CLIENTS_VIEW)
def index():
    return render_template(
        "categories/index.html",
        categories=Category.query.order_by(Category.name).all(),
        tags=Tag.query.order_by(Tag.name).all(),
        category_counts=category_counts(),
        tag_counts=tag_counts(),
        form=CategoryForm(),
        tag_form=TagForm(),
    )


def _name_taken(model, name, exclude_id=None):
    q = model.query.filter(db.func.lower(model.name) == name.lower())
    if exclude_id:
        q = q.filter(model.id != exclude_id)
    return q.first() is not None


@bp.route("/new", methods=["POST"])
@permission_required(Permission.CATEGORIES_MANAGE)
def create():
    form = CategoryForm()
    if form.validate_on_submit():
        name = form.name.data.strip()
        if _name_taken(Category, name) or Category.query.filter_by(slug=slugify(name)).first():
            flash(f"A category called “{name}” already exists.", "error")
        else:
            category = Category(name=name, slug=slugify(name), description=form.description.data or None,
                                color=form.color.data, is_featured=form.is_featured.data)
            db.session.add(category)
            db.session.flush()
            audit.record("category.created", "category", category.id, {"name": name})
            db.session.commit()
            flash(f"Category “{name}” created.", "success")
    else:
        flash("Please give the category a name.", "error")
    return redirect(url_for("categories.index"))


@bp.route("/<int:category_id>/edit", methods=["GET", "POST"])
@permission_required(Permission.CATEGORIES_MANAGE)
def edit(category_id):
    category = db.session.get(Category, category_id) or abort(404)
    form = CategoryForm(obj=category)
    if form.validate_on_submit():
        name = form.name.data.strip()
        slug = slugify(name)
        if _name_taken(Category, name, category.id) or Category.query.filter(
            Category.slug == slug, Category.id != category.id
        ).first():
            form.name.errors.append("Another category already uses this name.")
        else:
            category.name, category.slug = name, slug
            category.description = form.description.data or None
            category.color = form.color.data
            category.is_featured = form.is_featured.data
            audit.record("category.updated", "category", category.id)
            db.session.commit()
            flash("Category updated.", "success")
            return redirect(url_for("categories.index"))
    return render_template("categories/edit.html", form=form, category=category,
                           count=category_counts().get(category.id, 0))


@bp.route("/<int:category_id>/delete", methods=["POST"])
@permission_required(Permission.CATEGORIES_MANAGE)
def delete(category_id):
    category = db.session.get(Category, category_id) or abort(404)
    name = category.name
    audit.record("category.deleted", "category", category.id, {"name": name})
    db.session.delete(category)
    db.session.commit()
    flash(f"Category “{name}” deleted. Clients were not affected.", "success")
    return redirect(url_for("categories.index"))


@bp.route("/tags/new", methods=["POST"])
@permission_required(Permission.CATEGORIES_MANAGE)
def tag_create():
    form = TagForm()
    if form.validate_on_submit():
        name = form.name.data.strip().lstrip("#")
        if _name_taken(Tag, name):
            flash(f"Tag #{name} already exists.", "error")
        else:
            tag = Tag(name=name)
            db.session.add(tag)
            db.session.flush()
            audit.record("tag.created", "tag", tag.id, {"name": name})
            db.session.commit()
            flash(f"Tag #{name} created.", "success")
    return redirect(url_for("categories.index") + "#tags")


@bp.route("/tags/<int:tag_id>/rename", methods=["POST"])
@permission_required(Permission.CATEGORIES_MANAGE)
def tag_rename(tag_id):
    tag = db.session.get(Tag, tag_id) or abort(404)
    name = (request.form.get("name") or "").strip().lstrip("#")[:60]
    if not name or _name_taken(Tag, name, tag.id):
        flash("Choose a different, unused tag name.", "error")
    else:
        tag.name = name
        audit.record("tag.updated", "tag", tag.id, {"name": name})
        db.session.commit()
        flash("Tag renamed.", "success")
    return redirect(url_for("categories.index") + "#tags")


@bp.route("/tags/<int:tag_id>/delete", methods=["POST"])
@permission_required(Permission.CATEGORIES_MANAGE)
def tag_delete(tag_id):
    tag = db.session.get(Tag, tag_id) or abort(404)
    name = tag.name
    audit.record("tag.deleted", "tag", tag.id, {"name": name})
    db.session.delete(tag)
    db.session.commit()
    flash(f"Tag #{name} deleted.", "success")
    return redirect(url_for("categories.index") + "#tags")
