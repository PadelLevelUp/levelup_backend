"""Shared admin authentication for the dev-only seed scripts (PAD-92).

PAD-88 put the generic model-CRUD blueprint (`/api/create|edit|delete/<model>`)
behind an admin guard, and PAD-92 removed the unauthenticated `/api/app/*`
entity-creation shortcuts these scripts used to call. Both scripts now have to
present an admin JWT, exactly like the Jinja editor's own JS does.

Credentials come from the environment so nothing is hard-coded:

    export SEED_ADMIN_USERNAME=your_admin
    export SEED_ADMIN_PASSWORD=...
    python docs/tests/seed_frontend_demo.py

These scripts are dev tooling — they are not exercised by pytest or CI.
"""
import os
import sys

import requests

HOST = os.environ.get("SEED_API_HOST", "http://127.0.0.1:5000")
API = f"{HOST}/api"


def admin_session() -> requests.Session:
    """Log in as an admin and return a Session with the bearer token attached."""
    username = os.environ.get("SEED_ADMIN_USERNAME")
    password = os.environ.get("SEED_ADMIN_PASSWORD")

    if not username or not password:
        sys.exit(
            "SEED_ADMIN_USERNAME and SEED_ADMIN_PASSWORD must be set — the seed "
            "scripts authenticate as an administrator (PAD-88/PAD-92). Export "
            "them for a local admin account before running this script."
        )

    res = requests.post(
        f"{API}/auth/login", json={"username": username, "password": password}
    )
    if res.status_code != 200:
        sys.exit(f"Admin login failed ({res.status_code}): {res.text}")

    token = res.json().get("accessToken")
    if not token:
        sys.exit("Admin login returned no accessToken")

    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {token}"})
    return session


def create(session: requests.Session, model: str, values: dict):
    """Create ``model`` through the admin CRUD API.

    Posted as form data on purpose: that makes the endpoint run the model's
    `Form.set_values()`, which is the same code path the deleted `/api/app/*`
    create-services used — notably it hashes `password` (the JSON `values`
    branch does not) and resolves multi-selects from repeated keys.
    """
    form = {}
    for key, value in values.items():
        if value is None:
            continue
        if isinstance(value, (list, tuple)):
            form[key] = [str(v) for v in value]
        elif isinstance(value, bool):
            form[key] = "true" if value else ""
        else:
            form[key] = str(value)

    res = session.post(f"{API}/create/{model.lower()}", data=form)
    if res.status_code not in (200, 201):
        raise ValueError(f"create {model} failed: {res.status_code} {res.text}")
    body = res.json()
    if not body.get("success", True):
        raise ValueError(f"create {model} failed: {body}")
    return body


def delete(session: requests.Session, model: str, obj_id):
    res = session.post(f"{API}/delete/{model.lower()}/{obj_id}")
    if res.status_code not in (200, 201):
        raise ValueError(f"delete {model} {obj_id} failed: {res.status_code} {res.text}")
    return res
