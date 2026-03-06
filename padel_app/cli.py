import click
from werkzeug.security import generate_password_hash
from padel_app.sql_db import db


def register_cli(app):
    @app.cli.command("seed")
    @click.option(
        "--table",
        "tables",
        multiple=True,
        help="Seed one table (or repeat --table). Example: --table users --table coaches",
    )
    @click.option(
        "--all-mock-data",
        "--all_mock_data",
        "all_mock_data",
        is_flag=True,
        help="Seed all mock data tables.",
    )
    @click.option("--admin-user", default="admin")
    @click.option("--admin-email", default="admin@example.com")
    @click.option(
        "--admin-password",
        envvar="ADMIN_PASSWORD",
        hide_input=True,
        default=None,
        help="Admin password (used only when seeding admin user).",
    )
    def seed(tables, all_mock_data, admin_user, admin_email, admin_password):
        from padel_app.models import Backend_App, User
        from padel_app.seed import (
            ALL_SEED_TABLES,
            available_table_names,
            normalize_table_name,
            seed_mock_tables,
        )

        with app.app_context():
            if tables and all_mock_data:
                click.echo("❌ Use either --table or --all_mock_data, not both.")
                return

            if tables or all_mock_data:
                requested_tables = []
                if all_mock_data:
                    requested_tables = list(ALL_SEED_TABLES)
                else:
                    invalid_tables = []
                    for table in tables:
                        normalized = normalize_table_name(table)
                        if normalized is None:
                            invalid_tables.append(table)
                        else:
                            requested_tables.append(normalized)

                    if invalid_tables:
                        click.echo(
                            "❌ Unknown table(s): "
                            + ", ".join(sorted(set(invalid_tables)))
                        )
                        click.echo(
                            "Available tables: "
                            + ", ".join(available_table_names())
                        )
                        return

                    requested_tables = list(dict.fromkeys(requested_tables))

                try:
                    results = seed_mock_tables(requested_tables)
                    click.echo("✅ Mock data seeded:")
                    for table_name, result in results.items():
                        click.echo(
                            f" - {table_name}: inserted={result.inserted}, updated={result.updated}"
                        )
                except Exception as exc:
                    db.session.rollback()
                    click.echo(f"❌ Mock seed failed: {exc}")
                    raise
                return

            if not admin_password:
                admin_password = click.prompt(
                    "Admin password",
                    hide_input=True,
                    confirmation_prompt=True,
                )

            admin = User.query.filter_by(username=admin_user).first()
            if not admin:
                admin = User(
                    name=admin_user,
                    username=admin_user,
                    email=admin_email,
                    password=generate_password_hash(admin_password),
                    is_admin=True,
                )
                admin.create()

            apps_app = Backend_App.query.filter_by(name="Aplicações").first()
            if not apps_app:
                apps_app = Backend_App(name="Aplicações", app_model_name="Backend_App")
                apps_app.create()

            click.echo("Seeding done.")

    @app.cli.command("db-reset")
    @click.option(
        "--yes",
        is_flag=True,
        help="Confirm database reset (required).",
    )
    @click.option(
        "--table",
        "tables",
        multiple=True,
        help="Table name(s) to truncate. If omitted, all tables are truncated.",
    )
    def db_reset(yes, tables):
        """Truncate tables and reset identities (DEV ONLY)."""

        if not yes:
            click.echo("❌ Aborted. Use --yes to confirm.")
            return

        click.echo("⚠️  Resetting database…")

        all_tables = set(db.metadata.tables.keys())

        if tables:
            requested_tables = set(tables)
            invalid_tables = requested_tables - all_tables

            if invalid_tables:
                click.echo(
                    f"❌ Unknown table(s): {', '.join(invalid_tables)}"
                )
                return

            table_names = sorted(requested_tables)
        else:
            table_names = sorted(all_tables)

        if not table_names:
            click.echo("ℹ️  No tables found.")
            return

        sql = (
            "TRUNCATE TABLE "
            + ", ".join(table_names)
            + " RESTART IDENTITY CASCADE"
        )

        db.session.execute(sql)
        db.session.commit()

        click.echo(f"✅ Reset {len(table_names)} table(s): {', '.join(table_names)}")
