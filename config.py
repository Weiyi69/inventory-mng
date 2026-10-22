import os
from datetime import timedelta

from dotenv import load_dotenv

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))


class Config:
    APP_ENV = os.environ.get("APP_ENV", "development").strip().lower()
    SECRET_KEY = os.environ.get("SECRET_KEY")
    JWT_SECRET_KEY = os.environ.get("JWT_SECRET_KEY")
    JWT_TOKEN_LOCATION = ("cookies",)
    JWT_ACCESS_TOKEN_EXPIRES = timedelta(minutes=15)
    JWT_REFRESH_TOKEN_EXPIRES = timedelta(days=7)
    JWT_COOKIE_SECURE = os.environ.get("JWT_COOKIE_SECURE", "false").lower() == "true"
    JWT_COOKIE_HTTPONLY = True
    JWT_COOKIE_SAMESITE = "Lax"
    JWT_COOKIE_CSRF_PROTECT = True
    JWT_CSRF_CHECK_FORM = True
    JWT_ACCESS_COOKIE_PATH = "/"
    JWT_REFRESH_COOKIE_PATH = "/auth"
    JWT_ACCESS_CSRF_COOKIE_PATH = "/"
    JWT_REFRESH_CSRF_COOKIE_PATH = "/"
    JWT_CSRF_METHODS = ["POST", "PUT", "PATCH", "DELETE"]
    RATELIMIT_STORAGE_URI = os.environ.get("RATELIMIT_STORAGE_URI", "memory://")
    RATELIMIT_SWALLOW_ERRORS = False
    RATELIMIT_HEADERS_ENABLED = True
    RATELIMIT_DEFAULT = "300 per hour"
    LOGIN_IP_RATE_LIMIT = os.environ.get(
        "LOGIN_IP_RATE_LIMIT",
        "5 per 15 minutes",
    )
    LOGIN_IDENTIFIER_RATE_LIMIT = os.environ.get(
        "LOGIN_IDENTIFIER_RATE_LIMIT",
        "10 per hour",
    )
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SECURE = JWT_COOKIE_SECURE
    SESSION_COOKIE_SAMESITE = "Lax"
    MAX_CONTENT_LENGTH = 6 * 1024 * 1024
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "DATABASE_URL",
        f"sqlite:///{os.path.join(BASE_DIR, 'instance', 'inventory.db')}",
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
