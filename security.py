from functools import wraps
import secrets

from flask import (
    abort,
    g,
    has_request_context,
    make_response,
    redirect,
    request,
    session,
    url_for,
)
from flask_jwt_extended import (
    get_jwt,
    get_jwt_identity,
    unset_jwt_cookies,
    verify_jwt_in_request,
)
from flask_jwt_extended.exceptions import JWTDecodeError
from jwt.exceptions import ExpiredSignatureError
from sqlalchemy import func

from models import Cart, CartItem, User, db


def optional_identity():
    try:
        verify_jwt_in_request(optional=True)
    except (ExpiredSignatureError, JWTDecodeError):
        g._jwt_extended_jwt = {}
        g._jwt_extended_jwt_header = {}
        g._jwt_extended_jwt_user = {"loaded_user": None}
        g._jwt_extended_jwt_location = None
        g.discard_invalid_auth_cookies = True
    return get_jwt_identity()


def discard_invalid_auth_cookies(response):
    if getattr(g, "discard_invalid_auth_cookies", False):
        response = make_response(response)
        unset_jwt_cookies(response)
    return response


def catalog_access(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        identity = optional_identity()
        user = None
        if identity is not None:
            try:
                user = db.session.get(User, int(identity))
            except (TypeError, ValueError):
                abort(401)
            if user is None or not user.is_active:
                abort(401)
            if user.role != "admin" and not user.allows("products"):
                abort(403)

        g.current_user = user
        g.csrf_token = (
            get_jwt().get("csrf", "")
            if user
            else session.setdefault("guest_csrf", secrets.token_urlsafe(32))
        )
        if user is None:
            cart = session.get("cart", {})
            g.cart_count = (
                sum(
                    value
                    for value in cart.values()
                    if isinstance(value, int) and value > 0
                )
                if isinstance(cart, dict)
                else 0
            )
        elif user.role == "user":
            g.cart_count = db.session.query(
                func.coalesce(func.sum(CartItem.quantity), 0)
            ).join(Cart).filter(Cart.user_id == user.id).scalar()
        else:
            g.cart_count = 0
        return discard_invalid_auth_cookies(view(*args, **kwargs))

    return wrapped


def access_required(page=None, action=None, admin=False):
    def decorate(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            verify_jwt_in_request()
            try:
                user_id = int(get_jwt_identity())
            except (TypeError, ValueError):
                abort(401)
            user = db.session.get(User, user_id)
            if user is None or not user.is_active:
                abort(401)
            g.current_user = user
            g.csrf_token = get_jwt().get("csrf", "")
            if admin and user.role != "admin":
                abort(403)
            if user.role != "admin":
                if page and not user.allows(page):
                    if page != "products":
                        return redirect(url_for("products.index"))
                    abort(403)
                if action and not user.allows(action):
                    abort(403)
            return view(*args, **kwargs)

        return wrapped

    return decorate


def audit_event(event, actor=None, target=None, details=None):
    from models import AuditLog

    db.session.add(AuditLog(
        actor_id=actor.id if actor else None,
        event=event,
        target_user_id=target.id if target else None,
        details=details or {},
        ip_address=request.remote_addr if has_request_context() else None,
    ))
