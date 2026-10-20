import hmac
import secrets
from datetime import datetime, timezone
from urllib.parse import urlsplit

from flask import (
    Blueprint,
    abort,
    current_app,
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


def _login_error(message):
    return render_template(
        "login.html",
        csrf_token=session.setdefault("login_csrf", secrets.token_urlsafe(32)),
        error=message,
    ), 401


def _safe_next(value):
    if not value:
        return url_for("dashboard.index")
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
        return url_for("dashboard.index")
    return value


@web_bp.route("/login", methods=["GET", "POST"])
@limiter.limit("5 per 15 minutes", methods=["POST"])
def login():
    if request.method == "GET":
        csrf_token = session.setdefault("login_csrf", secrets.token_urlsafe(32))
        return render_template("login.html", csrf_token=csrf_token, error=None)

    expected = session.get("login_csrf", "")
    supplied = request.form.get("csrf_token", "")
    if not expected or not hmac.compare_digest(expected, supplied):
        abort(400, description="Your sign-in form expired. Please try again.")
    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")
    user = User.query.filter_by(email=email).first()
    if (
        user is None
        or not user.is_active
        or not user.verify_password(password)
    ):
        audit_event("login_failed", details={"email": email[:254]})
        db.session.commit()
        session.pop("login_csrf", None)
        return _login_error("Email or password is incorrect.")

    access_token = create_access_token(
        identity=str(user.id),
        additional_claims={"role": user.role, "auth_version": user.auth_version},
    )
    refresh_token = create_refresh_token(
        identity=str(user.id),
        additional_claims={"role": user.role, "auth_version": user.auth_version},
    )
    audit_event("login_succeeded", actor=user)
    db.session.commit()
    session.clear()
    if user.role == "admin":
        destination = _safe_next(
            request.args.get("next") or request.form.get("next")
        )
    else:
        destination = url_for("products.index")
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
    response = redirect(url_for("web.login"))
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
