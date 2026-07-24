from __future__ import annotations

import base64

from nacl.signing import SigningKey


def main() -> None:
    signing_key = SigningKey.generate()
    verify_key = signing_key.verify_key

    private_key = base64.b64encode(signing_key.encode()).decode("utf-8")
    public_key = base64.b64encode(verify_key.encode()).decode("utf-8")

    print("PUBLIC KEY - paste this into Robinhood:")
    print(public_key)
    print()
    print("PRIVATE KEY - paste this into local .env only:")
    print(private_key)
    print()
    print("WARNING: Never share the private key. Store it only in your local .env or an encrypted password manager.")


if __name__ == "__main__":
    main()
