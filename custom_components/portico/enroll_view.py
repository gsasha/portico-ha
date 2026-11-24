"""UI split P2: `GET /api/portico/enroll/qr/<token>`, the enrolment QR as a PNG for the Phones tab.

The QR decodes to the one-time code and, in `auto` mode, a pre-authorised Tailscale key, so it is never a websocket
frame (HA logs every frame at DEBUG, F15). The panel fetches this URL ONCE as a blob with the user's own bearer
header and draws it from an object URL: no signed path, no secret in a URL.

  * Authenticated: HA's auth middleware answers a request with no valid login (that 401 is HA's, before this code).
  * ADMINISTRATORS ONLY, answered `403`. Not `components.http.require_admin`: that decorator raises `Unauthorized`,
    which HA's view wrapper turns into a `401`, and a 401 from a view feeds HA's IP-ban counter (`ban.py`), so a
    household member poking at the URL could ban their own address. The check is made here instead.
  * An unknown, expired or cancelled token is `404`. Never 401.
  * `Cache-Control: no-store`. The token is not consumed by a read.
"""
from __future__ import annotations

from http import HTTPStatus

from aiohttp import web
from homeassistant.components import http
from homeassistant.core import HomeAssistant, callback

from .enroll_qr import get_qr_store

QR_PATH = "/api/portico/enroll/qr"
TEXT_PATH = "/api/portico/enroll/text"
_DATA_VIEW = "portico_enroll_qr_view"
_NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache", "X-Content-Type-Options": "nosniff"}


class EnrollQrView(http.HomeAssistantView):
    requires_auth = True
    url = QR_PATH + "/{token}"
    name = "api:portico:enroll:qr"

    async def get(self, request: web.Request, token: str) -> web.Response:
        hass: HomeAssistant = request.app[http.KEY_HASS]
        user = request.get("hass_user")
        if user is None or not user.is_admin:
            return web.Response(status=HTTPStatus.FORBIDDEN, headers=_NO_STORE)
        png = get_qr_store(hass).png_for(hass, token)
        if png is None:
            return web.Response(status=HTTPStatus.NOT_FOUND, headers=_NO_STORE)
        return web.Response(body=png, content_type="image/png", headers=_NO_STORE)


class EnrollTextView(http.HomeAssistantView):
    """`GET /api/portico/enroll/text/<token>`: the text the QR encodes, for the panel's "Copy enrolment text" button
    (a wall tablet cannot always scan; its app's Settings field takes this text). The same rules as the PNG: administrators
    only (403), unknown/expired/cancelled token 404, `no-store`, never a websocket frame, never logged. `text/plain`."""

    requires_auth = True
    url = TEXT_PATH + "/{token}"
    name = "api:portico:enroll:text"

    async def get(self, request: web.Request, token: str) -> web.Response:
        hass: HomeAssistant = request.app[http.KEY_HASS]
        user = request.get("hass_user")
        if user is None or not user.is_admin:
            return web.Response(status=HTTPStatus.FORBIDDEN, headers=_NO_STORE)
        text = get_qr_store(hass).text_for(hass, token)
        if text is None:
            return web.Response(status=HTTPStatus.NOT_FOUND, headers=_NO_STORE)
        return web.Response(text=text, content_type="text/plain", charset="utf-8", headers=_NO_STORE)


@callback
def async_register_view(hass: HomeAssistant) -> None:
    """Register the view once (HA's router accepts late routes)."""
    if hass.data.get(_DATA_VIEW):
        return
    hass.http.register_view(EnrollQrView())
    hass.http.register_view(EnrollTextView())
    hass.data[_DATA_VIEW] = True
