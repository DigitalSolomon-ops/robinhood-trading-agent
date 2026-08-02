from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.dashboard import dashboard_app
from src.web_security import (
    IAP_AUDIENCE_ENV,
    IAP_HEADER,
    IAPVerificationError,
    PUBLIC_ORIGIN_ENV,
    install_security_middleware,
)

ORIGIN = "https://crypto.digitalsolomon.com"


def _app(verifier=None) -> FastAPI:
    app = FastAPI()
    if verifier is None:
        install_security_middleware(app)
    else:
        install_security_middleware(app, verifier=verifier)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/settings")
    def settings() -> dict[str, str]:
        return {"page": "settings"}

    @app.post("/live-control")
    def live_control() -> dict[str, str]:
        return {"action": "accepted"}

    return app


def _accepting_verifier(token: str, audience: str) -> dict[str, str]:
    if token != "good-token":
        raise IAPVerificationError("bad token")
    return {"email": "marcus.barber@digitalsolomon.com"}


# --- IAP guard -------------------------------------------------------------


def test_guards_are_inert_when_unconfigured(monkeypatch) -> None:
    monkeypatch.delenv(IAP_AUDIENCE_ENV, raising=False)
    monkeypatch.delenv(PUBLIC_ORIGIN_ENV, raising=False)
    client = TestClient(_app(_accepting_verifier))
    assert client.get("/settings").status_code == 200
    assert client.post("/live-control").status_code == 200


def test_missing_iap_header_is_rejected(monkeypatch) -> None:
    monkeypatch.setenv(IAP_AUDIENCE_ENV, "/projects/1/global/backendServices/2")
    monkeypatch.delenv(PUBLIC_ORIGIN_ENV, raising=False)
    client = TestClient(_app(_accepting_verifier))
    assert client.get("/settings").status_code == 401


def test_invalid_iap_assertion_is_rejected(monkeypatch) -> None:
    monkeypatch.setenv(IAP_AUDIENCE_ENV, "/projects/1/global/backendServices/2")
    monkeypatch.delenv(PUBLIC_ORIGIN_ENV, raising=False)
    client = TestClient(_app(_accepting_verifier))
    response = client.get("/settings", headers={IAP_HEADER: "forged"})
    assert response.status_code == 401


def test_valid_iap_assertion_passes(monkeypatch) -> None:
    monkeypatch.setenv(IAP_AUDIENCE_ENV, "/projects/1/global/backendServices/2")
    monkeypatch.delenv(PUBLIC_ORIGIN_ENV, raising=False)
    client = TestClient(_app(_accepting_verifier))
    response = client.get("/settings", headers={IAP_HEADER: "good-token"})
    assert response.status_code == 200


@pytest.mark.parametrize("path", ["/healthz"])
def test_healthz_bypasses_the_iap_guard(monkeypatch, path: str) -> None:
    monkeypatch.setenv(IAP_AUDIENCE_ENV, "/projects/1/global/backendServices/2")
    monkeypatch.setenv(PUBLIC_ORIGIN_ENV, ORIGIN)
    client = TestClient(_app(_accepting_verifier))
    response = client.get(path)
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_order_placing_route_is_closed_without_an_assertion(monkeypatch) -> None:
    """The health-check ranges and the default VPC reach :8000 directly.

    POST /live-control can place a real order, so an unauthenticated request
    from inside those ranges must not reach the handler.
    """
    monkeypatch.setenv(IAP_AUDIENCE_ENV, "/projects/1/global/backendServices/2")
    monkeypatch.delenv(PUBLIC_ORIGIN_ENV, raising=False)
    client = TestClient(_app(_accepting_verifier))
    assert client.post("/live-control").status_code == 401


# --- CSRF guard ------------------------------------------------------------


def test_cross_site_post_is_rejected(monkeypatch) -> None:
    monkeypatch.delenv(IAP_AUDIENCE_ENV, raising=False)
    monkeypatch.setenv(PUBLIC_ORIGIN_ENV, ORIGIN)
    client = TestClient(_app(_accepting_verifier))
    response = client.post("/live-control", headers={"origin": "https://evil.example"})
    assert response.status_code == 403


def test_post_without_origin_or_referer_is_rejected(monkeypatch) -> None:
    monkeypatch.delenv(IAP_AUDIENCE_ENV, raising=False)
    monkeypatch.setenv(PUBLIC_ORIGIN_ENV, ORIGIN)
    client = TestClient(_app(_accepting_verifier))
    assert client.post("/live-control").status_code == 403


def test_same_origin_post_is_accepted(monkeypatch) -> None:
    monkeypatch.delenv(IAP_AUDIENCE_ENV, raising=False)
    monkeypatch.setenv(PUBLIC_ORIGIN_ENV, ORIGIN)
    client = TestClient(_app(_accepting_verifier))
    response = client.post("/live-control", headers={"origin": ORIGIN})
    assert response.status_code == 200


def test_referer_fallback_is_accepted(monkeypatch) -> None:
    monkeypatch.delenv(IAP_AUDIENCE_ENV, raising=False)
    monkeypatch.setenv(PUBLIC_ORIGIN_ENV, ORIGIN)
    client = TestClient(_app(_accepting_verifier))
    response = client.post(
        "/live-control", headers={"referer": f"{ORIGIN}/live-control"}
    )
    assert response.status_code == 200


def test_safe_methods_skip_the_origin_check(monkeypatch) -> None:
    monkeypatch.delenv(IAP_AUDIENCE_ENV, raising=False)
    monkeypatch.setenv(PUBLIC_ORIGIN_ENV, ORIGIN)
    client = TestClient(_app(_accepting_verifier))
    assert client.get("/settings").status_code == 200


# --- wiring into the real dashboard ---------------------------------------


def test_dashboard_exposes_healthz(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv(IAP_AUDIENCE_ENV, raising=False)
    monkeypatch.delenv(PUBLIC_ORIGIN_ENV, raising=False)
    client = TestClient(dashboard_app(tmp_path))
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_dashboard_healthz_touches_no_state(tmp_path: Path, monkeypatch) -> None:
    """A health check must not read the database or call the broker.

    tmp_path has no config, no .env and no databases; the home page cannot be
    rendered from it, but /healthz must still answer.
    """
    monkeypatch.delenv(IAP_AUDIENCE_ENV, raising=False)
    monkeypatch.delenv(PUBLIC_ORIGIN_ENV, raising=False)
    client = TestClient(dashboard_app(tmp_path))
    assert client.get("/healthz").json() == {"status": "ok"}
    assert not list(tmp_path.iterdir())


def test_dashboard_mutating_route_requires_assertion(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv(IAP_AUDIENCE_ENV, "/projects/1/global/backendServices/2")
    monkeypatch.delenv(PUBLIC_ORIGIN_ENV, raising=False)
    client = TestClient(dashboard_app(tmp_path))
    assert client.post("/settings", data={}).status_code == 401
    assert client.post("/kill/clear", data={}).status_code == 401
    assert client.post("/live-control", data={}).status_code == 401
