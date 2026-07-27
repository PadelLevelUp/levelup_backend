"""Regression tests for PAD-95.

The base ``Config`` class used to interpolate ``SQLALCHEMY_DATABASE_URI`` inside
its own class body, which meant subclass overrides of ``POSTGRES_HOST`` never
reached the connection string: ``DevConfig`` inherited the base URI verbatim and
silently pointed at the production database whenever ``POSTGRES_HOST`` was unset.

These tests pin the two properties that matter:

* every config class resolves the URI from *its own* host, and
* an unset ``POSTGRES_HOST`` fails closed to ``localhost`` instead of falling
  through to a production address.

They never open a database connection — only the resolved strings are asserted.
"""

import importlib
import os

import pytest

import padel_app.config as config_module

DB_ENV_KEYS = (
    "POSTGRES_HOST",
    "POSTGRES_USER",
    "POSTGRES_PW",
    "POSTGRES_DB",
    "POSTGRES_PORT",
    "FLASK_ENV",
)


@pytest.fixture
def load_config():
    """Reload padel_app.config with a controlled database environment."""
    original_env = os.environ.copy()

    def _load(**env):
        for key in DB_ENV_KEYS:
            os.environ.pop(key, None)
        os.environ.update(env)
        return importlib.reload(config_module)

    yield _load

    os.environ.clear()
    os.environ.update(original_env)
    importlib.reload(config_module)


def _host_of(uri):
    """Extract the host portion of a postgresql:// URI without a DB driver."""
    return uri.split("@", 1)[1].split(":", 1)[0]


# --------------------------------------------------------------------------
# Fail-closed defaults
# --------------------------------------------------------------------------


def test_dev_config_resolves_to_localhost_when_host_unset(load_config):
    config = load_config()

    assert config.DevConfig.resolve_postgres_host() == "localhost"
    assert _host_of(config.DevConfig.SQLALCHEMY_DATABASE_URI) == "localhost"


def test_base_config_fails_closed_to_localhost_when_host_unset(load_config):
    config = load_config()

    assert config.Config.resolve_postgres_host() == "localhost"
    assert _host_of(config.Config.SQLALCHEMY_DATABASE_URI) == "localhost"


@pytest.mark.parametrize("config_name", ["Config", "DevConfig"])
def test_dev_paths_never_point_at_production_when_host_unset(
    load_config, config_name
):
    config = load_config()
    config_cls = getattr(config, config_name)

    host = _host_of(config_cls.SQLALCHEMY_DATABASE_URI)
    assert host not in config.PRODUCTION_POSTGRES_HOSTS
    assert not config.is_production_host(host)


# --------------------------------------------------------------------------
# Explicit opt-ins keep working (no production behaviour change)
# --------------------------------------------------------------------------


def test_prod_config_defaults_to_internal_host(load_config):
    config = load_config()

    assert config.ProdConfig.resolve_postgres_host() == "10.132.0.2"
    assert _host_of(config.ProdConfig.SQLALCHEMY_DATABASE_URI) == "10.132.0.2"


def test_dev_config_prod_db_remains_the_explicit_production_opt_in(load_config):
    config = load_config()

    host = config.DevConfigProdDB.resolve_postgres_host()
    assert config.is_production_host(host)
    assert _host_of(config.DevConfigProdDB.SQLALCHEMY_DATABASE_URI) == host


@pytest.mark.parametrize(
    "config_name", ["Config", "DevConfig", "DevConfigProdDB", "ProdConfig"]
)
def test_explicit_env_host_wins_for_every_config(load_config, config_name):
    config = load_config(POSTGRES_HOST="10.132.0.2")
    config_cls = getattr(config, config_name)

    assert config_cls.resolve_postgres_host() == "10.132.0.2"
    assert _host_of(config_cls.SQLALCHEMY_DATABASE_URI) == "10.132.0.2"


def test_uri_carries_user_port_and_database(load_config):
    config = load_config(
        POSTGRES_HOST="db.example.test",
        POSTGRES_USER="someone",
        POSTGRES_PW="hunter2",
        POSTGRES_DB="levelup",
        POSTGRES_PORT="6543",
    )

    assert config.DevConfig.SQLALCHEMY_DATABASE_URI == (
        "postgresql://someone:hunter2@db.example.test:6543/levelup"
    )


# --------------------------------------------------------------------------
# Runtime resolution (the URI must not be frozen at import time)
# --------------------------------------------------------------------------


def test_resolve_database_uri_reads_the_environment_at_call_time(load_config):
    config = load_config()
    os.environ["POSTGRES_HOST"] = "late.example.test"

    assert config.DevConfig.resolve_postgres_host() == "late.example.test"
    assert _host_of(config.DevConfig.resolve_database_uri()) == "late.example.test"


def test_get_config_class_selects_by_flask_env(load_config):
    config = load_config()

    assert config.get_config_class("production") is config.ProdConfig
    assert config.get_config_class("development") is config.DevConfig
    assert config.get_config_class(None) is config.DevConfig


# --------------------------------------------------------------------------
# Migration guard
# --------------------------------------------------------------------------


def test_migration_guard_blocks_production_host_outside_production(load_config):
    config = load_config()

    with pytest.raises(RuntimeError) as excinfo:
        config.assert_safe_migration_target("34.77.91.59", env="development")

    assert "34.77.91.59" in str(excinfo.value)


def test_migration_guard_allows_production_host_in_production(load_config):
    config = load_config()

    config.assert_safe_migration_target("10.132.0.2", env="production")


def test_migration_guard_allows_local_hosts(load_config):
    config = load_config()

    config.assert_safe_migration_target("localhost", env="development")
    config.assert_safe_migration_target("127.0.0.1", env="development")


@pytest.mark.parametrize(
    "argv,expected",
    [
        (["flask", "db", "upgrade"], True),
        (["flask", "--app", "app.py", "db", "current"], True),
        (["flask", "run", "--port", "5000"], False),
        (["gunicorn", "app:run_app"], False),
        (["pytest", "padel_app/tests"], False),
    ],
)
def test_is_migration_invocation(load_config, argv, expected):
    config = load_config()

    assert config.is_migration_invocation(argv) is expected


def test_migration_guard_has_an_explicit_escape_hatch(load_config):
    config = load_config()
    os.environ["ALLOW_PRODUCTION_MIGRATIONS"] = "1"
    try:
        config.assert_safe_migration_target("34.77.91.59", env="development")
    finally:
        os.environ.pop("ALLOW_PRODUCTION_MIGRATIONS", None)
