from __future__ import annotations

import os
from typing import Any

import httpx


FRED_SERIES = ["FEDFUNDS", "CPIAUCSL", "UNRATE", "DGS10", "DGS2", "T10Y2Y", "DCOILWTICO"]


class FredMacroClient:
    def __init__(self, api_key: str | None = None, base_url: str = "https://api.stlouisfed.org/fred") -> None:
        self.api_key = api_key if api_key is not None else os.getenv("FRED_API_KEY", "")
        self.base_url = base_url.rstrip("/")

    @property
    def status(self) -> str:
        return "enabled" if self.api_key else "disabled_missing_api_key"

    def fetch(self, series_ids: list[str] | None = None) -> dict[str, Any]:
        if not self.api_key:
            return {"provider": "fred", "status": self.status, "observations": [], "submitted": False}
        observations: list[dict[str, Any]] = []
        with httpx.Client(timeout=20) as client:
            for series_id in series_ids or FRED_SERIES:
                response = client.get(
                    f"{self.base_url}/series/observations",
                    params={
                        "series_id": series_id,
                        "api_key": self.api_key,
                        "file_type": "json",
                        "sort_order": "desc",
                        "limit": "2",
                    },
                )
                response.raise_for_status()
                rows = response.json().get("observations", [])
                values = [self._float_or_none(row.get("value")) for row in rows if isinstance(row, dict)]
                latest = values[0] if values else None
                previous = values[1] if len(values) > 1 else None
                direction = "flat"
                if latest is not None and previous is not None:
                    direction = "up" if latest > previous else "down" if latest < previous else "flat"
                observations.append(
                    {
                        "provider": "fred",
                        "series_id": series_id,
                        "value": latest,
                        "previous_value": previous,
                        "direction": direction,
                        "details": {"rows_seen": len(rows)},
                    }
                )
        return {"provider": "fred", "status": "ok", "observations": observations, "submitted": False}

    @staticmethod
    def _float_or_none(value: Any) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
