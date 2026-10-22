import hmac
import re
import secrets
from datetime import datetime, timezone
from urllib.parse import urlsplit

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_jwt_extended import (
    create_access_token,
    create_refresh_token,
    decode_token,
    get_jwt,
    get_jwt_identity,
    get_csrf_token,
    jwt_required,
    set_access_cookies,
    set_refresh_cookies,
    unset_jwt_cookies,
)

from extensions import limiter
from flask_jwt_extended.exceptions import JWTExtendedException
from models import AuditLog, RevokedToken, User, db
from security import audit_event

auth_bp = Blueprint("auth", __name__, url_prefix="/auth")
web_bp = Blueprint("web", __name__)
_EMAIL_PATTERN = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
_dummy_password_hasher = PasswordHasher()
_DUMMY_PASSWORD_HASH = _dummy_password_hasher.hash(secrets.token_urlsafe(32))


def _login_error(message, email="", field_errors=None, status=401):
    return render_template(
        "login.html",
        csrf_token=session.setdefault("login_csrf", secrets.token_urlsafe(32)),
        email=email,
        field_errors=field_errors or {},
        error=message,
    ), status


def _login_identifier_key():
    identifier = request.form.get("email", "").strip().lower()[:254]
    if not identifier:
        identifier = f"<missing>:{request.remote_addr or ''}"
    digest = hmac.new(
        current_app.config["SECRET_KEY"].encode("utf-8"),
        identifier.encode("utf-8"),
        "sha256",
    ).hexdigest()
    return f"login:{digest}"


def _login_rate_limit_response(_limit):
    email = request.form.get("email", "").strip().lower()
    if len(email) > 254:
        email = ""
    return current_app.response_class(
        render_template(
            "login.html",
            csrf_token=session.setdefault(
                "login_csrf",
                secrets.token_urlsafe(32),
            ),
            email=email,
            field_errors={},
            error="Too many sign-in attempts. Please wait before trying again.",
        ),
        status=429,
    )


def _verify_dummy_password(password):
    try:
        _dummy_password_hasher.verify(_DUMMY_PASSWORD_HASH, password)
    except (InvalidHashError, VerifyMismatchError):
        pass
    return False


def _safe_next(value, fallback=None):
    if not value:
        return fallback or url_for("dashboard.index")
    parsed = urlsplit(value)
    if (
        parsed.scheme
        or parsed.netloc
        or not value.startswith("/")
        or value.startswith("//")
        or "\\" in value
        or "\r" in value
        or "\n" in value
    ):
        return fallback or url_for("dashboard.index")
    return value


@web_bp.route("/login", methods=["GET", "POST"])
@limiter.limit(
    lambda: current_app.config["LOGIN_IDENTIFIER_RATE_LIMIT"],
    key_func=_login_identifier_key,
    methods=["POST"],
    on_breach=_login_rate_limit_response,
)
@limiter.limit(
    lambda: current_app.config["LOGIN_IP_RATE_LIMIT"],
    methods=["POST"],
    on_breach=_login_rate_limit_response,
)
def login():
    if request.method == "GET":
        csrf_token = session.setdefault("login_csrf", secrets.token_urlsafe(32))
        return render_template(
            "login.html",
            csrf_token=csrf_token,
            email="",
            field_errors={},
            error=None,
        )

    expected = session.get("login_csrf", "")
    supplied = request.form.get("csrf_token", "")
    if not expected or not hmac.compare_digest(expected, supplied):
        return _login_error(
            "Your sign-in form expired. Please try again.",
            status=400,
        )
    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")
    safe_email = email if len(email) <= 254 else ""
    field_errors = {}
    if not email:
        field_errors["email"] = "Enter your email address."
    elif len(email) > 254 or not _EMAIL_PATTERN.fullmatch(email):
        field_errors["email"] = "Enter a valid email address."
    if not password:
        field_errors["password"] = "Enter your password."
    if field_errors:
        return _login_error(
            "Please correct the highlighted fields.",
            email=safe_email,
            field_errors=field_errors,
            status=400,
        )

    user = User.query.filter_by(email=email).first()
    password_valid = (
        user.verify_password(password)
        if user is not None and user.is_active
        else _verify_dummy_password(password)
    )
    if (
        user is None
        or not user.is_active
        or not password_valid
    ):
        audit_event("login_failed")
        db.session.commit()
        session.pop("login_csrf", None)
        return _login_error("Email or password is incorrect.", email=safe_email)

    access_token = create_access_token(
        identity=str(user.id),
        additional_claims={"role": user.role, "auth_version": user.auth_version},
    )
    refresh_token = create_refresh_token(
        identity=str(user.id),
        additional_claims={"role": user.role, "auth_version": user.auth_version},
    )
    merge_notice = None
    if user.role == "user":
        from routes.shop import merge_guest_cart

        merge_notice = merge_guest_cart(user, session.get("cart", {}))
    audit_event("login_succeeded", actor=user)
    db.session.commit()
    session.clear()
    destination = _safe_next(
        request.args.get("next") or request.form.get("next"),
        fallback=(
            url_for("dashboard.index")
            if user.role == "admin"
            else url_for("products.index")
        ),
    )
    if merge_notice:
        flash(merge_notice, "success")
    response = redirect(destination)
    set_access_cookies(response, access_token)
    set_refresh_cookies(response, refresh_token)
    return response


@auth_bp.post("/refresh")
@jwt_required(refresh=True)
def refresh():
    user = db.session.get(User, int(get_jwt_identity()))
    if user is None or not user.is_active:
        abort(401)
    claims = get_jwt()
    if claims.get("auth_version") != user.auth_version:
        abort(401)
    expires_at = datetime.fromtimestamp(claims["exp"], tz=timezone.utc).replace(tzinfo=None)
    db.session.add(RevokedToken(
        jti=claims["jti"],
        token_type="refresh",
        expires_at=expires_at,
    ))
    db.session.query(RevokedToken).filter(
        RevokedToken.expires_at
        < datetime.now(timezone.utc).replace(tzinfo=None)
    ).delete(synchronize_session=False)
    access_token = create_access_token(
        identity=str(user.id),
        additional_claims={"role": user.role, "auth_version": user.auth_version},
    )
    refresh_token = create_refresh_token(
        identity=str(user.id),
        additional_claims={"role": user.role, "auth_version": user.auth_version},
    )
    db.session.add(AuditLog(
        actor_id=user.id,
        event="token_refreshed",
        ip_address=request.remote_addr,
    ))
    db.session.commit()
    response = jsonify({
        "message": "Session refreshed.",
        "csrf_token": get_csrf_token(access_token),
    })
    set_access_cookies(response, access_token)
    set_refresh_cookies(response, refresh_token)
    return response


@auth_bp.post("/logout")
@jwt_required()
def logout():
    claims = get_jwt()
    expires_at = datetime.fromtimestamp(claims["exp"], tz=timezone.utc).replace(tzinfo=None)
    db.session.add(RevokedToken(
        jti=claims["jti"],
        token_type=claims["type"],
        expires_at=expires_at,
    ))
    user = db.session.get(User, int(get_jwt_identity()))
    refresh_encoded = request.cookies.get(
        current_app.config["JWT_REFRESH_COOKIE_NAME"]
    )
    if refresh_encoded:
        try:
            refresh_claims = decode_token(refresh_encoded, allow_expired=True)
        except JWTExtendedException:
            refresh_claims = None
        if (
            refresh_claims
            and refresh_claims.get("type") == "refresh"
            and refresh_claims.get("sub") == str(user.id)
        ):
            refresh_expires = datetime.fromtimestamp(
                refresh_claims["exp"],
                tz=timezone.utc,
            ).replace(tzinfo=None)
            db.session.add(RevokedToken(
                jti=refresh_claims["jti"],
                token_type="refresh",
                expires_at=refresh_expires,
            ))
    audit_event("logout", actor=user)
    db.session.commit()
    session.clear()
    response = redirect(url_for("products.index"))
    unset_jwt_cookies(response)
    return response


@web_bp.get("/profile")
def profile():
    from flask_jwt_extended import verify_jwt_in_request

    verify_jwt_in_request()
    user = db.session.get(User, int(get_jwt_identity()))
    if user is None or not user.is_active:
        abort(401)
    if user.role != "admin":
        return redirect(url_for("products.index"))
    g.current_user = user
    g.csrf_token = get_jwt().get("csrf", "")
    return render_template("profile.html")
