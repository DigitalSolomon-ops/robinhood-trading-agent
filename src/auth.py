from __future__ import annotations

import base64
import time
from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True)
class RobinhoodAuth:
    api_key: str
    private_key_base64: str

    def has_credentials(self) -> bool:
        return bool(self.api_key and self.private_key_base64)

    def headers(self, method: str, path: str, body: str = "", timestamp: int | None = None) -> Mapping[str, str]:
        if not self.has_credentials():
            raise ValueError("Robinhood API credentials are missing")

        try:
            from nacl.signing import SigningKey
        except ImportError as exc:
            raise RuntimeError("pynacl is required for Robinhood request signing") from exc

        ts = int(timestamp if timestamp is not None else time.time())
        private_key_seed = base64.b64decode(self.private_key_base64)
        signing_key = SigningKey(private_key_seed)
        message = f"{self.api_key}{ts}{path}{method.upper()}{body}"
        signed = signing_key.sign(message.encode("utf-8"))
        return {
            "x-api-key": self.api_key,
            "x-signature": base64.b64encode(signed.signature).decode("utf-8"),
            "x-timestamp": str(ts),
        }
