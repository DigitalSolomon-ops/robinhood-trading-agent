"""Cloud Run web entry for the 007 dashboard.

Serves dashboard_app on 0.0.0.0:$PORT (Cloud Run sets $PORT). The arm toggle
shares state via Firestore when TRADER_ARM_FIRESTORE_PROJECT is set; data-heavy
views degrade until a trader runtime is co-located on the same service.
"""
from __future__ import annotations

import os

import uvicorn

from .dashboard import dashboard_app
from .main import ROOT


def main() -> None:
    port = int(os.getenv("PORT", "8080"))
    uvicorn.run(dashboard_app(ROOT), host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
