"""Placeholder usernames for accounts whose owner has not chosen one yet.

`users.username` is NOT NULL and UNIQUE, but a username is a login credential —
so only the person who will log in gets to pick it. Coaches create player
accounts (PAD-105) and coaches invite players (PAD-32) without ever supplying a
username; those rows carry a generated placeholder until the player activates
their own account and replaces it (see `players.invite-completion` and
`auth.activate` in the spec tree).

The placeholder is deliberately unusable as a login: nobody can guess 16 random
hex characters, and these accounts have no password hash until activation.
It is an internal detail and is never rendered in the coach-facing UI.
"""

import secrets

PLACEHOLDER_USERNAME_PREFIX = "pending-"


def is_placeholder_username(username):
    """True if `username` was generated rather than chosen by its owner."""
    return bool(username) and username.startswith(PLACEHOLDER_USERNAME_PREFIX)


def unique_placeholder_username():
    """Returns a fresh placeholder username that no user currently holds."""
    # Imported lazily: this module is pulled in by services that are themselves
    # imported while the model layer is still being assembled.
    from padel_app.models import User

    while True:
        candidate = f"{PLACEHOLDER_USERNAME_PREFIX}{secrets.token_hex(8)}"
        if User.query.filter_by(username=candidate).first() is None:
            return candidate
