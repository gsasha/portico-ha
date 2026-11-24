"""Live video of a camera tile (live.py): `portico/content/live | live_ice | live_close`.

A FAKE camera component and a FAKE WebRTC provider stand in for Home Assistant's own camera support (go2rtc): the real
`camera` component runs, the real `Camera.async_handle_async_webrtc_offer` hands the offer to the provider, so the
capability test (`camera_capabilities`), the helper that finds the entity and the provider calls are the ones Portico makes on
a real install. No stream, no network.

What is pinned: the grant is `content/snapshot`'s (own endpoint's cameras only, no entity id, typed refusals, an admin must
name an endpoint); offer -> result, answer and candidates as events; client candidates reach the provider; a close, an
unsubscribe and a dropped websocket each close the session (no leak, also when the offer is still in flight); a session has no
expiry; the runaway guard; `live_unavailable` for a camera HA cannot stream or whose handler fails; `size` picks the main or
the explicitly mapped sub stream.
"""
from __future__ import annotations

import asyncio
import json
from datetime import timedelta

import pytest
from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.components.camera.const import DATA_COMPONENT
from homeassistant.components.camera.webrtc import (
    CameraWebRTCProvider,
    WebRTCAnswer,
    WebRTCCandidate,
    WebRTCError,
    async_register_webrtc_provider,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
    async_fire_time_changed,
)
from webrtc_models import RTCIceCandidateInit

from .. import live
from ..config_flow import DOMAIN
from ..push import const as push_const
from .test_editor_commands import PHONE_ID, _client, world  # noqa: F401 - fixtures by name

pytestmark = pytest.mark.asyncio

OFFER = "v=0\r\nm=video 9 UDP/TLS/RTP/SAVPF 96\r\na=recvonly\r\n"
CLIENT_CANDIDATE = {"candidate": "candidate:7 1 UDP 2122260223 10.6.1.28 52000 typ host", "sdpMid": "0",
                    "sdpMLineIndex": 0, "usernameFragment": "frag"}


class FakeCamera(Camera):
    """A camera entity. `stream` False = no STREAM feature (a still-only camera); `on` False = switched off."""

    def __init__(self, name: str, *, stream: bool = True, on: bool = True) -> None:
        super().__init__()
        self.entity_id = f"camera.{name}"
        self._attr_name = name
        self._attr_supported_features = CameraEntityFeature.STREAM if stream else CameraEntityFeature(0)
        self._attr_is_on = on
        self._stream = stream

    async def stream_source(self) -> str | None:
        return f"rtsp://fake.invalid/{self.entity_id}" if self._stream else None


class FakeWebRTC(CameraWebRTCProvider):
    """HA's provider interface (what go2rtc implements), recording every call. `mode` says what an offer does."""

    def __init__(self) -> None:
        self.offers: list[tuple[str, str, str]] = []
        self.candidates: list[tuple[str, RTCIceCandidateInit]] = []
        self.closed: list[str] = []
        self.mode = "answer"
        self.gate = asyncio.Event()          # mode "hang": the offer returns when this is set
        self.fail_candidates = False

    @property
    def domain(self) -> str:
        return "fake_webrtc"

    def async_is_supported(self, stream_source: str) -> bool:
        return True

    async def async_handle_async_webrtc_offer(self, camera, offer_sdp, session_id, send_message) -> None:
        self.offers.append((camera.entity_id, offer_sdp, session_id))
        if self.mode == "raise":
            raise HomeAssistantError("go2rtc at http://10.0.0.1:1984 refused the stream")
        if self.mode == "provider_error":
            send_message(WebRTCError("go2rtc_webrtc_offer_failed", "no stream at 10.0.0.1"))
            return
        if self.mode == "hang":
            await self.gate.wait()
            return
        send_message(WebRTCAnswer(f"v=0 FAKE-ANSWER-{len(self.offers)}"))
        send_message(WebRTCCandidate(RTCIceCandidateInit("candidate:1 1 UDP 2130706431 10.6.0.5 8555 typ host", sdp_mid="0")))

    async def async_on_webrtc_candidate(self, session_id, candidate) -> None:
        if self.fail_candidates:
            raise HomeAssistantError("go2rtc is gone")
        self.candidates.append((session_id, candidate))

    def async_close_session(self, session_id: str) -> None:
        self.closed.append(session_id)


def _cam(cid, entity, **extra):
    return {"id": cid, "label": cid.title(), "entity_id": entity, **extra}


def _page(pid, label, cameras):
    return {"id": pid, "label": label, "blocks": [{"id": pid, "title": label, "cameras": cameras, "actions": []}]}


@pytest.fixture
async def lw(world, hass_ws_client):  # noqa: F811
    """The `world` of the editor tests (admin, the phone `ep-alice`, a plain user, the tablet user) with a real camera component,
    a fake provider and cameras; the phone and the kitchen screen see `gate`, `yard`, `nostream`, `gone`, `down`, `off`, only the
    screen sees `vault` (its own page)."""
    hass = world.hass
    hass.states.async_remove("camera.gate")               # the `world` planted a bare state; the real entity takes its place
    assert await async_setup_component(hass, "camera", {})
    provider = FakeWebRTC()
    component = hass.data[DATA_COMPONENT]
    await component.async_add_entities([
        FakeCamera("gate"), FakeCamera("yard"), FakeCamera("vault"), FakeCamera("nostream", stream=False),
        FakeCamera("off", on=False), FakeCamera("gate_sub"), FakeCamera("sub_nostream", stream=False)])
    async_register_webrtc_provider(hass, provider)
    await hass.async_block_till_done()
    hass.states.async_set("camera.gone", "idle")                  # a state with no entity behind it
    hass.states.async_set("camera.down", "unavailable")
    cameras = [_cam("gate", "camera.gate", live=True), _cam("yard", "camera.yard"), _cam("nostream", "camera.nostream"),
               _cam("gone", "camera.gone"), _cam("down", "camera.down"), _cam("off", "camera.off")]
    document = {
        "schema_version": 4,
        "template": {"pages": [_page("home", "Home", cameras)], "rules": [], "roster": ["*"]},
        "overrides": {"kitchen": {"pages": [_page("vaultpage", "Vault", [_cam("vault", "camera.vault")])], "rules": [],
                                  "roster_hide": []}},
        "dashboards": {"kitchen": {"name": "Kitchen", "ha_user_id": world.tablet.id, "room": "Kitchen", "cameras": []}},
        "endpoints": {}}
    await world.store.async_save_document(document, world.store.doc_rev)
    world.provider = provider
    world.component = component
    return world


async def _open(client, camera_id="gate", msg_id=1, sdp=OFFER, **extra):
    await client.send_json({"id": msg_id, "type": "portico/content/live", "camera_id": camera_id, "sdp": sdp, **extra})
    return await client.receive_json()


async def _open_ok(client, camera_id="gate", msg_id=1, **extra):
    """A successful open with the fake provider in its default mode: the result, then its two events read and dropped."""
    result = await _open(client, camera_id, msg_id, **extra)
    assert result["success"], result
    await client.receive_json(), await client.receive_json(), await client.receive_json()
    return result["result"]


async def _call(client, type_, msg_id, **body):
    await client.send_json({"id": msg_id, "type": type_, **body})
    return await client.receive_json()


async def _until(predicate, tries=200):
    for _ in range(tries):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the condition never came true")


def _sessions(world):
    return live.registry(world.hass).sessions


# ---- the happy path -------------------------------------------------------------------------------------------------------

async def test_offer_gives_a_result_then_the_answer_and_the_candidate_as_events(lw, hass_ws_client):
    phone = await _client(lw, hass_ws_client, "phone")
    result = await _open(phone)
    assert result["success"], result
    assert set(result["result"]) == {"session_id", "stream"} and result["result"]["stream"] == "main"
    first, answer, candidate = await phone.receive_json(), await phone.receive_json(), await phone.receive_json()
    assert first["event"] == {"type": "session", "session_id": result["result"]["session_id"], "stream": "main"}
    assert first["type"] == answer["type"] == candidate["type"] == "event" and answer["id"] == candidate["id"] == 1
    assert answer["event"] == {"type": "answer", "sdp": "v=0 FAKE-ANSWER-1"}
    assert candidate["event"] == {"type": "candidate", "candidate": {
        "candidate": "candidate:1 1 UDP 2130706431 10.6.0.5 8555 typ host", "sdpMid": "0"}}
    # HA's camera got the browser's offer untouched, under HA's own session id (not the card's)
    ((entity, sdp, ha_session),) = lw.provider.offers
    assert (entity, sdp) == ("camera.gate", OFFER) and ha_session != result["result"]["session_id"]
    # nothing on the wire names an entity or a stream address
    wire = json.dumps([result, first, answer, candidate])
    assert "camera." not in wire and "rtsp" not in wire and "fake.invalid" not in wire


async def test_a_client_candidate_reaches_the_camera_and_a_bad_one_does_not(lw, hass_ws_client):
    phone = await _client(lw, hass_ws_client, "phone")
    sid = (await _open(phone))["result"]["session_id"]
    await phone.receive_json(), await phone.receive_json(), await phone.receive_json()
    ok = await _call(phone, "portico/content/live_ice", 2, session_id=sid, candidate=CLIENT_CANDIDATE)
    assert ok["success"] and ok["result"] == {}
    ((ha_session, parsed),) = lw.provider.candidates
    assert ha_session == lw.provider.offers[0][2]
    assert (parsed.candidate, parsed.sdp_mid, parsed.sdp_m_line_index, parsed.user_fragment) == (
        CLIENT_CANDIDATE["candidate"], "0", 0, "frag")
    end = await _call(phone, "portico/content/live_ice", 3, session_id=sid, candidate={"candidate": ""})
    assert end["success"]                                       # the end-of-candidates mark
    for n, bad in enumerate(({}, {"candidate": 5}, {"candidate": "x", "sdpMLineIndex": -1}, {"candidate": "x", "url": "y"},
                             {"candidate": "x" * (live.MAX_CANDIDATE + 1)}, "candidate:1"), start=4):
        resp = await _call(phone, "portico/content/live_ice", n, session_id=sid, candidate=bad)
        assert not resp["success"] and resp["error"]["code"] == "invalid_format", bad
    assert len(lw.provider.candidates) == 2


async def test_a_candidate_for_no_session_or_for_a_failing_camera(lw, hass_ws_client):
    phone = await _client(lw, hass_ws_client, "phone")
    nothing = await _call(phone, "portico/content/live_ice", 1, session_id="nope", candidate=CLIENT_CANDIDATE)
    assert not nothing["success"] and nothing["error"]["code"] == "unknown_session"
    sid = (await _open(phone, msg_id=2))["result"]["session_id"]
    await phone.receive_json(), await phone.receive_json(), await phone.receive_json()
    lw.provider.fail_candidates = True
    broken = await _call(phone, "portico/content/live_ice", 3, session_id=sid, candidate=CLIENT_CANDIDATE)
    assert not broken["success"] and broken["error"]["code"] == "live_unavailable" and "go2rtc" not in json.dumps(broken)


async def test_the_default_size_is_big_and_the_schema_checks_the_request(lw, hass_ws_client):
    phone = await _client(lw, hass_ws_client, "phone")
    assert (await _open_ok(phone, msg_id=1))["stream"] == "main"
    for n, bad in enumerate(({"size": "huge"}, {"sdp": ""}, {"sdp": "x" * (live.MAX_SDP + 1)}, {"camera_id": ""}), start=2):
        resp = await _call(phone, "portico/content/live", n, **{"camera_id": "gate", "sdp": OFFER, **bad})
        assert not resp["success"] and resp["error"]["code"] == "invalid_format", bad
    missing = await _call(phone, "portico/content/live", 9, camera_id="gate")
    assert not missing["success"] and missing["error"]["code"] == "invalid_format"
    assert len(lw.provider.offers) == 1


# ---- the grant: content/snapshot's ------------------------------------------------------------------------------------------

async def test_a_camera_outside_the_callers_endpoint_is_unknown_and_logged(lw, hass_ws_client):
    phone = await _client(lw, hass_ws_client, "phone")
    log = async_capture_events(lw.hass, "logbook_entry")
    # the screen's own `vault`, an entity id, a path, an id that was never there: all `unknown_camera`, none reaches HA
    for n, bad in enumerate(("vault", "camera.gate", "../x", "front"), start=1):
        resp = await _open(phone, bad, msg_id=n)
        assert not resp["success"] and resp["error"]["code"] == "unknown_camera", bad
    await lw.hass.async_block_till_done()
    assert not lw.provider.offers and not _sessions(lw)
    assert 'refused a live view of "vault"' in log[0].data["message"]


async def test_each_caller_gets_its_own_endpoint_and_nothing_else(lw, hass_ws_client):
    screen_user_token = lw.tokens["tablet"]
    screen = await hass_ws_client(lw.hass, access_token=screen_user_token)
    await _open_ok(screen, "vault")                                           # the declared screen: its own page, no `endpoint`
    await _open_ok(screen, "gate", msg_id=2)                                  # and the template's
    phone = await _client(lw, hass_ws_client, "phone")
    # a phone names another's endpoint, or the screen's
    other = await _open(phone, "vault", msg_id=1, endpoint="kitchen")
    assert not other["success"] and other["error"]["code"] == "unauthorized"
    # an administrator must name an endpoint, and gets that endpoint's cameras
    admin = await _client(lw, hass_ws_client)
    none = await _open(admin, "gate")
    assert not none["success"] and none["error"]["code"] == "unauthorized"
    unknown = await _open(admin, "gate", msg_id=2, endpoint="nope")
    assert not unknown["success"] and unknown["error"]["code"] == "unknown_endpoint"
    await _open_ok(admin, "vault", msg_id=3, endpoint="kitchen")
    wrong = await _open(admin, "vault", msg_id=4, endpoint=PHONE_ID)
    assert not wrong["success"] and wrong["error"]["code"] == "unknown_camera"
    # a login with no Portico role gets nothing, for any of the three commands
    plain = await _client(lw, hass_ws_client, "plain")
    for n, (kind, body) in enumerate((("live", {"camera_id": "gate", "sdp": OFFER}),
                                       ("live_ice", {"session_id": "x", "candidate": CLIENT_CANDIDATE}),
                                       ("live_close", {"session_id": "x"})), start=1):
        resp = await _call(plain, f"portico/content/{kind}", n, **body)
        assert not resp["success"] and resp["error"]["code"] == "unauthorized", kind


async def test_an_unavailable_camera_is_unavailable_and_a_session_is_only_its_owners(lw, hass_ws_client):
    phone = await _client(lw, hass_ws_client, "phone")
    down = await _open(phone, "down")
    assert not down["success"] and down["error"]["code"] == "unavailable" and not _sessions(lw)
    sid = (await _open(phone, "gate", msg_id=2))["result"]["session_id"]
    await phone.receive_json(), await phone.receive_json(), await phone.receive_json()
    # the same user on another connection (or anyone else) knows nothing of this session id
    again = await hass_ws_client(lw.hass, access_token=lw.tokens["phone"])
    ice = await _call(again, "portico/content/live_ice", 1, session_id=sid, candidate=CLIENT_CANDIDATE)
    assert not ice["success"] and ice["error"]["code"] == "unknown_session"
    close = await _call(again, "portico/content/live_close", 2, session_id=sid)
    assert close["success"] and close["result"] == {"closed": False}
    assert sid in _sessions(lw) and not lw.provider.closed and not lw.provider.candidates


# ---- closing: no leak ---------------------------------------------------------------------------------------------------

async def test_live_close_closes_the_camera_session_and_is_idempotent(lw, hass_ws_client):
    phone = await _client(lw, hass_ws_client, "phone")
    sid = (await _open(phone))["result"]["session_id"]
    await phone.receive_json(), await phone.receive_json(), await phone.receive_json()
    ha_session = lw.provider.offers[0][2]
    closed = await _call(phone, "portico/content/live_close", 2, session_id=sid)
    assert closed["success"] and closed["result"] == {"closed": True}
    assert lw.provider.closed == [ha_session] and not _sessions(lw)
    assert (await _call(phone, "portico/content/live_close", 3, session_id=sid))["result"] == {"closed": False}
    ice = await _call(phone, "portico/content/live_ice", 4, session_id=sid, candidate=CLIENT_CANDIDATE)
    assert ice["error"]["code"] == "unknown_session"
    assert lw.provider.closed == [ha_session]                      # closed once


async def test_a_dropped_websocket_closes_every_session_it_opened(lw, hass_ws_client):
    phone = await _client(lw, hass_ws_client, "phone")
    for n in (1, 2, 3):
        assert (await _open(phone, "gate", msg_id=n))["success"]
        await phone.receive_json(), await phone.receive_json(), await phone.receive_json()
    assert len(_sessions(lw)) == 3
    await phone.close()
    await _until(lambda: not _sessions(lw))
    assert sorted(lw.provider.closed) == sorted(o[2] for o in lw.provider.offers) and len(lw.provider.closed) == 3


async def test_unsubscribing_closes_the_session(lw, hass_ws_client):
    phone = await _client(lw, hass_ws_client, "phone")
    assert (await _open(phone, msg_id=5))["success"]
    await phone.receive_json(), await phone.receive_json(), await phone.receive_json()
    gone = await _call(phone, "unsubscribe_events", 6, subscription=5)
    assert gone["success"]
    assert not _sessions(lw) and lw.provider.closed == [lw.provider.offers[0][2]]


async def test_closing_while_the_offer_is_still_in_flight_leaves_nothing_behind(lw, hass_ws_client):
    """The provider registers its session AFTER our close ran (go2rtc does so while connecting): it is closed again when the
    offer returns, so nothing stays open on the camera's side."""
    lw.provider.mode = "hang"
    phone = await _client(lw, hass_ws_client, "phone")
    sid = (await _open(phone))["result"]["session_id"]
    assert (await phone.receive_json())["event"]["type"] == "session"      # sent before the (still hanging) offer
    await _until(lambda: lw.provider.offers)
    closed = await _call(phone, "portico/content/live_close", 2, session_id=sid)
    assert closed["result"] == {"closed": True} and len(lw.provider.closed) == 1
    lw.provider.gate.set()
    await _until(lambda: len(lw.provider.closed) == 2)
    assert set(lw.provider.closed) == {lw.provider.offers[0][2]} and not _sessions(lw)


async def test_a_session_has_no_expiry_while_its_websocket_is_open(lw, hass_ws_client):
    phone = await _client(lw, hass_ws_client, "phone")
    sid = (await _open(phone))["result"]["session_id"]
    await phone.receive_json(), await phone.receive_json(), await phone.receive_json()
    async_fire_time_changed(lw.hass, dt_util.utcnow() + timedelta(hours=6))
    await lw.hass.async_block_till_done()
    assert sid in _sessions(lw) and not lw.provider.closed
    ice = await _call(phone, "portico/content/live_ice", 2, session_id=sid, candidate=CLIENT_CANDIDATE)
    assert ice["success"]


# ---- live_unavailable: stills carry on -----------------------------------------------------------------------------------

@pytest.mark.parametrize("camera", ["nostream", "gone", "off"])
async def test_a_camera_home_assistant_cannot_stream_is_live_unavailable(lw, hass_ws_client, camera):
    phone = await _client(lw, hass_ws_client, "phone")
    resp = await _open(phone, camera)
    assert not resp["success"] and resp["error"]["code"] == "live_unavailable"
    assert not lw.provider.offers and not _sessions(lw)


async def test_with_no_webrtc_provider_a_streaming_camera_is_live_unavailable(lw, hass_ws_client):
    for camera in list(lw.component.entities):
        camera._webrtc_provider = None
        camera._invalidate_camera_capabilities_cache()          # HLS only, as a camera with no go2rtc is
    phone = await _client(lw, hass_ws_client, "phone")
    resp = await _open(phone)
    assert not resp["success"] and resp["error"]["code"] == "live_unavailable" and not lw.provider.offers


@pytest.mark.parametrize("mode", ["raise", "provider_error"])
async def test_a_failing_handler_is_an_error_event_and_closes_the_session(lw, hass_ws_client, mode):
    lw.provider.mode = mode
    phone = await _client(lw, hass_ws_client, "phone")
    result = await _open(phone)
    assert result["success"]                                          # as HA's own handler: the result first, then the failure
    session_event = await phone.receive_json()
    assert session_event["event"]["type"] == "session"        # the session exists once the result is out; the failure follows it
    error = await phone.receive_json()
    assert error["type"] == "event" and error["event"] == {
        "type": "error", "code": "live_unavailable", "message": "live video is not available for this camera"}
    assert "10.0.0.1" not in json.dumps(error)                        # the provider's text names internal hosts: never forwarded
    await _until(lambda: not _sessions(lw))
    assert set(lw.provider.closed) == {lw.provider.offers[0][2]}      # (a provider's error message closes it, and the returned offer once more)


# ---- the runaway guard ---------------------------------------------------------------------------------------------------

async def test_the_guard_is_very_high_and_is_not_a_product_cap():
    assert (live.MAX_LIVE_PER_ENDPOINT, live.MAX_LIVE_PER_INSTALL) == (16, 32)


async def test_the_guard_is_per_endpoint_and_per_install_and_a_close_frees_a_slot(lw, hass_ws_client, monkeypatch):
    monkeypatch.setattr(live, "MAX_LIVE_PER_ENDPOINT", 2)
    monkeypatch.setattr(live, "MAX_LIVE_PER_INSTALL", 3)
    phone = await _client(lw, hass_ws_client, "phone")
    screen = await hass_ws_client(lw.hass, access_token=lw.tokens["tablet"])
    first = await _open_ok(phone, msg_id=1)
    await _open_ok(phone, msg_id=2)
    over = await _open(phone, msg_id=3)
    assert not over["success"] and over["error"]["code"] == "too_many_live"        # this endpoint is at its limit
    await _open_ok(screen, "gate", msg_id=1)                                        # another endpoint is not
    full = await _open(screen, "gate", msg_id=2)
    assert not full["success"] and full["error"]["code"] == "too_many_live"        # the install is at ITS limit (3)
    await _call(phone, "portico/content/live_close", 4, session_id=first["session_id"])
    assert (await _open(screen, "gate", msg_id=3))["success"]                       # a slot came back


async def test_a_refused_request_takes_no_slot(lw, hass_ws_client, monkeypatch):
    monkeypatch.setattr(live, "MAX_LIVE_PER_ENDPOINT", 1)
    phone = await _client(lw, hass_ws_client, "phone")
    for n, camera in enumerate(("front", "down", "nostream"), start=1):
        assert not (await _open(phone, camera, msg_id=n))["success"]
    assert not _sessions(lw)
    assert (await _open(phone, "gate", msg_id=9))["success"]


# ---- DEV-805: who holds the slots ----------------------------------------------------------------------------------------

async def test_open_and_close_are_logged_with_the_holder_and_the_reason(lw, hass_ws_client, caplog):
    caplog.set_level("INFO", logger=live.__name__)
    phone = await _client(lw, hass_ws_client, "phone")
    opened = await _open_ok(phone, msg_id=1)
    sid = opened["session_id"]
    assert any(f"opened session {sid} of ep-alice (camera gate, main stream); now 1 for it, 1 in all" in r.getMessage()
               for r in caplog.records)
    await _call(phone, "portico/content/live_close", 2, session_id=sid)
    line = next(r.getMessage() for r in caplog.records if "closed session" in r.getMessage())
    assert sid in line and "ep-alice" in line and "camera gate" in line and "live_close" in line
    assert "now 0 for it, 0 in all" in line


async def test_a_dropped_socket_and_a_failed_offer_are_named_in_the_close_line(lw, hass_ws_client, caplog):
    caplog.set_level("INFO", logger=live.__name__)
    phone = await _client(lw, hass_ws_client, "phone")
    await _open_ok(phone, msg_id=1)
    await phone.close()
    await _until(lambda: not _sessions(lw))
    assert any("socket closed" in r.getMessage() for r in caplog.records if "closed session" in r.getMessage())


async def test_the_refusal_lists_what_the_endpoint_holds(lw, hass_ws_client, monkeypatch, caplog):
    monkeypatch.setattr(live, "MAX_LIVE_PER_ENDPOINT", 2)
    phone = await _client(lw, hass_ws_client, "phone")
    first = await _open_ok(phone, msg_id=1)
    second = await _open_ok(phone, "yard", msg_id=2)
    assert not (await _open(phone, msg_id=3))["success"]
    line = next(r.getMessage() for r in caplog.records if "refused a session" in r.getMessage())
    assert "2 of 2 for it" in line and "endpoint ep-alice" in line
    assert f"{first['session_id']} (gate, main, " in line and f"{second['session_id']} (yard, main, " in line
    assert line.index(first["session_id"]) < line.index(second["session_id"])      # oldest first


# ---- size: main or an explicitly mapped sub stream ---------------------------------------------------------------------------

def _settings_entry(hass, options):
    entry = MockConfigEntry(domain=DOMAIN, data={push_const.ENTRY_TYPE: push_const.ENTRY_TYPE_PUSH}, options=options)
    entry.add_to_hass(hass)
    return entry


async def _stream_of(lw, hass_ws_client, size, msg_id=1):
    phone = await _client(lw, hass_ws_client, "phone")
    result = await _open(phone, "gate", msg_id=msg_id, size=size)
    assert result["success"], result
    return result["result"]["stream"], lw.provider.offers[-1][0]


@pytest.mark.parametrize("size", ["big", "small"])
async def test_with_no_mapping_both_sizes_use_the_main_stream(lw, hass_ws_client, size):
    assert await _stream_of(lw, hass_ws_client, size) == ("main", "camera.gate")


@pytest.mark.parametrize("options", [
    {live.CONF_LIVE_SUB_CAMERAS: ["camera.gate", "camera.gate_sub"]},        # not a mapping
    {live.CONF_LIVE_SUB_CAMERAS: {"camera.gate": "light.gate_sub"}},         # not a camera
    {live.CONF_LIVE_SUB_CAMERAS: {"camera.gate": "camera.gate"}},            # itself
    {live.CONF_LIVE_SUB_CAMERAS: {"camera.gate": 5}},
    {live.CONF_LIVE_SUB_CAMERAS: {"camera.yard": "camera.gate_sub"}},        # another camera's sub
    {}])
async def test_a_mapping_that_is_not_a_sub_stream_for_this_camera_is_ignored(lw, hass_ws_client, options):
    _settings_entry(lw.hass, options)
    assert await _stream_of(lw, hass_ws_client, "small") == ("main", "camera.gate")


async def test_small_takes_the_mapped_sub_stream_and_big_takes_the_main_one(lw, hass_ws_client):
    _settings_entry(lw.hass, {live.CONF_LIVE_SUB_CAMERAS: {"camera.gate": "camera.gate_sub"}})
    assert await _stream_of(lw, hass_ws_client, "small", 1) == ("sub", "camera.gate_sub")
    assert await _stream_of(lw, hass_ws_client, "big", 2) == ("main", "camera.gate")


@pytest.mark.parametrize("sub", ["sub_nostream", "gone", "down"])
async def test_a_sub_stream_that_cannot_stream_falls_back_to_the_main_one(lw, hass_ws_client, sub):
    _settings_entry(lw.hass, {live.CONF_LIVE_SUB_CAMERAS: {"camera.gate": f"camera.{sub}"}})
    assert await _stream_of(lw, hass_ws_client, "small") == ("main", "camera.gate")


async def test_the_mapping_does_not_widen_the_grant(lw, hass_ws_client):
    """The sub entity is reached only THROUGH a granted camera: asking for it by any name is `unknown_camera`."""
    _settings_entry(lw.hass, {live.CONF_LIVE_SUB_CAMERAS: {"camera.gate": "camera.gate_sub"}})
    phone = await _client(lw, hass_ws_client, "phone")
    for n, name in enumerate(("gate_sub", "camera.gate_sub"), start=1):
        resp = await _open(phone, name, msg_id=n, size="small")
        assert not resp["success"] and resp["error"]["code"] == "unknown_camera"


# ---- the grant for a caller's camera (DEV-713 S2-1): every `c-` id the caller block lists, and no other ---------------------------

def _caller_camera_ids(result):
    return [c["id"] for caller in result["callers"].values() for c in caller["cameras"]]


async def test_every_c_id_the_caller_block_lists_opens_live_and_no_other_c_id_does(lw, hass_ws_client):
    from ..content_resolve import short_id

    phone = await _client(lw, hass_ws_client, "phone")
    got = await _call(phone, "portico/content/get", 1)
    listed = _caller_camera_ids(got["result"])
    assert listed == [short_id("c", "camera.gate")], "the gate door's camera is the one caller camera of this world"
    for n, cid in enumerate(listed, start=2):
        await _open_ok(phone, cid, msg_id=n)                      # the camera is the caller's, not a page tile: the same grant
    assert [entity for entity, _sdp, _session in lw.provider.offers] == ["camera.gate"]
    # an entity that exists but is nobody's caller camera, and an id that was never there
    for n, bad in enumerate((short_id("c", "camera.yard"), short_id("c", "camera.vault"), "c-000000000000"), start=10):
        resp = await _open(phone, bad, msg_id=n)
        assert not resp["success"] and resp["error"]["code"] == "unknown_camera", bad
    assert len(lw.provider.offers) == 1


async def test_a_rule_decides_which_c_ids_are_listed_and_the_grant_follows_it(lw, hass_ws_client):
    from ..content_resolve import short_id

    gate, yard = short_id("c", "camera.gate"), short_id("c", "camera.yard")
    phone = await _client(lw, hass_ws_client, "phone")

    async def with_cameras(cameras, msg_id):
        document = lw.store.document()
        document["template"]["rules"] = [{"id": "r1", "caller": "kind:door", "cameras": cameras, "ring_buttons": [],
                                          "call_buttons": []}]
        await lw.store.async_save_document(document, lw.store.doc_rev)
        got = await _call(phone, "portico/content/get", msg_id)
        assert got["success"], got
        return _caller_camera_ids(got["result"])

    assert await with_cameras(["camera.yard"], 1) == [yard]       # the rule swaps the camera
    assert (await _open(phone, gate, msg_id=2))["error"]["code"] == "unknown_camera", "no longer listed: no longer granted"
    await _open_ok(phone, yard, msg_id=3)
    assert await with_cameras([], 4) == []                        # the rule shows none
    assert (await _open(phone, yard, msg_id=5))["error"]["code"] == "unknown_camera"
    assert await with_cameras(["camera.gate", "camera.yard"], 6) == [gate, yard]      # two cameras: both listed, both granted
    await _open_ok(phone, gate, msg_id=7)
    await _open_ok(phone, yard, msg_id=8)


# ---- DEV-768: the element's own small stream ------------------------------------------------------------------------------

async def _element_world(lw, small):
    """The `gate` tile replaced by a reference to a camera element whose small stream is `small`."""
    from ..elements import get_elements_store

    elements = get_elements_store(lw.hass)
    el = (await elements.async_set({"kind": "camera", "name": "Gate", "entity_id": "camera.gate",
                                    "options": {"small_entity_id": small}}, elements.rev))["element"]
    cameras = [{"id": "gate", "element": el["id"], "live": True}, _cam("yard", "camera.yard")]
    document = {"schema_version": 5, "template": {"pages": [_page("home", "Home", cameras)], "rules": [], "roster": ["*"]},
                "overrides": {}, "dashboards": {}, "endpoints": {}}
    await lw.store.async_save_document(document, lw.store.doc_rev)


async def test_small_takes_the_elements_small_stream_and_big_the_main_one(lw, hass_ws_client):
    await _element_world(lw, "camera.gate_sub")
    assert await _stream_of(lw, hass_ws_client, "small", 1) == ("sub", "camera.gate_sub")
    assert await _stream_of(lw, hass_ws_client, "big", 2) == ("main", "camera.gate")


async def test_the_elements_small_stream_wins_over_live_sub_cameras(lw, hass_ws_client):
    _settings_entry(lw.hass, {live.CONF_LIVE_SUB_CAMERAS: {"camera.gate": "camera.yard"}})
    await _element_world(lw, "camera.gate_sub")
    assert await _stream_of(lw, hass_ws_client, "small") == ("sub", "camera.gate_sub")


@pytest.mark.parametrize("sub", ["sub_nostream", "gone", "down"])
async def test_an_elements_small_stream_that_cannot_stream_falls_back_to_main(lw, hass_ws_client, sub):
    await _element_world(lw, f"camera.{sub}")
    assert await _stream_of(lw, hass_ws_client, "small") == ("main", "camera.gate")


async def test_the_elements_small_stream_does_not_widen_the_grant(lw, hass_ws_client):
    await _element_world(lw, "camera.gate_sub")
    phone = await _client(lw, hass_ws_client, "phone")
    resp = await _open(phone, "camera.gate_sub", size="small")
    assert not resp["success"] and resp["error"]["code"] == "unknown_camera"
