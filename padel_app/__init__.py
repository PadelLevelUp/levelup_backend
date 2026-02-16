import os
from tempfile import mkdtemp

from flask import Flask
from flask_cors import CORS
from flask_assets import Bundle, Environment
from flask_login import LoginManager
from flask_session import Session
from flask_jwt_extended import JWTManager
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
    )

    # Load config
    env = os.getenv("FLASK_ENV", "development")
    if test_config:
        app.config.from_mapping(test_config)
    elif env == "production":
        from .config import ProdConfig

        app.config.from_object(ProdConfig)
    else:
        from .config import DevConfig

        app.config.from_object(DevConfig)

    # Ensure responses aren't cached
    @app.after_request
    def after_request(response):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Expires"] = 0
        response.headers["Pragma"] = "no-cache"
        return response

    with app.app_context():
        modules.startup.add_to_session()

    app.config["SESSION_FILE_DIR"] = mkdtemp()
    Session(app)

    jwt = JWTManager(app)
    register_jwt_handlers(jwt)
    app.jwt = jwt

    mail.mail.init_app(app)

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

    @app.teardown_appcontext
    def shutdown_session(exception=None):
        sql_db.db.session.remove()

    return app
