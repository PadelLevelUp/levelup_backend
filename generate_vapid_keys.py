#!/usr/bin/env python3
# Audit findings (Phase 1):
# - The Flask app reads environment values from local dotenv files in app.py during non-production runs.
# - Push delivery should be used in parallel with existing SSE, mainly for closed-app messaging.
# - JWT-protected API routes already exist and will expose the VAPID public key to authenticated clients.
from py_vapid import Vapid01


def main():
    vapid = Vapid01()
    vapid.generate_keys()

    private_key = vapid.private_pem().decode("utf-8")
    public_key = vapid.public_key.decode("utf-8") if isinstance(vapid.public_key, bytes) else vapid.public_key
    private_key_one_line = private_key.replace("\n", "\\n")

    print(f"VAPID_PUBLIC_KEY={public_key}")
    print(f'VAPID_PRIVATE_KEY="{private_key_one_line}"')
    print("VAPID_CLAIMS_EMAIL=mailto:your-email@example.com")


if __name__ == "__main__":
    main()
