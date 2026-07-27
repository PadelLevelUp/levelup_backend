import logging
import os
import sys
from datetime import timedelta

logger = logging.getLogger(__name__)

# Hosts that are known to hold real (remote) data. Used to fail closed: a
# non-production process must never silently migrate one of these unless it
# opted in explicitly.
PRODUCTION_POSTGRES_HOSTS = frozenset(
    {
        "34.77.91.59",  # production Cloud SQL public IP
        "34.78.247.45",  # shared dev/staging VM
        "10.132.0.2",  # production Cloud SQL private IP
    }
)

# An unset POSTGRES_HOST must fail closed to the local machine, never fall
# through to a remote database (PAD-95).
LOCAL_POSTGRES_HOST = "localhost"


def build_database_uri(user, password, host, port, database):
    """Build a SQLAlchemy Postgres URI from its parts."""
    return f"postgresql://{user}:{password}@{host}:{port}/{database}"


def is_production_host(host):
    """True when ``host`` is one of the known production/remote databases."""
    return host in PRODUCTION_POSTGRES_HOSTS


def is_migration_invocation(argv=None):
    """True when this process was started as a ``flask db …`` command.

    The migration guard has to fire during ``create_app`` — by the time
    Alembic's ``env.py`` runs, the scheduler's job store has already opened a
    connection to whatever host was configured.
    """
    argv = sys.argv if argv is None else argv
    args = [a for a in argv[1:] if not a.startswith("-")]
    return "db" in args


def assert_safe_migration_target(host, env=None):
    """Refuse to run migrations against a production host outside production.

    Set ``ALLOW_PRODUCTION_MIGRATIONS=1`` to override deliberately (e.g. a
    one-off manual migration run from a workstation).
    """
    env = env if env is not None else os.getenv("FLASK_ENV", "development")

    if not is_production_host(host) or env == "production":
        return

    if os.getenv("ALLOW_PRODUCTION_MIGRATIONS") == "1":
        logger.warning(
            "Running migrations against production host %s with FLASK_ENV=%s "
            "(allowed by ALLOW_PRODUCTION_MIGRATIONS=1)",
            host,
            env,
        )
        return

    raise RuntimeError(
        f"Refusing to run migrations against production database host {host} "
        f"with FLASK_ENV={env!r}. Set FLASK_ENV=production for a real "
        "deployment, or ALLOW_PRODUCTION_MIGRATIONS=1 to override deliberately."
    )


class Config:
    """Base config (shared defaults).

    The database URI is resolved *per config class* instead of being
    interpolated in this class body — an f-string here would freeze the base
    class's own host, so a subclass overriding it could never affect the
    connection string it inherits (PAD-95).
    """

    SQLALCHEMY_TRACK_MODIFICATIONS = False
    TEMPLATES_AUTO_RELOAD = True
    # Pre-ping validates each connection before use — ensures stale connections
    # (e.g. after the test DB is dropped and recreated) are replaced automatically.
    SQLALCHEMY_ENGINE_OPTIONS = {"pool_pre_ping": True}

    # Database.
    # DEFAULT_POSTGRES_HOST is the host used when POSTGRES_HOST is unset.
    # Subclasses override it and the resolved URI follows (__init_subclass__).
    DEFAULT_POSTGRES_HOST = LOCAL_POSTGRES_HOST

    POSTGRES_USER = os.getenv("POSTGRES_USER", "padel_app_user")
    POSTGRES_PW = os.getenv("POSTGRES_PW")
    POSTGRES_DB = os.getenv("POSTGRES_DB", "padel_app")
    POSTGRES_PORT = os.getenv("POSTGRES_PORT", "5432")

    # Secret key (fallback only for dev)
    SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-key")
    JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY", "dev-jwt-secret")
    JWT_ACCESS_TOKEN_EXPIRES = timedelta(days=30)
    JWT_TOKEN_LOCATION = ["headers", "query_string"]
    JWT_QUERY_STRING_NAME = "token"
    JWT_COOKIE_CSRF_PROTECT = False

    # Email
    MAIL_SERVER = "smtp.gmail.com"
    MAIL_PORT = 465
    MAIL_USE_SSL = True
    MAIL_USERNAME = os.getenv("MAIL_USERNAME", "")
    MAIL_PASSWORD = os.getenv("MAIL_PASSWORD", "")

    # Sessions
    SESSION_PERMANENT = False
    SESSION_TYPE = "filesystem"

    # i18n / localization (default + fallback locale is Portuguese)
    BABEL_DEFAULT_LOCALE = "pt"
    BABEL_DEFAULT_TIMEZONE = "UTC"
    LANGUAGES = ["pt", "en"]

    @classmethod
    def resolve_postgres_host(cls):
        """Host for this config: POSTGRES_HOST, else this class's default."""
        return os.getenv("POSTGRES_HOST") or cls.DEFAULT_POSTGRES_HOST

    @classmethod
    def resolve_database_uri(cls):
        """Build the database URI from the environment at call time."""
        return build_database_uri(
            user=os.getenv("POSTGRES_USER", "padel_app_user"),
            password=os.getenv("POSTGRES_PW"),
            host=cls.resolve_postgres_host(),
            port=os.getenv("POSTGRES_PORT", "5432"),
            database=os.getenv("POSTGRES_DB", "padel_app"),
        )

    @classmethod
    def refresh_database_settings(cls):
        """Recompute the class-level database attributes from the environment.

        ``Flask.config.from_object`` only copies plain uppercase *attributes*
        (``dir()`` skips descriptors defined on a metaclass), so the resolved
        values have to exist as real class attributes.
        """
        cls.POSTGRES_USER = os.getenv("POSTGRES_USER", "padel_app_user")
        cls.POSTGRES_PW = os.getenv("POSTGRES_PW")
        cls.POSTGRES_DB = os.getenv("POSTGRES_DB", "padel_app")
        cls.POSTGRES_PORT = os.getenv("POSTGRES_PORT", "5432")
        cls.POSTGRES_HOST = cls.resolve_postgres_host()
        cls.SQLALCHEMY_DATABASE_URI = cls.resolve_database_uri()

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        cls.refresh_database_settings()


class DevConfig(Config):
    DEBUG = True
    DEFAULT_POSTGRES_HOST = LOCAL_POSTGRES_HOST


class DevConfigProdDB(Config):
    """Explicit — and only — opt-in for pointing local code at production."""

    DEBUG = True
    DEFAULT_POSTGRES_HOST = "34.77.91.59"


class ProdConfig(Config):
    DEBUG = False
    DEFAULT_POSTGRES_HOST = "10.132.0.2"


# __init_subclass__ covers the subclasses; the base class has to resolve itself.
Config.refresh_database_settings()


def get_config_class(env=None):
    """Config class for a FLASK_ENV value (anything non-production is dev)."""
    env = env if env is not None else os.getenv("FLASK_ENV", "development")
    return ProdConfig if env == "production" else DevConfig
