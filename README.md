## Repository Layout

```
.
├── app.py
├── Dockerfile
├── instance/
├── migrations/            # Alembic migrations
├── padel_app/
│   ├── __init__.py
│   ├── auth.py, cli.py, config.py, context.py, mail.py, model.py, sql_db.py
│   ├── models/            # SQLAlchemy models (users, players, coaches, lessons, etc.)
│   ├── modules/           # Flask blueprints (api.py, auth.py, editor.py, main.py, startup.py)
│   ├── static/            # JS, SCSS/CSS, images, svg
│   ├── templates/         # Jinja templates (auth, editor, users, macros)
│   ├── tests/             # pytest suite
│   └── tools/             # helpers (auth_tools, email_tools, image_tools, input_tools)
├── poetry.lock
├── pyproject.toml
├── scripts/               # backup.sh, startup.sh
└── terraform/             # Terraform IaC (main.tf, variables.tf, outputs.tf, *.tfvars)
```


## API Routes

| Method | Path | Source |
|---|---|---|

| POST | `/create/<model>` | `api.py` |

| POST | `/edit/<model>/<id>` | `api.py` |

| GET | `/delete/<model>/<id>` | `api.py` |

| GET | `/query/<model>` | `api.py` |

| GET | `/remove_relationship` | `api.py` |

| GET | `/modal_create_page/<model>` | `api.py` |

| GET | `/download_csv/<model>` | `api.py` |

| POST | `/download_csv/<model>` | `api.py` |

| GET | `/upload_csv_to_db/<model>` | `api.py` |

| POST | `/upload_csv_to_db/<model>` | `api.py` |

| GET | `/` | `auth.py` |

| GET | `/register` | `auth.py` |

| GET | `/login` | `auth.py` |

| GET | `/forgot_password` | `auth.py` |

| GET | `/verify_generated_code/<user_id>` | `auth.py` |

| GET | `/generate_new_code/<user_id>` | `auth.py` |

| GET | `/logout` | `auth.py` |

| GET | `/` | `editor.py` |

| GET | `/display/<model>` | `editor.py` |

| GET | `/display/<model>/<id>` | `editor.py` |

| GET | `/create/<model>` | `editor.py` |

| GET | `/` | `main.py` |


## Development
- Use a `.env` file for local config.
- Consider adding `Flask-Migrate`/Alembic for DB migrations.

## API Examples

### Create User
Example request:

```bash
curl -X POST http://127.0.0.1:5000/api/create/user \
  -H "Content-Type: application/json" \
  -d '{
    "values": {
      "user_image_id": null,
      "name": "Alice Smith",
      "username": "asmith",
      "email": "alice@example.com",
      "password": "pbkdf2:sha256:600000$demo$1234567890abcdef",
      "is_admin": true,
      "generated_code": 12939
    }
  }'
```

Example response:

```json
{"id": 8, "success": true}
```

> Note: `password` expects a pre-hashed string (e.g., Werkzeug `generate_password_hash`). Do **not** send plaintext passwords.


## Database & Migrations

- ORM: SQLAlchemy (models under `padel_app/models/`)
- Migrations: Alembic (in `migrations/`)

Initialize & upgrade:

```bash
flask db upgrade
```

Environment variables typically required:

- `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PW`


## Running Locally

### With Poetry
```bash
poetry install
poetry run python app.py
```


### With Docker
```bash
docker build -t levelup_backend .
docker run --rm -p 80:80 --env-file .env levelup_backend
```


## Terraform (optional)

The `terraform/` folder contains IaC for provisioning infra.

```bash
cd terraform
terraform init
terraform plan
terraform apply
```


## Environment Variables

| Key | Required | Example |
|---|---|---|
| `FLASK_ENV` | No |  |
| `SECRET_KEY` | Yes |  |
| `POSTGRES_HOST` | Yes |  |
| `POSTGRES_PORT` | No |  |
| `POSTGRES_DB` | Yes |  |
| `POSTGRES_USER` | Yes |  |
| `POSTGRES_PW` | Yes |  |
| `MAIL_USERNAME` | No |  |
| `MAIL_PASSWORD` | No |  |
| `GCS_UPLOADS_BUCKET` | No |  |

