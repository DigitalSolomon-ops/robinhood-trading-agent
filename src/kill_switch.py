from __future__ import annotations

import os
from pathlib import Path


class KillSwitch:
    def __init__(self, stop_file: str = "STOP_TRADING", env_var: str = "TRADING_ENABLED") -> None:
        self.stop_file = Path(stop_file)
        self.env_var = env_var

    def create_stop_file(self) -> Path:
        self.stop_file.write_text("Trading stopped by operator.\n", encoding="utf-8")
        return self.stop_file

    def stop_file_exists(self) -> bool:
        return self.stop_file.exists()

    def trading_env_enabled(self) -> bool:
        return os.getenv(self.env_var, "false").strip().lower() == "true"

    def halt_reasons(self) -> list[str]:
        reasons: list[str] = []
        if self.stop_file_exists():
            reasons.append(f"{self.stop_file} exists")
        if not self.trading_env_enabled():
            reasons.append(f"{self.env_var}=false")
        return reasons

    def assert_open(self) -> None:
        reasons = self.halt_reasons()
        if reasons:
            raise RuntimeError("; ".join(reasons))
