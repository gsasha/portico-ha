"""DEV-544 test helpers: redeem the one-time code a drawn QR carries, the way a phone would, without the HTTP layer
(`test_enroll_redeem.py` drives the real view). An enrollment no longer leaves a token behind, so a test that needs
the phone's token redeems first."""
from __future__ import annotations

from custom_components.portico import enroll_redeem

INSTALLATION = "inst-0f1e2d3c4b5a"
NONCE = "nonce-0123456789abcdef"


async def redeem(hass, payload: dict, *, installation_id: str = INSTALLATION, nonce: str = NONCE,
                 code: str | None = None, enroll_id: str | None = None) -> dict:
    """Redeem a QR payload's code in process. Returns the response body; AssertionError when it was refused."""
    outcome = await enroll_redeem.get_registry(hass).async_redeem(
        hass, enroll_id=enroll_id or payload["enroll_id"], code=code or payload["code"],
        installation_id=installation_id, nonce=nonce)
    assert outcome.ok, outcome.kind
    return outcome.payload


async def redeemed_token(hass, user_id: str, payload: dict):
    """Redeem `payload` and return (the user's refresh token, an access token for it)."""
    body = await redeem(hass, payload)
    user = await hass.auth.async_get_user(user_id)
    (token,) = [t for t in user.refresh_tokens.values() if t.token == body["refresh_token"]]
    return token, body["access_token"]
