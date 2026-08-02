"""Edge security for the dashboard when it runs behind Cloud IAP.

Two independent guards, each switched on by the presence of an environment
variable so that local development and the existing test suite are unaffected:

``IAP_AUDIENCE``
    When set, every request except :data:`EXEMPT_PATHS` must carry a valid
    ``X-Goog-IAP-JWT-Assertion`` header signed by Google for that audience.
    This matters because the load balancer's health-check ranges reach the VM
    on :8000 directly, and so does every other VM on the default VPC. IAP at
    the front door does not cover those paths; this does.

``PUBLIC_ORIGIN``
    When set, state-changing requests must carry a matching ``Origin`` (or,
    failing that, a ``Referer`` under the same origin). An IAP session cookie
    rides along on cross-site form posts, so JWT verification alone leaves the
    order-placing routes open to CSRF.

Both are enabled on the VM and neither is enabled locally.
"""

from __future__ import annotations

import os
from typing import Any, Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

IAP_AUDIENCE_ENV = "IAP_AUDIENCE"
PUBLIC_ORIGIN_ENV = "PUBLIC_ORIGIN"
IAP_HEADER = "x-goog-iap-jwt-assertion"
IAP_CERTS_URL = "https://www.gstatic.com/iap/verify/public_key"

#: Paths that bypass both guards. The load balancer health check is
#: unauthenticated by construction, so it must stay cheap and public.
EXEMPT_PATHS = frozenset({"/healthz"})

#: Methods treated as state-changing for the CSRF check.
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


class IAPVerificationError(Exception):
    """Raised when an IAP assertion is missing, malformed, or not trusted."""


def iap_audience() -> str:
    return os.getenv(IAP_AUDIENCE_ENV, "").strip()


def public_origin() -> str:
    return os.getenv(PUBLIC_ORIGIN_ENV, "").strip().rstrip("/")


def verify_iap_jwt(token: str, audience: str) -> dict[str, Any]:
    """Verify a Cloud IAP assertion and return its claims.

    Signature verification is delegated to ``google-auth``. The import is
    deferred so that the module remains importable (and the guard remains
    testable) on a machine that has not installed it.
    """
    if not token:
        raise IAPVerificationError("missing IAP assertion header")

    try:
        from google.auth.transport import requests as google_requests
        from google.oauth2 import id_token
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise IAPVerificationError(
            "google-auth is required to verify IAP assertions"
        ) from exc

    try:
        claims = id_token.verify_token(
            token,
            google_requests.Request(),
            audience=audience,
            certs_url=IAP_CERTS_URL,
        )
    except Exception as exc:  # google-auth raises a variety of ValueErrors
        raise IAPVerificationError(f"IAP assertion rejected: {exc}") from exc

    if not claims.get("email"):
        raise IAPVerificationError("IAP assertion carries no email claim")
    return claims


def _origin_allowed(request: Request, expected: str) -> bool:
    origin = request.headers.get("origin")
    if origin:
        return origin.rstrip("/") == expected
    referer = request.headers.get("referer")
    if referer:
        return referer.startswith(expected + "/") or referer.rstrip("/") == expected
    # A browser sends Origin on every cross-site form post. Neither header
    # present means this is not a browser form submission we should trust.
    return False


def install_security_middleware(
    app: FastAPI, verifier: Callable[[str, str], dict[str, Any]] = verify_iap_jwt
) -> None:
    """Attach the IAP and CSRF guards to ``app``.

    ``verifier`` is injectable so tests can exercise the middleware without
    minting real Google-signed assertions.
    """

    @app.middleware("http")
    async def _guard(request: Request, call_next):  # type: ignore[no-untyped-def]
        if request.url.path in EXEMPT_PATHS:
            return await call_next(request)

        audience = iap_audience()
        if audience:
            token = request.headers.get(IAP_HEADER, "")
            try:
                claims = verifier(token, audience)
            except IAPVerificationError as exc:
                return JSONResponse({"detail": str(exc)}, status_code=401)
            request.state.iap_email = claims.get("email")

        expected_origin = public_origin()
        if expected_origin and request.method in UNSAFE_METHODS:
            if not _origin_allowed(request, expected_origin):
                return JSONResponse(
                    {"detail": "origin check failed"}, status_code=403
                )

        return await call_next(request)
