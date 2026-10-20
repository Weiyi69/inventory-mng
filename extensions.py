from flask_jwt_extended import JWTManager
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_migrate import Migrate

jwt = JWTManager()
limiter = Limiter(key_func=get_remote_address)
migrate = Migrate()
