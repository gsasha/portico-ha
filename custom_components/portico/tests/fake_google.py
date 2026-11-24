"""A fake Google for the push sender's tests (sender design §8.1).

One local aiohttp server on 127.0.0.1 stands in for BOTH the OAuth token
endpoint (`/token`) and FCM v1 (`/v1/projects/{p}/messages:send`). Nothing
here, or in any test, ever reaches a real Google host: the production code's
endpoints are swapped for this server's URLs through
`push.const.ENDPOINTS`, the one test-only override point.

The token endpoint VERIFIES the signed assertion with the public half of a
throwaway RSA key generated per test run (RS256, `iss`, `aud`, `scope`,
`exp - iat <= 3600`, the `kid` header), so a wrong JWT fails here exactly as
it would at Google. Responses can be scripted per request.
"""
from __future__ import annotations

import json
from collections import deque
from collections.abc import Callable
from typing import Any

import jwt
from aiohttp import web
from aiohttp.test_utils import TestServer
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
PROJECT_ID = "portico-test"
CLIENT_EMAIL = "fcm-sender@portico-test.iam.example.invalid"
KEY_ID = "test-kid-0001"

_KEY = None


def throwaway_key():
    """One RSA key per test process, generated here, never written anywhere."""
    global _KEY
    if _KEY is None:
        _KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return _KEY


def service_account_info(token_uri: str, *, project_id: str = PROJECT_ID) -> dict[str, Any]:
    pem = throwaway_key().private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    return {
        "type": "service_account",
        "project_id": project_id,
        "private_key_id": KEY_ID,
        "private_key": pem,
        "client_email": CLIENT_EMAIL,
        "client_id": "000000000000000000000",
        "token_uri": token_uri,
    }


def fcm_error(status: int, code: str, *, error_code: str | None = None,
              field: str | None = None, headers: dict | None = None) -> tuple[int, dict, dict]:
    """A Google-shaped error body (google.rpc.Status + FCM details)."""
    details: list[dict] = []
    if error_code:
        details.append({"@type": "type.googleapis.com/google.firebase.fcm.v1.FcmError",
                        "errorCode": error_code})
    if field:
        details.append({"@type": "type.googleapis.com/google.rpc.BadRequest",
                        "fieldViolations": [{"field": field, "description": "bad"}]})
    body = {"error": {"code": status, "message": "Google says no", "status": code, "details": details}}
    return status, body, headers or {}


Response = tuple[int, dict, dict]


class FakeGoogle:
    def __init__(self) -> None:
        self.server: TestServer | None = None
        self.token_requests: list[dict] = []
        self.jwt_errors: list[str] = []
        self.fcm_requests: list[dict] = []
        self.token_script: deque[Response | Callable[[], Response]] = deque()
        self.fcm_script: deque[Response | Callable[[dict], Response]] = deque()
        self.fcm_default: Callable[[dict], Response] | None = None
        self.issued: list[str] = []
        self.revoked: set[str] = set()
        self.expires_in = 3600
        self.token_delay = 0.0
        self.fcm_delay = 0.0

    @property
    def base(self) -> str:
        assert self.server is not None
        return str(self.server.make_url("")).rstrip("/")

    @property
    def token_uri(self) -> str:
        return f"{self.base}/token"

    async def start(self) -> FakeGoogle:
        app = web.Application()
        app.router.add_post("/token", self._token)
        app.router.add_post("/v1/projects/{project}/messages:send", self._send)
        self.server = TestServer(app, host="127.0.0.1")
        await self.server.start_server()
        return self

    async def close(self) -> None:
        if self.server is not None:
            await self.server.close()

    async def _token(self, request: web.Request) -> web.Response:
        import asyncio

        form = await request.post()
        self.token_requests.append(dict(form))
        if self.token_delay:
            await asyncio.sleep(self.token_delay)
        if self.token_script:
            item = self.token_script.popleft()
            status, body, headers = item() if callable(item) else item
            return web.json_response(body, status=status, headers=headers)
        if form.get("grant_type") != "urn:ietf:params:oauth:grant-type:jwt-bearer":
            self.jwt_errors.append("grant_type")
            return web.json_response({"error": "unsupported_grant_type"}, status=400)
        assertion = str(form.get("assertion", ""))
        try:
            header = jwt.get_unverified_header(assertion)
            claims = jwt.decode(
                assertion,
                throwaway_key().public_key(),
                algorithms=["RS256"],
                audience=self.token_uri,
                options={"require": ["iss", "aud", "iat", "exp", "scope"]},
            )
        except jwt.PyJWTError as err:
            self.jwt_errors.append(type(err).__name__)
            return web.json_response({"error": "invalid_grant"}, status=400)
        problems = []
        if header.get("alg") != "RS256":
            problems.append("alg")
        if header.get("kid") != KEY_ID:
            problems.append("kid")
        if claims["iss"] != CLIENT_EMAIL:
            problems.append("iss")
        if claims["scope"] != SCOPE:
            problems.append("scope")
        if not 0 < claims["exp"] - claims["iat"] <= 3600:
            problems.append("exp")
        if problems:
            self.jwt_errors.extend(problems)
            return web.json_response({"error": "invalid_grant"}, status=400)
        token = f"ya29.fake-access-token-{len(self.issued):04d}-SECRETVALUE"
        self.issued.append(token)
        return web.json_response(
            {"access_token": token, "expires_in": self.expires_in, "token_type": "Bearer"}
        )

    async def _send(self, request: web.Request) -> web.Response:
        import asyncio

        body = await request.json()
        if self.fcm_delay:
            await asyncio.sleep(self.fcm_delay)
        auth = request.headers.get("Authorization", "")
        record = {"project": request.match_info["project"], "auth": auth, "json": body}
        self.fcm_requests.append(record)
        token = auth.removeprefix("Bearer ")
        if token not in self.issued or token in self.revoked:
            _, err_body, _ = fcm_error(401, "UNAUTHENTICATED")
            return web.json_response(err_body, status=401)
        if self.fcm_script:
            item = self.fcm_script.popleft()
            status, resp, headers = item(record) if callable(item) else item
        elif self.fcm_default is not None:
            status, resp, headers = self.fcm_default(record)
        else:
            status, resp, headers = 200, {"name": f"projects/{record['project']}/messages/{len(self.fcm_requests)}"}, {}
        return web.Response(status=status, text=json.dumps(resp), headers=headers,
                            content_type="application/json")

    def sent_messages(self) -> list[dict]:
        return [r["json"]["message"] for r in self.fcm_requests]
