import os
from datetime import datetime, timezone, timedelta
from tempfile import mkdtemp

from flask import Flask
from flask_cors import CORS
from flask_assets import Bundle, Environment
from flask_login import LoginManager
from flask_session import Session
from flask_jwt_extended import JWTManager, get_jwt, get_jwt_identity, create_access_token
from .auth import register_jwt_handlers

from . import cli, mail, modules, sql_db


def create_app(test_config=None):
    app = Flask(__name__, instance_relative_config=True)
    CORS(
        app,
        resources={r"/api/*": {"origins": [
            "http://localhost:8080",
            "http://34.78.247.45",
        ]}},
        supports_credentials=False,
        allow_headers=["Content-Type", "Authorization"],
        expose_headers=["X-New-Token"],
    )

    # Load config
    env = os.getenv("FLASK_ENV", "development")
    if test_config:
        app.config.from_mapping(test_config)
    else:
        from .config import (
            assert_safe_migration_target,
            get_config_class,
            is_migration_invocation,
        )

        config_cls = get_config_class(env)
        # Resolve the database settings from the *selected* config class, and
        # from the environment as it stands at startup, before copying them in
        # — the URI must follow the class's host override (PAD-95).
        config_cls.refresh_database_settings()
        app.config.from_object(config_cls)

        # Never log the URI itself — it carries the password.
        app.logger.info(
            "Config %s (FLASK_ENV=%s) using database %s@%s:%s/%s",
            config_cls.__name__,
            env,
            config_cls.POSTGRES_USER,
            config_cls.POSTGRES_HOST,
            config_cls.POSTGRES_PORT,
            config_cls.POSTGRES_DB,
        )

        # Fail closed before anything opens a connection: never migrate a
        # production database from a non-production process (PAD-95).
        if is_migration_invocation():
            assert_safe_migration_target(config_cls.POSTGRES_HOST, env)

    # Ensure responses aren't cached + refresh nearly-expired JWT tokens
    @app.after_request
    def after_request(response):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Expires"] = 0
        response.headers["Pragma"] = "no-cache"

        try:
            jwt_data = get_jwt()
            exp_timestamp = jwt_data.get("exp")
            if exp_timestamp:
                remaining = (
                    datetime.fromtimestamp(exp_timestamp, timezone.utc)
                    - datetime.now(timezone.utc)
                )
                if remaining < timedelta(days=15):
                    new_token = create_access_token(identity=get_jwt_identity())
                    response.headers["X-New-Token"] = new_token
        except Exception:
            pass

        return response

    with app.app_context():
        modules.startup.add_to_session()

    app.config["SESSION_FILE_DIR"] = mkdtemp()
    Session(app)

    jwt = JWTManager(app)
    register_jwt_handlers(jwt)
    app.jwt = jwt

    mail.mail.init_app(app)

    from flask_babel import Babel
    babel = Babel(app)
    app.babel = babel

    try:
        os.makedirs(app.instance_path)
    except OSError:
        pass

    modules.register_blueprints(app)

    # Assets
    assets = Environment(app)
    scss_bundle = Bundle(
        "styles/scss/main.scss",
        filters="pyscss",
        depends="styles/scss/*.scss",
        output="styles/styles.css",
    )
    assets.register("scss", scss_bundle)

    scss_bundle_backend = Bundle(
        "styles/scss/main_backend.scss",
        filters="pyscss",
        depends="styles/scss/*.scss",
        output="styles/styles_backend.css",
    )
    assets.register("scss_backend", scss_bundle_backend)

    # Login manager
    login_manager = LoginManager(app)
    from .auth import setup_login_manager

    setup_login_manager(login_manager)
    app.login_manager = login_manager

    sql_db.init_db(app)
    cli.register_cli(app)

    from .scheduler import init_scheduler
    init_scheduler(app, test_config=test_config)

    @app.teardown_appcontext
    def shutdown_session(exception=None):
        sql_db.db.session.remove()

    return app
