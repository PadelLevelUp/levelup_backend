#!/usr/bin/env python3
# Audit findings (Phase 1):
# - The Flask app reads environment values from local dotenv files in app.py during non-production runs.
# - Push delivery should be used in parallel with existing SSE, mainly for closed-app messaging.
# - JWT-protected API routes already exist and will expose the VAPID public key to authenticated clients.
import base64

from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from py_vapid import Vapid01


def main():
    vapid = Vapid01()
    vapid.generate_keys()

    public_key_bytes = vapid.public_key.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    public_key = base64.urlsafe_b64encode(public_key_bytes).rstrip(b"=").decode("utf-8")

    raw_private = vapid._private_key.private_numbers().private_value.to_bytes(32, "big")
    private_key = base64.urlsafe_b64encode(raw_private).rstrip(b"=").decode("utf-8")

    print(f"VAPID_PUBLIC_KEY={public_key}")
    print(f"VAPID_PRIVATE_KEY={private_key}")
    print("VAPID_CLAIMS_EMAIL=mailto:your-email@example.com")


if __name__ == "__main__":
    main()
