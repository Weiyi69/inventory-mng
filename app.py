import os
import secrets
from datetime import datetime, timezone

import click
from flask import (
    Flask,
    abort,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_jwt_extended import unset_jwt_cookies
from limits.errors import StorageError

from config import Config
from extensions import jwt, limiter, migrate
from models import AuditLog, RevokedToken, User, db
from routes.auth import auth_bp, web_bp
from routes.dashboard import dashboard_bp
from routes.expenses import expenses_bp
from routes.products import products_bp
from routes.reports import reports_bp
from routes.sales import sales_bp
from routes.shop import shop_bp
from routes.user_admin import admin_bp
from security import audit_event


def create_app(config_object=Config):
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_object(config_object)
    app_environment = str(app.config.get("APP_ENV", "development")).lower()
    if app_environment not in {"development", "test", "production"}:
        raise RuntimeError("APP_ENV must be development, test, or production.")
    if app_environment == "production":
        storage_uri = str(app.config.get("RATELIMIT_STORAGE_URI", "")).lower()
        if (
            not app.config.get("JWT_COOKIE_SECURE")
            or not app.config.get("SESSION_COOKIE_SECURE")
        ):
            raise RuntimeError(
                "Set JWT_COOKIE_SECURE=true so auth and session cookies are "
                "Secure when APP_ENV=production."
            )
        if not storage_uri.startswith(("redis://", "rediss://")):
            raise RuntimeError(
                "Set RATELIMIT_STORAGE_URI to a shared Redis URL when "
                "APP_ENV=production."
            )
    if app_environment == "production" or not app.config.get("TESTING"):
        secret_key = app.config.get("SECRET_KEY")
        jwt_secret_key = app.config.get("JWT_SECRET_KEY")
        if (
            not isinstance(secret_key, str)
            or len(secret_key) < 32
            or not isinstance(jwt_secret_key, str)
            or len(jwt_secret_key) < 32
            or secret_key == jwt_secret_key
        ):
            raise RuntimeError(
                "Set distinct SECRET_KEY and JWT_SECRET_KEY values of at least "
                "32 characters in the environment or .env file."
            )
    os.makedirs(app.instance_path, exist_ok=True)

    @app.context_processor
    def inject_current_time():
        return {"now": lambda: datetime.now(timezone.utc)}

    @app.context_processor
    def inject_auth_context():
        return {
            "current_user": getattr(g, "current_user", None),
            "csrf_token": getattr(g, "csrf_token", ""),
            "cart_count": getattr(g, "cart_count", 0),
        }

    db.init_app(app)
    migrate.init_app(app, db, compare_type=True)
    limiter.init_app(app)
    jwt.init_app(app)

    @jwt.token_in_blocklist_loader
    def is_token_revoked(_jwt_header, jwt_payload):
        try:
            user_id = int(jwt_payload["sub"])
        except (KeyError, TypeError, ValueError):
            return True
        user = db.session.get(User, user_id)
        if (
            user is None
            or not user.is_active
            or jwt_payload.get("auth_version") != user.auth_version
        ):
            return True
        return db.session.query(
            db.exists().where(RevokedToken.jti == jwt_payload["jti"])
        ).scalar()

    def authentication_error(message, status=401):
        if request.is_json or request.path.startswith("/auth/"):
            return jsonify(error=message), status
        response = redirect(url_for("web.login", next=request.full_path.rstrip("?")))
        unset_jwt_cookies(response)
        return response

    @jwt.unauthorized_loader
    def missing_authentication(_reason):
        return authentication_error("Please sign in to continue.")

    @jwt.expired_token_loader
    def expired_authentication(_header, _payload):
        return authentication_error("Your session expired. Please sign in again.")

    @jwt.revoked_token_loader
    def revoked_authentication(_header, _payload):
        return authentication_error("Your session is no longer active.")

    @jwt.invalid_token_loader
    def invalid_authentication(_reason):
        return authentication_error("Your session is invalid. Please sign in again.")

    @jwt.token_verification_failed_loader
    def failed_token_verification(_header, _payload):
        return authentication_error("Your session could not be verified.")

    @jwt.needs_fresh_token_loader
    def fresh_token_required(_header, _payload):
        return authentication_error("Please sign in again to continue.")

    @app.errorhandler(403)
    def access_denied(error):
        if request.is_json:
            return jsonify(error="You do not have permission to access this resource."), 403
        return render_template("access_denied.html", error=error), 403

    @app.errorhandler(401)
    def unauthenticated(error):
        return authentication_error("Please sign in to continue.")

    @app.errorhandler(StorageError)
    def rate_limit_storage_unavailable(_error):
        if request.endpoint == "web.login":
            email = request.form.get("email", "").strip().lower()
            if len(email) > 254:
                email = ""
            return render_template(
                "login.html",
                csrf_token=session.setdefault(
                    "login_csrf",
                    secrets.token_urlsafe(32),
                ),
                email=email,
                field_errors={},
                error=(
                    "Sign-in is temporarily unavailable. "
                    "Please try again later."
                ),
            ), 503
        return jsonify(
            error="This request is temporarily unavailable. Please try again later."
        ), 503

    @app.after_request
    def secure_response(response):
        if getattr(g, "current_user", None) is not None:
            response.headers["Cache-Control"] = "no-store"
            response.headers["Vary"] = "Cookie"
        elif request.endpoint in {
            "products.index",
            "products.view_product",
            "shop.cart",
            "shop.checkout",
            "shop.place_order",
            "shop.order_confirmation",
        } or (
            request.endpoint
            and request.endpoint.startswith("shop.")
            and not request.endpoint.startswith("shop.admin_")
        ):
            response.headers["Cache-Control"] = "private, no-store"
            response.headers["Vary"] = "Cookie"
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("X-Frame-Options", "DENY")
        return response

    @app.cli.command("create-admin")
    def create_admin():
        """Interactively create the first administrator account."""
        if User.query.filter_by(role="admin").first():
            raise click.ClickException(
                "An administrator already exists; use User Management instead."
            )
        name = click.prompt("Administrator name").strip()
        email = click.prompt("Administrator email").strip().lower()
        password = click.prompt("Password (minimum 12 characters)", hide_input=True)
        confirmation = click.prompt("Confirm password", hide_input=True)
        if not name or len(name) > 120:
            raise click.ClickException("Name must contain 1 to 120 characters.")
        if len(email) > 254 or "@" not in email:
            raise click.ClickException("Enter a valid email address.")
        if len(password) < 12:
            raise click.ClickException("Password must contain at least 12 characters.")
        if password != confirmation:
            raise click.ClickException("Passwords do not match.")
        user = User(name=name, email=email, role="admin", is_active=True)
        user.set_password(password)
        db.session.add(user)
        db.session.flush()
        audit_event("admin_bootstrapped", target=user, details={"email": email})
        db.session.commit()
        click.echo(f"Administrator account created for {email}.")

    app.register_blueprint(auth_bp)
    app.register_blueprint(web_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(products_bp)
    app.register_blueprint(sales_bp)
    app.register_blueprint(expenses_bp)
    app.register_blueprint(reports_bp)
    app.register_blueprint(shop_bp)

    return app


app = create_app()


if __name__ == "__main__":
    app.run(debug=app.config["APP_ENV"] != "production")
