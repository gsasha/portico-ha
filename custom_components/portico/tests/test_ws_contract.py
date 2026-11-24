# ruff: noqa: F811 - test parameters named after the imported fixtures
"""contract/ws_v3.json: the WebSocket exchange of contract 3, recorded from the REAL handlers (signaling.py,
away_media.py, editor_commands.py) over a real HA websocket connection, with `FakeProvider` standing in for the
driver part. HA-harness session.

Contract 3 (M6 E3) replaces contract 2 with no aliases: `portico/doors`, `portico/calls/subscribe` and
`portico/content/subscribe` for `devices` and `subscribe`, `door_id` for `device_id`, no `entry_id`, no secrets, no
flat `phone`/`cameras`/`actions` in `content/get`, no `profile`, `content/list` gone, push registration refused below
0.5.0. The file records all of it; `ws_v2.json` and `ws_push_v1.json` are retired with the code they recorded.

Each case is one request as a client sends it and every frame HA sends back. `as` is the caller: `phone` an enrolled
phone's dedicated HA user, `screen` a user declared for the dashboard endpoint `kitchen`, `user` an ordinary HA user
(neither administrator, phone nor screen), `admin` an administrator. `_meta.auth` is `auth.COMMANDS`; the test fails
when the file differs from what the handlers emit, and when a case's outcome disagrees with the table.

After an intended change, regenerate and review the diff:

    PORTICO_REGENERATE_WS_CONTRACT=1 python -m pytest -o asyncio_mode=auto \\
        custom_components/portico/tests/test_ws_contract.py

(in the estate's container add `-e PORTICO_REGENERATE_WS_CONTRACT=1` and `--user "$(id -u):$(id -g)"`).

Three companions: `contract/app_commands_v3.txt` is the list of commands the app sends (the app's
`WsContractTest::commandsSentEqualTheVendoredList` checks its code against the same file, and the test here checks every
one is served, in the auth table and in this file), `contract/ws_media_v2.json` is the away-media exchange
(test_away_media.py replays it) and `contract/push_v2.json` the push messages (test_push_wire.py records it).
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import types
from datetime import UTC, datetime
from pathlib import Path

import pytest
from homeassistant.components.camera import Image
from homeassistant.config_entries import ConfigEntryDisabler
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_mock_service

from custom_components.portico import auth, live
from custom_components.portico.config_flow import DOMAIN
from custom_components.portico.content import get_content_store
from custom_components.portico.provider import DATA_PROVIDERS, ProviderError
from custom_components.portico.push import const
from custom_components.portico.push import registry as registry_module

from .fake_provider import FakeProvider
from .test_push_registry import (  # noqa: F401 - fixtures by name
    _custom_integrations,
    _manager,
    _phone_user,
    _push_entry,
    google,
)

ROOT = Path(__file__).resolve().parents[3] / "contract"
CONTRACT = ROOT / "ws_v3.json"
APP_COMMANDS = ROOT / "app_commands_v3.txt"
MEDIA_CONTRACT = ROOT / "ws_media_v2.json"
REGENERATE_ENV = "PORTICO_REGENERATE_WS_CONTRACT"

NOW = datetime(2026, 10, 6, 10, 0, 0, tzinfo=UTC)
GATE_ENTRY = "01J9ENTRYGATE0000000000000"
GATE_ID = "http://10.6.0.7:8091"
FRONT_ID = "vto-front.example"
PHONE_NAME = "Alice's Pixel"
PHONE_ENDPOINT = "ep-alice"
SCREEN = "kitchen"
INSTALLATION = "inst-0f1e2d3c4b5a"
# Fake values only: none of these is, or looks like, a real credential.
REGISTRATION = "FAKE-fcm-registration-0001"
REGISTRATION_ROTATED = "FAKE-fcm-registration-0002"

# the classes each caller of the file belongs to (auth.caller_classes, written out)
CALLER_CLASSES = {
    "phone": {auth.ANY, auth.PHONE},
    "screen": {auth.ANY, auth.DASHBOARD},
    "user": {auth.ANY},
    "admin": {auth.ANY, auth.ADMIN},
}
# the codes a refusal by the table carries (auth.REFUSALS)
REFUSAL_CODES = {"unauthorized", "not_enrolled"}
# Cases from a caller INSIDE the command's row that the handler itself refuses (`unauthorized`, signaling._content_target):
# a phone or a screen naming an endpoint that is not its own, an administrator naming none. The wire code is the same as
# the table's, so these are listed by name for the agreement test.
HANDLER_REFUSALS = frozenset({
    "content get: a declared screen names another's endpoint",
    "content get: a phone names another's endpoint",
    "content get: an admin names none",
    "content subscribe: a phone names another's endpoint",
    "live: phone names another's endpoint",
    "live: an admin names none",
})

META = {
    "contract": "portico-ws",
    "version": 3,
    "description": (
        "The WebSocket frames between a Portico client (portico-app, the dashboard card, the panel) and the Portico "
        "integration, contract 3: one case per request, with every frame HA sends back in order, recorded from the "
        "integration's real handlers over a real Home Assistant websocket connection, with a fake door provider behind "
        "them. `as` is the caller: `phone` is an enrolled phone's dedicated HA user, `screen` a user declared for the "
        "dashboard endpoint `kitchen`, `user` an ordinary HA user (an administrator, an enrolled phone and a declared "
        "screen it is not), `admin` an administrator. `auth` is who may send each command (classes: admin, phone, "
        "dashboard, any; one table, custom_components/portico/auth.py); a caller outside a command's classes gets an "
        "`unauthorized` error (`not_enrolled` for portico/media_open). Message ids are per connection, as recorded. "
        "Errors with code invalid_format come from Home Assistant's own schema check; their message text is HA's and "
        "may change with HA versions. HA user ids are replaced by `user-<name>`, and `doc_rev` (a hash of a document that "
        "names HA user ids) by `<doc_rev>`; `content/save` is recorded only as a refusal and a stale save, because a "
        "good save changes the document."
    ),
    "consumers": "portico-app's MockWebServer tests replay these frames; the card and panel node tests are written "
                 "against the same shapes.",
    "deterministic": "reg_id comes from a counter instead of uuid4, the registry clock is fixed at "
                     "2026-10-06T10:00:00+00:00, the content rate limiter's clock is fixed; call ids and door ids come "
                     "from the fake provider; everything else is as emitted.",
    "fake_values": "Every token, registration and id here is a fake test value.",
    "producer": "custom_components/portico/tests/test_ws_contract.py; regenerate with "
                f"{REGENERATE_ENV}=1 (see that file). The test fails when this file drifts from the handlers.",
}


@pytest.fixture
def deterministic(monkeypatch):
    counter = iter(range(1, 1000))
    monkeypatch.setattr(registry_module, "uuid", types.SimpleNamespace(
        uuid4=lambda: types.SimpleNamespace(hex=f"{next(counter):032x}")))
    real = registry_module.dt_util
    monkeypatch.setattr(registry_module, "dt_util", types.SimpleNamespace(
        utcnow=lambda: NOW, parse_datetime=real.parse_datetime))
    live_ids = iter(range(1, 1000))
    monkeypatch.setattr(live, "new_session_id", lambda: f"live-{next(live_ids)}")


@pytest.fixture
def content_fakes(monkeypatch):
    """The still a camera returns, and the clock the content rate limits read, are fixed so the recorded content
    frames are deterministic."""
    async def _image(hass, entity_id, timeout=10, width=None, height=None):
        return Image(content_type="image/jpeg", content=b"FAKE-JPEG-0001")

    monkeypatch.setattr("homeassistant.components.camera.async_get_image", _image)
    monkeypatch.setattr("custom_components.portico.content.time", types.SimpleNamespace(monotonic=lambda: 1000.0))


CONTENT_PROFILE = {
    "cameras": [
        {"id": "gate", "label": "Gate", "entity_id": "camera.gate"},
        {"id": "garage", "label": "Garage", "entity_id": "camera.garage"},
    ],
    "actions": [
        {"id": "open-front", "label": "Front door", "kind": "lock_open", "entity_id": "lock.front", "confirm": True},
        {"id": "evening", "label": "Evening", "kind": "scene_activate", "entity_id": "scene.evening", "confirm": False},
        {"id": "gate-relay", "label": "Gate", "kind": "portico_open", "device_id": GATE_ID, "confirm": True},
    ],
}

# the kitchen screen's home page, from the template (the phone's own override replaces it for the phone)
SCREEN_PAGES = [{"id": "home", "label": "Home",
                 "cameras": [{"id": "porch", "label": "Porch", "entity_id": "camera.gate"}],
                 "actions": [{"id": "evening", "label": "Evening", "kind": "scene_activate",
                              "entity_id": "scene.evening", "confirm": False}]}]


class _Recorder:
    """One connection per caller; every case is a request and the frames back."""

    def __init__(self, clients: dict) -> None:
        self.clients = clients
        self.ids = dict.fromkeys(clients, 0)
        self.cases: list[dict] = []

    async def case(self, name: str, caller: str, body: dict, frames: int = 1) -> list[dict]:
        self.ids[caller] += 1
        request = {"id": self.ids[caller], **body}
        client = self.clients[caller]
        await client.send_json(request)
        received = [await asyncio.wait_for(client.receive_json(), timeout=5) for _ in range(frames)]
        self.cases.append({"name": name, "as": caller, "send": request, "receive": received})
        return received

    async def more(self, caller: str, frames: int, into: dict | None = None) -> list[dict]:
        """Frames that arrive later on a subscription: appended to the last case, or to `into`."""
        client = self.clients[caller]
        received = [await asyncio.wait_for(client.receive_json(), timeout=5) for _ in range(frames)]
        (into or self.cases[-1])["receive"].extend(received)
        return received


def _register(**overrides) -> dict:
    return {"type": "portico/push/register", "installation_id": INSTALLATION, "kind": "fid",
            "registration": REGISTRATION, "package": const.PORTICO_PACKAGE,
            "fcm_project_id": "portico-test", "app_version": "0.5.0-alpha", **overrides}


def _blank_doc_rev(node):
    if isinstance(node, dict):
        return {k: ("<doc_rev>" if k == "doc_rev" else _blank_doc_rev(v)) for k, v in node.items()}
    if isinstance(node, list):
        return [_blank_doc_rev(v) for v in node]
    return node


async def _scrub_user_ids(hass, cases: list[dict]) -> list[dict]:
    """HA generates user ids at random; the recording names them by the user's name."""
    text = json.dumps(cases)
    for user in await hass.auth.async_get_users():
        label = "user-" + re.sub(r"[^a-z0-9]+", "-", user.name.lower()).strip("-")
        text = text.replace(user.id, label)
    return _blank_doc_rev(json.loads(text))


def _blocks_page() -> dict:
    """The phone's home page as blocks: a block with a door (a bare `portico_open`, a coloured icon-only unlock, Talk,
    a scene, three cameras of which one is explicitly big) and a block without one (a gate cover)."""
    return {"id": "home", "label": "Home", "blocks": [
        {"id": "entrance", "title": "Entrance", "door": GATE_ID,
         "cameras": [{"id": "gate", "label": "Gate", "entity_id": "camera.gate"},
                     {"id": "garage", "label": "Garage", "entity_id": "camera.garage", "size": "big"},
                     {"id": "porch", "label": "Porch", "entity_id": "camera.porch"}],
         "actions": [{"id": "open-door", "label": "Open the door", "kind": "portico_open", "confirm": True},
                     {"id": "unlock-front", "label": "Unlock", "kind": "lock_unlock", "entity_id": "lock.front",
                      "confirm": True, "color": "red", "icon": "lock-open-variant", "show_label": False},
                     {"id": "talk", "label": "Talk", "kind": "talk", "confirm": False},
                     {"id": "evening", "label": "Evening", "kind": "scene_activate", "entity_id": "scene.evening",
                      "confirm": False}]},
        {"id": "garden", "title": "Garden",
         "cameras": [{"id": "lawn", "label": "Lawn", "entity_id": "camera.lawn", "size": "small"}],
         "actions": [{"id": "gate-cover", "label": "Open the gate", "kind": "cover_open", "entity_id": "cover.gate",
                      "confirm": True, "color": "green"}]}]}


def _draft_of(page: dict) -> dict:
    return {"schema_version": 4, "template": {"pages": [page], "rules": [], "roster": ["*"]}, "overrides": {},
            "dashboards": {}, "endpoints": {}}


async def _record_blocks(hass, hass_ws_client, store, phone_token, rec) -> None:
    """Cases for blocks, on fresh connections (ids start at 1 again), appended to `rec.cases`.

    The case NAMES below are the app's lookup keys (portico-app replays this file by name), so they are kept as they
    were when B8 removed the flat form: "flat form beside them" and "the flat mirror" now name what is GONE, and the
    two "flat edit" previews are refusals (`invalid`, "update the Portico panel")."""
    hass.states.async_set("camera.porch", "idle", {"friendly_name": "Porch camera"})
    hass.states.async_set("camera.lawn", "idle", {"friendly_name": "Lawn camera"})
    hass.states.async_set("cover.gate", "closed", {"friendly_name": "Gate cover"})
    unlocked = async_mock_service(hass, "lock", "unlock")
    covered = async_mock_service(hass, "cover", "open_cover")
    document = store.document()
    document["overrides"][PHONE_ENDPOINT]["pages"] = [_blocks_page()]
    await store.async_save_document(document, store.doc_rev)
    fresh = _Recorder({"phone": await hass_ws_client(hass, access_token=phone_token),
                       "admin": await hass_ws_client(hass)})
    await fresh.case("blocks, content get: a phone with two blocks (door, sizes, colour, Talk off, flat form beside them)",
                     "phone", {"type": "portico/content/get"})
    await fresh.case("blocks, content document: admin (schema 4: blocks and the flat mirror)", "admin",
                     {"type": "portico/content/document"})
    one_block = _blocks_page()
    one_block["blocks"] = one_block["blocks"][:1]
    await fresh.case("blocks, content preview: a draft whose door does not exist (door_available false)", "admin",
                     {"type": "portico/content/preview", "endpoint": PHONE_ENDPOINT,
                      "document": _draft_of({**_blocks_page(), "blocks": [
                          {**_blocks_page()["blocks"][0], "door": "http://10.6.0.99:1"}, _blocks_page()["blocks"][1]]})})
    flat_edit = {**one_block, **{"cameras": [{"id": "gate", "label": "Front gate", "entity_id": "camera.gate"}],
                                 "actions": [{"id": "evening", "label": "Tonight", "kind": "scene_activate",
                                              "entity_id": "scene.evening", "confirm": False}]}}
    await fresh.case("blocks, content preview: a flat edit from the panel on disk on a one-block page", "admin",
                     {"type": "portico/content/preview", "endpoint": PHONE_ENDPOINT, "document": _draft_of(flat_edit)})
    multi = {**_blocks_page(), "cameras": [{"id": "gate", "label": "Front gate", "entity_id": "camera.gate"}],
             "actions": []}
    await fresh.case("blocks, content preview: a flat edit on a page of several blocks is refused", "admin",
                     {"type": "portico/content/preview", "endpoint": PHONE_ENDPOINT, "document": _draft_of(multi)})
    await fresh.case("blocks, content preview: a schema 3 document is refused", "admin",
                     {"type": "portico/content/preview", "endpoint": PHONE_ENDPOINT,
                      "document": {**_draft_of(one_block), "schema_version": 3}})
    await fresh.case("blocks, content save: a schema 3 document with a stale revision conflicts first", "admin",
                     {"type": "portico/content/save", "base_doc_rev": "stale",
                      "document": {**_draft_of(one_block), "schema_version": 3}})
    await fresh.case("blocks, content run: lock_unlock", "phone", {"type": "portico/content/run", "action_id": "unlock-front"})
    await fresh.case("blocks, content run: cover_open in the second block", "phone",
                     {"type": "portico/content/run", "action_id": "gate-cover"})
    await fresh.case("blocks, content run: talk is in the grant and refused (switched off)", "phone",
                     {"type": "portico/content/run", "action_id": "talk"})
    await fresh.case("blocks, content run: talk by an administrator naming the phone is not_allowed", "admin",
                     {"type": "portico/content/run", "action_id": "talk", "endpoint": PHONE_ENDPOINT})
    screen_draft = _draft_of(_blocks_page())
    screen_draft["dashboards"] = {SCREEN: store.document()["dashboards"][SCREEN]}
    await fresh.case("blocks, content preview: a Talk button on a screen's page is off, phone_only", "admin",
                     {"type": "portico/content/preview", "endpoint": SCREEN, "document": screen_draft})
    await hass.async_block_till_done()
    assert len(unlocked) == 1 and len(covered) == 1
    await fresh.case("blocks, content snapshot: width 1920", "phone",
                     {"type": "portico/content/snapshot", "camera_id": "lawn", "width": 1920})
    await fresh.case("blocks, content snapshot: width 1921 (invalid_format)", "phone",
                     {"type": "portico/content/snapshot", "camera_id": "lawn", "width": 1921})
    rec.cases.extend(fresh.cases)


HOUSE_BODIES = {
    "portico/house/answer": {"call_id": "hso-1", "client_kind": "app"},
    "portico/house/decline": {"call_id": "hso-1"},
    "portico/house/hangup": {"call_id": "hso-1"},
    "portico/house/presence": {"dnd": True},
}
HOUSE_MEDIA_OPEN = {"type": "portico/house/media_open", "call_id": "hso-1"}      # 3b-S: recorded on its own connections


async def _record_house(hass, hass_ws_client, tokens, rec, phone_user_id) -> None:
    """The four house commands (M7 3a), appended last on fresh connections so no earlier frame moved: first with house
    calls OFF (the shipped default: `not_available` for a caller in the row, `unauthorized` outside it), then ON with the
    phone's `receive` switched on: the Everyone key in the screen's roster, the screen ringing the phone, the phone
    answering and the screen hanging up, with the `house_call` and `house_roster_changed` events as they arrive."""
    from custom_components.portico.house import async_setup_house
    from .house_env import phone_entry

    fresh = _Recorder({"phone": await hass_ws_client(hass, access_token=tokens["phone"]),
                       "screen": await hass_ws_client(hass, access_token=tokens["screen"]),
                       "user": await hass_ws_client(hass, access_token=tokens["user"]),
                       "admin": await hass_ws_client(hass)})
    for command, body in HOUSE_BODIES.items():
        for caller in ("phone", "screen", "user", "admin"):
            await fresh.case(f"{command.split('/', 1)[1]}: {caller} (house calls off)", caller, {"type": command, **body})
    # 3b-S: the new command OFF, on connections of its own so no id of an earlier case moved
    off_media = _Recorder({"phone": await hass_ws_client(hass, access_token=tokens["phone"]),
                           "screen": await hass_ws_client(hass, access_token=tokens["screen"]),
                           "user": await hass_ws_client(hass, access_token=tokens["user"]),
                           "admin": await hass_ws_client(hass)})
    for caller in ("phone", "screen", "user", "admin"):
        await off_media.case(f"media_open: {caller} (house calls off)", caller, HOUSE_MEDIA_OPEN)

    # --- ON: nothing below is reachable on a real install until a later PR switches house calls on -------------------
    # the phone becomes a phone entry with receive on (it is a row on a disabled door entry in the recording above)
    phone_entry(hass, phone_user_id, PHONE_ENDPOINT, PHONE_NAME, receive=True)
    ids = iter(range(1, 100))
    house = async_setup_house(hass, enabled=True, id_factory=lambda: f"hso-{next(ids)}")
    assert house is not None
    on = " (house calls on)"
    await fresh.case("house/presence: not subscribed" + on, "phone", {"type": "portico/house/presence", "dnd": False})
    await fresh.case("calls subscribe: phone, with house calls on" + on, "phone", {"type": "portico/calls/subscribe"}, frames=3)
    phone_sub = fresh.cases[-1]
    await fresh.case("calls subscribe: screen, with house calls on" + on, "screen", {"type": "portico/calls/subscribe"}, frames=3)
    screen_sub = fresh.cases[-1]
    await fresh.more("phone", 1, phone_sub)                    # house_roster_changed: the screen came online
    await fresh.case("house/presence: subscribed" + on, "phone", {"type": "portico/house/presence", "dnd": False})
    roster = await fresh.case("content get: the screen's roster leads with Everyone" + on, "screen",
                              {"type": "portico/content/get"})
    (everyone,) = [e for e in roster[0]["result"]["roster"] if e["kind"] == "group"]
    run = await fresh.case("content run: Everyone from the screen" + on, "screen",
                           {"type": "portico/content/run", "action_id": everyone["action_id"]}, frames=2)
    await fresh.more("phone", 1, phone_sub)                    # the ring
    assert run[1]["result"]["call_id"] == "hso-1" and run[1]["result"]["media"] is None
    await fresh.case("house/answer: the phone answers" + on, "phone", {"type": "portico/house/answer", **HOUSE_BODIES["portico/house/answer"]}, frames=2)
    await fresh.more("screen", 1, screen_sub)                  # answered (screen side)
    await fresh.case("house/answer: a second answer" + on, "phone", {"type": "portico/house/answer", **HOUSE_BODIES["portico/house/answer"]})
    await fresh.case("house/hangup: the screen ends it" + on, "screen", {"type": "portico/house/hangup", **HOUSE_BODIES["portico/house/hangup"]}, frames=2)
    await fresh.more("phone", 1, phone_sub)                    # ended (phone side)
    await fresh.case("house/decline: an administrator, no such call" + on, "admin", {"type": "portico/house/decline", **HOUSE_BODIES["portico/house/decline"]})
    await fresh.case("house/answer: ordinary user" + on, "user", {"type": "portico/house/answer", **HOUSE_BODIES["portico/house/answer"]})
    # --- 3b-S, appended after everything above so no earlier frame moved: the audio leg of an answered call. No provider
    # here runs the house-call bridge (the fake is a version-1 provider with none of the optional methods, like the
    # driver part's 0.5.x wheel), so an end of an answered call is told `not_supported`.
    media = {**HOUSE_MEDIA_OPEN, "call_id": "hso-2"}
    await fresh.case("house/media_open: the call is not answered yet" + on, "phone", media)
    get_content_store(hass)._last_run.clear()                  # the same key within RUN_INTERVAL is `rate_limited`
    run = await fresh.case("content run: Everyone again" + on, "screen",
                           {"type": "portico/content/run", "action_id": everyone["action_id"]}, frames=2)
    await fresh.more("phone", 1)                    # the ring
    assert run[1]["result"]["call_id"] == "hso-2"
    await fresh.case("house/answer: the phone answers the second call" + on, "phone",
                     {"type": "portico/house/answer", "call_id": "hso-2", "client_kind": "app"}, frames=2)
    await fresh.more("screen", 1)                  # answered (screen side)
    await fresh.case("house/media_open: no bridge on this Home Assistant" + on, "phone", media)
    await fresh.case("house/media_open: the screen, no bridge" + on, "screen", media)
    await fresh.case("house/media_open: an administrator is not in the call" + on, "admin", media)
    await fresh.case("house/media_open: ordinary user" + on, "user", media)
    await fresh.case("house/hangup: the phone ends the second call" + on, "phone",
                     {"type": "portico/house/hangup", "call_id": "hso-2"}, frames=2)
    await fresh.more("screen", 1)                  # ended (screen side)
    await fresh.case("house/media_open: the call is over" + on, "phone", media)
    rec.cases.extend(fresh.cases)
    rec.cases.extend(off_media.cases)


TALK_CALL = "out-0123456789abcdef"


async def _record_talk(hass, hass_ws_client, tokens, fake, store) -> list[dict]:
    """`talk_state` (M7 P3-B): what a phone's `portico/calls/subscribe` carries while it talks to the gate, with the gate
    standing in as a door that reports `CanDial` (no real door does yet). Recorded on fresh connections BEFORE the house
    cases (house calls are still off here) and returned, to be appended after them, so no earlier frame or case moved.
    The kitchen screen is the bystander: its subscription gets nothing, and the case after shows its next frame is its
    own answer. The door goes back to what it was."""
    gate = dict(name="Vehicle gate", driver="gate_sip_observer", camera_entity_id="camera.gate")
    fake.add_door(GATE_ID, capabilities={"DirectCapable": False, "HasRelay": True, "CanDial": True}, **gate)
    original_dial = fake.dial

    async def dial(door_id, host, port):
        fake._record("dial", door_id, host, port)
        fake._owner[TALK_CALL] = door_id
        fake.outbound(door_id, TALK_CALL, "in_call", "dialing")      # the driver's StateAnswered, before the reply
        return {"ok": True, "call_id": TALK_CALL}

    fake.dial = dial
    fresh = _Recorder({"phone": await hass_ws_client(hass, access_token=tokens["phone"]),
                       "screen": await hass_ws_client(hass, access_token=tokens["screen"])})
    on = " (talk to the gate)"
    await fresh.case("calls subscribe: phone" + on, "phone", {"type": "portico/calls/subscribe"}, frames=3)
    phone_sub = fresh.cases[-1]
    await fresh.case("calls subscribe: screen" + on, "screen", {"type": "portico/calls/subscribe"}, frames=3)
    store._last_run.clear()
    await fresh.case("content run: talk starts, the phone hears dialing and in_call before the reply" + on, "phone",
                     {"type": "portico/content/run", "action_id": "talk"}, frames=3)
    fake.outbound(GATE_ID, TALK_CALL, "ended", "in_call")           # the gate hung up
    await fresh.more("phone", 1, phone_sub)
    await fresh.case("version: the screen heard nothing of it, its next frame is its own answer" + on, "screen",
                     {"type": "portico/version"})
    store._last_run.clear()
    store._talk_calls.clear()          # the fake publishes no outbound state, so the store's own memory of the call is dropped here
    fake.raise_on["dial"] = ProviderError("busy", "the gate is busy")
    await fresh.case("content run: talk refused by the gate, dialing then ended with the reason" + on, "phone",
                     {"type": "portico/content/run", "action_id": "talk"}, frames=3)
    del fake.raise_on["dial"]
    await _record_gate_target(fresh, hass_ws_client, hass, fake, store)
    fake.dial = original_dial
    fake.add_door(GATE_ID, capabilities={"DirectCapable": False, "HasRelay": True}, **gate)
    return fresh.cases


async def _record_gate_target(fresh, hass_ws_client, hass, fake, store) -> None:
    """The gate as a roster endpoint and call target (M7 3f), with the gate standing in as a door that reports `CanDial`.
    Appended to the Talk cases, on the same connections: the gate's roster entry carries `can_dial: true` (and is callable
    for a phone, off with `phone_only` for the screen), the editor's endpoint row carries `can_dial`, and a press of the
    gate's roster key dials through Talk's path (the same three frames as the Talk press). With the dial off none of these
    carries a new key: every earlier case is that recording. The document is not touched."""
    on = " (gate as a call target)"
    fresh.clients["admin"], fresh.ids["admin"] = await hass_ws_client(hass), 0
    got = await fresh.case("content get: the gate is in the roster, callable, with can_dial" + on, "phone",
                           {"type": "portico/content/get"})
    (gate_entry,) = [e for e in got[0]["result"]["roster"] if e.get("can_dial")]
    await fresh.case("content get: a screen sees the gate off with phone_only" + on, "screen",
                     {"type": "portico/content/get"})
    await fresh.case("content document: the gate's endpoint row carries can_dial" + on, "admin",
                     {"type": "portico/content/document"})
    # DEV-695: a call button aimed at the gate carries `gate_call: true` on the action itself, so a phone whose roster
    # hides the gate (`roster_hide`) can still tell. Previews of a draft (nothing is saved), the gate's endpoint id as the target.
    call_gate = {"id": "call-gate", "label": "Gate", "kind": "call", "target": {"type": "door", "id": gate_entry["id"]},
                 "confirm": False}
    call_page = {"id": "home", "label": "Home", "blocks": [
        {"id": "entrance", "title": "Entrance", "cameras": [], "actions": [call_gate]}]}
    draft = _draft_of(call_page)
    await fresh.case("content preview: a call button aimed at the gate carries gate_call" + on, "admin",
                     {"type": "portico/content/preview", "endpoint": PHONE_ENDPOINT, "document": draft})
    hidden = {**draft, "overrides": {PHONE_ENDPOINT: {"pages": [], "rules": [], "roster_hide": [gate_entry["id"]]}}}
    await fresh.case("content preview: the gate hidden from the roster, the call button still carries gate_call" + on,
                     "admin", {"type": "portico/content/preview", "endpoint": PHONE_ENDPOINT, "document": hidden})
    store._last_run.clear()
    store._talk_calls.clear()
    await fresh.case("content run: the gate's roster key dials through Talk's path" + on, "phone",
                     {"type": "portico/content/run", "action_id": gate_entry["action_id"]}, frames=3)
    fake.outbound(GATE_ID, TALK_CALL, "ended", "in_call")           # the gate hung up
    await fresh.more("phone", 1)
    store._last_run.clear()
    store._talk_calls.clear()


async def _record_cameras_info(hass, hass_ws_client, tokens, rec) -> None:
    """The panel's picker command `portico/cameras/info` (administrators only; the phone app never sends it), appended
    after every earlier case, on fresh connections (no subscription event is waiting on them). A camera with a size in its attributes, one without (a 1280x720 PNG header is what the
    patched `async_get_image` returns), one unavailable, a non-camera id, and the callers outside the row."""
    from unittest.mock import patch

    hass.states.async_set("camera.spec_attrs", "idle", {"friendly_name": "Spec attrs", "width": 1600, "height": 1200, "fps": 20})
    hass.states.async_set("camera.spec_still", "idle", {"friendly_name": "Spec still"})
    png = (b"\x89PNG\r\n\x1a\n" + (13).to_bytes(4, "big") + b"IHDR" + (1280).to_bytes(4, "big") + (720).to_bytes(4, "big")
           + b"\x08\x02\x00\x00\x00")

    async def _still(hass, entity_id, timeout=10, width=None, height=None):
        return Image(content_type="image/png", content=png)

    ids = ["camera.spec_attrs", "camera.spec_still", "camera.garage"]
    fresh = _Recorder({"phone": await hass_ws_client(hass, access_token=tokens["phone"]),
                       "screen": await hass_ws_client(hass, access_token=tokens["screen"]),
                       "user": await hass_ws_client(hass, access_token=tokens["user"]),
                       "admin": await hass_ws_client(hass)})
    with patch("homeassistant.components.camera.async_get_image", _still):
        await fresh.case("cameras info: admin (attributes, a still's header, an unavailable camera)", "admin",
                       {"type": "portico/cameras/info", "entity_ids": ids})
    await fresh.case("cameras info: admin, a non-camera id is refused", "admin",
                   {"type": "portico/cameras/info", "entity_ids": ["camera.spec_attrs", "lock.front"]})
    await fresh.case("cameras info: admin, more than 20 ids (invalid_format)", "admin",
                   {"type": "portico/cameras/info", "entity_ids": [f"camera.c{n}" for n in range(21)]})
    await fresh.case("cameras info: enrolled phone", "phone", {"type": "portico/cameras/info", "entity_ids": ids})
    await fresh.case("cameras info: declared screen", "screen", {"type": "portico/cameras/info", "entity_ids": ids})
    await fresh.case("cameras info: ordinary user", "user", {"type": "portico/cameras/info", "entity_ids": ids})
    # DEV-824 P5: the editor banner's read of the YAML files (administrators only, read-only; the phone app never sends it)
    for who, label in (("admin", "admin (no portico folder: a quiet state)"), ("phone", "enrolled phone"), ("screen", "declared screen"), ("user", "ordinary user")):
        await fresh.case(f"config status: {label}", who, {"type": "portico/config/status"})
    # the panel's live window asks Frigate's go2rtc for the answer (administrators only; the phone app never sends it).
    # No network and no Frigate entry here: the refusals before any request, and the callers outside the row.
    offer = {"type": "portico/camera/webrtc_offer", "entity_id": "camera.spec_still", "sdp": "FAKE-offer"}
    await fresh.case("camera webrtc_offer: admin, a non-camera id is refused (invalid_entity)", "admin",
                   {**offer, "entity_id": "lock.front"})
    await fresh.case("camera webrtc_offer: admin, a camera Home Assistant does not know (not_found)", "admin",
                   {**offer, "entity_id": "camera.nowhere"})
    await fresh.case("camera webrtc_offer: enrolled phone", "phone", offer)
    await fresh.case("camera webrtc_offer: declared screen", "screen", offer)
    await fresh.case("camera webrtc_offer: ordinary user", "user", offer)
    rec.cases.extend(fresh.cases)


PHONES_EXTERNAL_URL = "https://ha.example.org"
PHONES_NEW_PHONE = "Yael's Pixel"
_PLACEHOLDER_TIME = "<iso time>"


async def _record_phones(hass, hass_ws_client, tokens, rec, push) -> None:
    """The Phones tab's three admin commands (UI split P2), appended after every earlier case on connections of their
    own. The list before any Tailscale answer, the typed refusals of `enroll` in the core's order (name, address, URL),
    then with the answer `manual` and an https URL a QR drawn, drawn again (the earlier one dies), listed as waiting,
    cancelled (the phone it made is removed) and cancelled again; each command from a caller outside its row too.
    Random values are blanked after the recording: the phone entry's id, the QR token (32 random bytes) and the expiry
    time (the clock). The PNG is never in a frame."""
    from custom_components.portico.phone_entry import phone_entries

    fresh = _Recorder({"phone": await hass_ws_client(hass, access_token=tokens["phone"]),
                       "screen": await hass_ws_client(hass, access_token=tokens["screen"]),
                       "user": await hass_ws_client(hass, access_token=tokens["user"]),
                       "admin": await hass_ws_client(hass)})
    list_msg = {"type": "portico/phones/list"}
    enroll = {"type": "portico/phones/enroll", "name": PHONES_NEW_PHONE, "allow_http": False, "ha_dial": ""}
    await fresh.case("phones list: admin (no Tailscale answer yet, one legacy phone)", "admin", list_msg)
    await fresh.case("phones list: enrolled phone", "phone", list_msg)
    await fresh.case("phones list: declared screen", "screen", list_msg)
    await fresh.case("phones list: ordinary user", "user", list_msg)
    await fresh.case("phones enroll: ordinary user", "user", enroll)
    await fresh.case("phones enroll: enrolled phone", "phone", enroll)
    await fresh.case("phones enroll: declared screen", "screen", enroll)
    await fresh.case("phones enroll: admin, a blank name (bad_name)", "admin", {**enroll, "name": "   "})
    await fresh.case("phones enroll: admin, an address the app would refuse (invalid_ha_dial)", "admin",
                     {**enroll, "ha_dial": "8.8.8.8:443"})
    await fresh.case("phones enroll: admin, Home Assistant has no URL (no_ha_url)", "admin", enroll)
    hass.config.external_url = "http://ha.example.org:8123"
    await fresh.case("phones enroll: admin, only a plain http URL (no_https_url)", "admin", enroll)
    hass.config.external_url = PHONES_EXTERNAL_URL
    await fresh.case("phones enroll: admin, no Tailscale answer yet (needs_tailscale_setup)", "admin", enroll)
    hass.config_entries.async_update_entry(push, data={**push.data, "tailscale_mode": "manual"})
    await fresh.case("phones enroll: admin, bad schema (name missing)", "admin",
                     {"type": "portico/phones/enroll", "allow_http": False, "ha_dial": ""})
    created = await fresh.case("phones enroll: admin, a QR is drawn (manual mode)", "admin",
                               {**enroll, "ha_dial": "10.6.0.5:443"})
    assert created[0]["success"] and created[0]["result"]["result"] == "created" and created[0]["result"]["created"]
    entry_id = created[0]["result"]["entry_id"]
    first_token = created[0]["result"]["qr_token"]
    await fresh.case("phones list: admin (the new phone is waiting for its first scan)", "admin", list_msg)
    again = await fresh.case("phones enroll: admin, the same name again (a new code, the earlier QR dies)", "admin",
                             {**enroll, "ha_dial": "10.6.0.5:443"})
    assert again[0]["result"]["entry_id"] == entry_id and again[0]["result"]["created"] is False
    second_token = again[0]["result"]["qr_token"]
    cancel = {"type": "portico/phones/cancel", "entry_id": entry_id}
    await fresh.case("phones cancel: ordinary user", "user", cancel)
    await fresh.case("phones cancel: enrolled phone", "phone", cancel)
    await fresh.case("phones cancel: declared screen", "screen", cancel)
    await fresh.case("phones cancel: admin, the phone that QR made is removed", "admin", cancel)
    await fresh.case("phones cancel: admin, nothing left to cancel (unknown_phone)", "admin", cancel)
    await fresh.case("phones cancel: admin, bad schema (entry_id missing)", "admin", {"type": "portico/phones/cancel"})
    # UI split P3: set and revoke, on a phone of their own (pending: it has no push registration yet)
    made = await fresh.case("phones enroll: admin, a phone for the set and revoke cases", "admin",
                            {**enroll, "name": "Revoke me", "ha_dial": "10.6.0.5:443"})
    made_id, made_token = made[0]["result"]["entry_id"], made[0]["result"]["qr_token"]

    def set_msg(patch):
        return {"type": "portico/phones/set", "entry_id": made_id, "patch": patch}

    for who, label in (("user", "ordinary user"), ("phone", "enrolled phone"), ("screen", "declared screen")):
        await fresh.case(f"phones set: {label}", who, set_msg({"house_receive": True}))
    await fresh.case("phones set: admin, an empty patch (bad_patch)", "admin", set_msg({}))
    await fresh.case("phones set: admin, a door Portico does not have (unknown_door)", "admin",
                     set_msg({"doors": ["no-such-door"]}))
    await fresh.case("phones set: admin, the push ring notification before the app has registered (no_registration)", "admin",
                     set_msg({"away_ring": True}))
    await fresh.case("phones set: admin, house calls on", "admin", set_msg({"house_receive": True}))
    await fresh.case("phones set: admin, every door", "admin", set_msg({"doors": None}))
    await fresh.case("phones set: admin, no such phone (unknown_phone)", "admin",
                     {**set_msg({"house_receive": True}), "entry_id": "no-such-entry"})
    await fresh.case("phones set: admin, bad schema (patch missing)", "admin",
                     {"type": "portico/phones/set", "entry_id": made_id})
    # DEV-807: the deprecated alias "make a panel screen" (kept one release; the slug is ignored, no screen is declared)
    panel = {"type": "portico/phones/make_panel_screen", "entry_id": made_id, "slug": "revoke-me-panel"}
    for who, label in (("user", "ordinary user"), ("phone", "enrolled phone"), ("screen", "declared screen")):
        await fresh.case(f"phones make_panel_screen: {label}", who, panel)
    await fresh.case("phones make_panel_screen: admin, no such phone (unknown_phone)", "admin",
                     {**panel, "entry_id": "no-such-entry"})
    await fresh.case("phones make_panel_screen: admin, the phone becomes a panel (the slug is ignored)", "admin", panel)
    await fresh.case("phones make_panel_screen: admin, again (already_panel)", "admin", panel)
    # own pages: the command that replaces it (administrators only)
    make = {"type": "portico/phones/make_panel", "entry_id": made_id}
    for who, label in (("user", "ordinary user"), ("phone", "enrolled phone"), ("screen", "declared screen")):
        await fresh.case(f"phones make_panel: {label}", who, make)
    await fresh.case("phones make_panel: admin, no such phone (unknown_phone)", "admin", {**make, "entry_id": "no-such-entry"})
    await fresh.case("phones make_panel: admin, already a panel (already_panel)", "admin", make)
    revoke = {"type": "portico/phones/revoke", "entry_id": made_id, "confirm": True}
    for who, label in (("user", "ordinary user"), ("phone", "enrolled phone"), ("screen", "declared screen")):
        await fresh.case(f"phones revoke: {label}", who, revoke)
    await fresh.case("phones revoke: admin, not confirmed (confirmation_required)", "admin", {**revoke, "confirm": False})
    await fresh.case("phones revoke: admin, confirmed (the phone, its user and its tokens go)", "admin", revoke)
    await fresh.case("phones revoke: admin, again (unknown_phone)", "admin", revoke)
    await fresh.case("phones revoke: admin, bad schema (confirm missing)", "admin",
                     {"type": "portico/phones/revoke", "entry_id": made_id})
    blanks = {entry_id: "<phone entry id>", first_token: "<qr_token>", second_token: "<qr_token>",
              made_id: "<phone entry id>", made_token: "<qr_token>"}
    for known in phone_entries(hass):
        blanks.setdefault(known.entry_id, "<entry id of " + known.data["name"] + ">")
    for case in fresh.cases:
        text = json.dumps(case, ensure_ascii=False)
        for value, label in blanks.items():
            text = text.replace(value, label)
        case.clear()
        case.update(_blank_times(json.loads(text)))
    rec.cases.extend(fresh.cases)


async def _record_settings_tab(hass, hass_ws_client, tokens, rec) -> None:
    """The Settings tab (UI split P4 to P6), appended last on connections of its own: `portico/door_status`,
    `portico/door_settings/get | set` and `portico/ui_settings/get | set`, administrators only. The recording has no door entry
    of the unified shape (the one entry is the old format and disabled), so the door commands record their empty and refused
    forms; the door cases proper (a real door, the idle rule, the reload) are in tests/test_door_commands.py. The push ring notification
    preferences are recorded on the real settings entry, ending with every value as it began. `as_of` is the clock."""
    fresh = _Recorder({"phone": await hass_ws_client(hass, access_token=tokens["phone"]),
                       "screen": await hass_ws_client(hass, access_token=tokens["screen"]),
                       "user": await hass_ws_client(hass, access_token=tokens["user"]),
                       "admin": await hass_ws_client(hass)})
    refused = (("user", "ordinary user"), ("phone", "enrolled phone"), ("screen", "declared screen"))
    status = {"type": "portico/door_status"}
    door_get = {"type": "portico/door_settings/get"}
    door_set = {"type": "portico/door_settings/set", "entry_id": "no-such-entry", "revision": "r",
                "patch": {"away_media": True}}
    for who, label in refused:
        await fresh.case(f"door status: {label}", who, status)
        await fresh.case(f"door settings get: {label}", who, door_get)
        await fresh.case(f"door settings set: {label}", who, door_set)
    await fresh.case("door status: admin, no door yet", "admin", status)
    await fresh.case("door settings get: admin, no door yet", "admin", door_get)
    await fresh.case("door settings set: admin, no such door (unknown_door)", "admin", door_set)
    await fresh.case("door settings set: admin, bad schema (revision missing)", "admin",
                     {"type": "portico/door_settings/set", "entry_id": "no-such-entry", "patch": {"away_media": True}})

    ui_get = {"type": "portico/ui_settings/get"}
    ui_put = {"type": "portico/ui_settings/set", "patch": {"ring_enabled": False}, "confirm": True}
    for who, label in refused:
        await fresh.case(f"ui settings get: {label}", who, ui_get)
        await fresh.case(f"ui settings set: {label}", who, ui_put)
    # one registered phone for the test list to name (the earlier cases left none): the registry is the real one
    from custom_components.portico.push import get_manager

    row = await get_manager(hass).registry.async_register(
        ha_user_id="contract-user", installation_id="contract-installation", kind="fid", registration=REGISTRATION,
        package=const.PORTICO_PACKAGE, fcm_project_id="portico-test", app_version="0.5.0-alpha", name="Contract phone")
    reg_id = row["reg_id"]
    await fresh.case("ui settings get: admin", "admin", ui_get)
    await fresh.case("ui settings set: admin, validate only", "admin", {**ui_put, "validate_only": True, "confirm": False})
    await fresh.case("ui settings set: admin, the ring window alone needs no confirmation", "admin",
                     {"type": "portico/ui_settings/set", "patch": {"ring_window": 60}})
    await fresh.case("ui settings set: admin, not confirmed (confirmation_required)", "admin", {**ui_put, "confirm": False})
    await fresh.case("ui settings set: admin, the push ring notification off", "admin", ui_put)
    await fresh.case("ui settings set: admin, a test phone", "admin",
                     {"type": "portico/ui_settings/set", "patch": {"test_phones": [reg_id]}, "confirm": True})
    await fresh.case("ui settings set: admin, a phone that is not registered (unknown_phone)", "admin",
                     {"type": "portico/ui_settings/set", "patch": {"test_phones": ["no-such-registration"]}, "confirm": True})
    await fresh.case("ui settings set: admin, new phones on", "admin",
                     {"type": "portico/ui_settings/set", "patch": {"new_phones_enabled": True}, "confirm": True})
    await fresh.case("ui settings set: admin, a value out of range (bad_patch)", "admin",
                     {"type": "portico/ui_settings/set", "patch": {"ring_window": 5}, "confirm": True})
    await fresh.case("ui settings set: admin, nothing changes (unchanged)", "admin",
                     {"type": "portico/ui_settings/set", "patch": {"new_phones_enabled": True}})
    await fresh.case("ui settings set: admin, back as it began", "admin",
                     {"type": "portico/ui_settings/set", "patch": {"ring_enabled": True, "ring_window": 45, "new_phones_enabled": False,
                                                                    "test_phones": []}, "confirm": True})
    await fresh.case("ui settings set: admin, bad schema (patch missing)", "admin", {"type": "portico/ui_settings/set"})
    rec.cases.extend(_blank_times(case) for case in fresh.cases)


def _blank_minted_enrol_ids(case: dict) -> None:
    """The managed Talk elements (DEVICES D1) are derived from the enrolments the earlier cases made. `ep-alice` and the
    kitchen screen are fixed; the phones enrolled by `phones/enroll` get a random enrolment id (and so a random
    `talk-phone-<id>` element id and a registry `rev`). Those are not values to record: each becomes a numbered placeholder."""
    import re
    text = json.dumps(case["receive"])
    minted = []
    for found in re.findall(r"talk-phone-(?!ep-alice\b)([A-Za-z0-9_-]{20,})", text):
        if found not in minted:
            minted.append(found)
    for n, found in enumerate(minted, 1):
        text = text.replace(found, f"<enrol id {n}>")
    case["receive"] = json.loads(text)
    case["receive"][0]["result"]["rev"] = "<rev>"


async def _record_elements(hass, hass_ws_client, tokens, rec) -> None:
    """The Elements page (DEV-760 E2), appended last on connections of its own: `portico/elements/list | set | delete`,
    administrators only. The store is empty here. A create is not recorded as a success (the server mints a random id);
    tests/test_elements_commands.py covers it, so the admin cases are the registry read, a stale revision, a refused
    element, a validate-only check of a good one is not recorded for the same reason, and an unknown id."""
    fresh = _Recorder({"phone": await hass_ws_client(hass, access_token=tokens["phone"]),
                       "screen": await hass_ws_client(hass, access_token=tokens["screen"]),
                       "user": await hass_ws_client(hass, access_token=tokens["user"]),
                       "admin": await hass_ws_client(hass)})
    listing = {"type": "portico/elements/list"}
    put = {"type": "portico/elements/set", "element": {"kind": "status", "name": "Hall", "entity_id": "sensor.hall"},
           "base_rev": "stale"}
    drop = {"type": "portico/elements/delete", "element_id": "el-00000000", "base_rev": "stale"}
    for who, label in (("user", "ordinary user"), ("phone", "enrolled phone"), ("screen", "declared screen")):
        await fresh.case(f"elements list: {label}", who, listing)
        await fresh.case(f"elements set: {label}", who, put)
        await fresh.case(f"elements delete: {label}", who, drop)
    await fresh.case("elements list: admin, no elements yet (the registry the Add form is built from)", "admin", listing)
    _blank_minted_enrol_ids(fresh.cases[-1])
    await fresh.case("elements set: admin, a stale base_rev (conflict)", "admin", put)
    await fresh.case("elements set: admin, an entity id that is not one (invalid, field entity_id)", "admin",
                     {**put, "element": {**put["element"], "entity_id": "nope"}})
    await fresh.case("elements set: admin, bad schema (base_rev missing)", "admin",
                     {"type": "portico/elements/set", "element": {}})
    await fresh.case("elements delete: admin, a stale base_rev (conflict)", "admin", drop)
    rec.cases.extend(fresh.cases)


async def _record_pages_migrate(hass, hass_ws_client, tokens, rec) -> None:
    """The Pages migration (Pages unified P-A), appended last on connections of its own: `portico/content/pages_migrate`,
    administrators only. The dry run is recorded (it writes nothing); an apply is recorded only as the refusals, because a
    good apply changes the document for every case after it. tests/test_pages_migrate.py covers the apply."""
    fresh = _Recorder({"phone": await hass_ws_client(hass, access_token=tokens["phone"]),
                       "screen": await hass_ws_client(hass, access_token=tokens["screen"]),
                       "user": await hass_ws_client(hass, access_token=tokens["user"]),
                       "admin": await hass_ws_client(hass)})
    dry = {"type": "portico/content/pages_migrate", "validate_only": True}
    for who, label in (("user", "ordinary user"), ("phone", "enrolled phone"), ("screen", "declared screen")):
        await fresh.case(f"pages migrate: {label}", who, dry)
    await fresh.case("pages migrate: admin, the dry run (the report; nothing is written)", "admin", dry)
    await fresh.case("pages migrate: admin, an apply with no base_doc_rev (invalid)", "admin",
                     {"type": "portico/content/pages_migrate", "validate_only": False})
    await fresh.case("pages migrate: admin, an apply on a stale base_doc_rev (conflict)", "admin",
                     {"type": "portico/content/pages_migrate", "validate_only": False, "base_doc_rev": "stale"})
    # the ring-pages migration (ring pages per Talk element, R-B): the recorded document is still schema 4, so the admin dry
    # run is the `not_pages_model` refusal; tests/test_ring_pages_migrate.py covers the report and the apply
    ring_dry = {"type": "portico/content/ring_pages_migrate", "dry_run": True}
    for who, label in (("user", "ordinary user"), ("phone", "enrolled phone"), ("screen", "declared screen")):
        await fresh.case(f"ring pages migrate: {label}", who, ring_dry)
    await fresh.case("ring pages migrate: admin, the dry run on a schema 4 document (not_pages_model)", "admin", ring_dry)
    await fresh.case("ring pages migrate: admin, an apply with no base_doc_rev (invalid)", "admin",
                     {"type": "portico/content/ring_pages_migrate", "dry_run": False})
    rec.cases.extend(fresh.cases)


async def _record_live(hass, hass_ws_client, tokens, rec, screen_user_id) -> None:
    """Live video of a camera tile (live.py), appended last on connections of their own: `portico/content/live` (a
    subscription: the result, then the answer and the candidate as events), `live_ice` and `live_close`, and the `live` flag of
    a camera in a previewed document. A FAKE camera component and a FAKE WebRTC provider stand in for Home Assistant's own
    camera support (tests/test_live.py); session ids come from a counter. The snapshot's grant, case by case: the phone's own
    cameras, the screen's own, an administrator naming an endpoint, a login with no role."""
    from unittest.mock import patch

    from homeassistant.components.camera.const import DATA_COMPONENT
    from homeassistant.components.camera.webrtc import async_register_webrtc_provider

    from .test_live import FakeCamera, FakeWebRTC

    hass.states.async_remove("camera.gate")           # a bare state from the first cases; the real entity takes its place
    assert await async_setup_component(hass, "camera", {})
    provider = FakeWebRTC()
    await hass.data[DATA_COMPONENT].async_add_entities([FakeCamera("gate"), FakeCamera("garden", stream=False)])
    async_register_webrtc_provider(hass, provider)
    await hass.async_block_till_done()
    hass.states.async_set("camera.lawn", "idle")      # a state with no camera entity behind it: Home Assistant cannot stream it
    offer = "v=0 FAKE-offer"
    candidate = {"candidate": "candidate:7 1 UDP 2122260223 10.6.1.28 52000 typ host", "sdpMid": "0", "sdpMLineIndex": 0}
    fresh = _Recorder({"phone": await hass_ws_client(hass, access_token=tokens["phone"]),
                       "screen": await hass_ws_client(hass, access_token=tokens["screen"]),
                       "user": await hass_ws_client(hass, access_token=tokens["user"]),
                       "admin": await hass_ws_client(hass)})
    live_msg = {"type": "portico/content/live", "sdp": offer}
    opened = await fresh.case("live: phone, its own camera (the result, the session event, the answer and a candidate)", "phone",
                              {**live_msg, "camera_id": "gate"}, frames=4)
    assert opened[0]["result"]["session_id"] == "live-1"
    await fresh.case("live: phone, a small tile with no sub stream mapped uses the main stream", "phone",
                     {**live_msg, "camera_id": "gate", "size": "small"}, frames=4)
    await fresh.case("live: phone, a camera that is not in its profile (unknown_camera)", "phone",
                     {**live_msg, "camera_id": "front"})
    await fresh.case("live: phone, a camera that is unavailable", "phone", {**live_msg, "camera_id": "garage"})
    await fresh.case("live: phone, a camera Home Assistant cannot stream (live_unavailable)", "phone",
                     {**live_msg, "camera_id": "lawn"})
    await fresh.case("live: phone, a size that is not big or small (invalid_format)", "phone",
                     {**live_msg, "camera_id": "gate", "size": "huge"})
    await fresh.case("live: phone names another's endpoint", "phone", {**live_msg, "camera_id": "porch", "endpoint": SCREEN})
    await fresh.case("live: a declared screen, its own camera", "screen", {**live_msg, "camera_id": "porch"}, frames=4)
    await fresh.case("live: ordinary user", "user", {**live_msg, "camera_id": "gate"})
    await fresh.case("live: an admin names none", "admin", {**live_msg, "camera_id": "gate"})
    await fresh.case("live: admin names the screen's endpoint", "admin",
                     {**live_msg, "camera_id": "porch", "endpoint": SCREEN}, frames=4)
    await fresh.case("live: admin names the phone's endpoint, a camera it does not have (unknown_camera)", "admin",
                     {**live_msg, "camera_id": "nope", "endpoint": PHONE_ENDPOINT})
    # the guard: one live stream allowed for the phone's endpoint while this runs (the real limit is 16, 32 in all)
    with patch.object(live, "MAX_LIVE_PER_ENDPOINT", 2):
        await fresh.case("live: phone, too many live streams (too_many_live)", "phone", {**live_msg, "camera_id": "gate"})
    ice = {"type": "portico/content/live_ice", "session_id": "live-1", "candidate": candidate}
    await fresh.case("live_ice: phone, a candidate", "phone", ice)
    await fresh.case("live_ice: phone, the end-of-candidates mark", "phone", {**ice, "candidate": {"candidate": ""}})
    await fresh.case("live_ice: phone, a session that does not exist (unknown_session)", "phone",
                     {**ice, "session_id": "nope"})
    await fresh.case("live_ice: phone, not a candidate (invalid_format)", "phone", {**ice, "candidate": {"sdpMid": "0"}})
    await fresh.case("live_ice: the screen cannot use the phone's session", "screen", ice)
    await fresh.case("live_ice: ordinary user", "user", ice)
    close = {"type": "portico/content/live_close", "session_id": "live-1"}
    await fresh.case("live_close: the screen cannot close the phone's session", "screen", close)
    await fresh.case("live_close: ordinary user", "user", close)
    await fresh.case("live_close: phone", "phone", close)
    await fresh.case("live_close: phone, again (nothing is open: closed false)", "phone", close)
    provider.mode = "provider_error"
    await fresh.case("live: phone, the camera's handler fails (the result, the session event, then an error event)", "phone",
                     {**live_msg, "camera_id": "gate"}, frames=3)
    provider.mode = "answer"
    # the `live` flag of a camera, carried through a draft the way the card would receive it
    page = _blocks_page()
    page["blocks"][0]["cameras"][0]["live"] = True
    await fresh.case("live flag: preview of a draft whose first camera is live", "admin",
                     {"type": "portico/content/preview", "endpoint": PHONE_ENDPOINT, "document": _draft_of(page)})
    page["blocks"][0]["cameras"][0]["live"] = "yes"
    await fresh.case("live flag: preview with a live that is not a boolean is refused", "admin",
                     {"type": "portico/content/preview", "endpoint": PHONE_ENDPOINT, "document": _draft_of(page)})
    rec.cases.extend(fresh.cases)


async def _record_show_fps(hass, hass_ws_client, rec) -> None:
    """`show_fps` (live video, part 2): the document-level boolean "show the frame rate on live pictures", appended last on a
    connection of its own. Carried on the resolved document the way `home_name` is, only when true; a save or a preview refuses
    anything that is not a boolean. The cases before these are unchanged: a document without it resolves byte-identically."""
    fresh = _Recorder({"admin": await hass_ws_client(hass)})
    page = _blocks_page()
    await fresh.case("show_fps: preview of a draft with the frame rate on", "admin",
                     {"type": "portico/content/preview", "endpoint": PHONE_ENDPOINT, "document": {**_draft_of(page), "show_fps": True}})
    await fresh.case("show_fps: preview of a draft with it off carries nothing", "admin",
                     {"type": "portico/content/preview", "endpoint": PHONE_ENDPOINT, "document": {**_draft_of(page), "show_fps": False}})
    await fresh.case("show_fps: preview with a show_fps that is not a boolean is refused", "admin",
                     {"type": "portico/content/preview", "endpoint": PHONE_ENDPOINT, "document": {**_draft_of(page), "show_fps": "yes"}})
    rec.cases.extend(fresh.cases)


async def _record_live_rtsp(hass, hass_ws_client, rec) -> None:
    """`live_rtsp` (DEV-713 S1b-1): the per-camera RTSP stream hint for the phone app's away route, appended last on a connection
    of its own. COMPUTED per request, never stored: it is on a camera item of a resolved document (`content/preview` here;
    `content/get` and `content/document` carry the same item) only while the settings entry's `live_rtsp_base` is set, only
    on a `live: true` tile of a Frigate camera (a state with `camera_name` and `client_id`), and `sub` only for a camera named in
    `live_sub_streams`. The options are written through `portico/ui_settings/set` (admin, confirmed). Every case before these is
    unchanged: with no address the preview is what it always was. The recording ends with both options cleared, as it began."""
    fresh = _Recorder({"admin": await hass_ws_client(hass)})
    hass.states.async_set("camera.frigate_gate", "idle", {"friendly_name": "Gate", "camera_name": "gate_intercom",
                                                         "client_id": "frigate"})
    hass.states.async_set("camera.frigate_yard", "idle", {"friendly_name": "Yard", "camera_name": "yard", "client_id": "frigate"})
    hass.states.async_set("camera.not_frigate", "idle", {"friendly_name": "Tablet"})
    page = {"id": "home", "label": "Home", "blocks": [{"id": "live", "title": "Live", "cameras": [
        {"id": "gate", "label": "Gate", "entity_id": "camera.frigate_gate", "live": True},
        {"id": "yard", "label": "Yard", "entity_id": "camera.frigate_yard", "live": True},
        {"id": "still", "label": "Still", "entity_id": "camera.frigate_gate"},
        {"id": "tablet", "label": "Tablet", "entity_id": "camera.not_frigate", "live": True}], "actions": []}]}
    preview = {"type": "portico/content/preview", "endpoint": PHONE_ENDPOINT, "document": _draft_of(page)}
    put = {"type": "portico/ui_settings/set", "patch": {"live_rtsp_base": "rtsp://10.6.0.6:8554", "live_sub_streams": ["gate_intercom"]},
           "confirm": True}
    await fresh.case("live_rtsp: preview with no address set carries none", "admin", preview)
    await fresh.case("live_rtsp: ui settings, an address with credentials is refused (bad_patch)", "admin",
                     {**put, "patch": {"live_rtsp_base": "rtsp://user:pass@10.6.0.6:8554"}})
    await fresh.case("live_rtsp: ui settings, an address with a path is refused (bad_patch)", "admin",
                     {**put, "patch": {"live_rtsp_base": "rtsp://10.6.0.6:8554/gate"}})
    await fresh.case("live_rtsp: ui settings, a camera name that is not a name is refused (bad_patch)", "admin",
                     {**put, "patch": {"live_sub_streams": ["gate/intercom"]}})
    await fresh.case("live_rtsp: ui settings, not confirmed (confirmation_required)", "admin", {**put, "confirm": False})
    await fresh.case("live_rtsp: ui settings, validate only", "admin", {**put, "validate_only": True, "confirm": False})
    await fresh.case("live_rtsp: ui settings, the address and the sub-stream list", "admin", put)
    await fresh.case("live_rtsp: ui settings get, with both set", "admin", {"type": "portico/ui_settings/get"})
    await fresh.case("live_rtsp: preview, a live Frigate tile has main and (when listed) sub; a tile that is not live, or not Frigate, none",
                     "admin", preview)
    await fresh.case("live_rtsp: ui settings, the list alone cleared (the address stays, no sub)", "admin",
                     {**put, "patch": {"live_sub_streams": []}})
    await fresh.case("live_rtsp: preview with no sub-stream list has main only", "admin", preview)
    await fresh.case("live_rtsp: ui settings, back as it began (both cleared)", "admin",
                     {**put, "patch": {"live_rtsp_base": "", "live_sub_streams": []}})
    await fresh.case("live_rtsp: preview after clearing carries none again", "admin", preview)
    rec.cases.extend(fresh.cases)


async def _record_caller_live_rtsp(hass, hass_ws_client, rec) -> None:
    """`live_rtsp` on the CALLER's cameras (DEV-713 S2-1), appended last on a connection of its own: the cameras the ring and in-call
    screens show (`callers.<id>.cameras[]` of the resolved document, ids `c-...`) carry the same hint as a live page camera, on
    the same terms, except that no `live: true` is needed (a caller's camera has no such flag). Here the gate door's camera
    (`c-30102768d913`) is made a Frigate camera. The recording ends with both options cleared, as it began."""
    fresh = _Recorder({"admin": await hass_ws_client(hass)})
    hass.states.async_set("camera.gate", "idle", {"friendly_name": "Gate camera", "camera_name": "gate_intercom", "client_id": "frigate"})
    preview = {"type": "portico/content/preview", "endpoint": PHONE_ENDPOINT, "document": _draft_of(_blocks_page())}
    put = {"type": "portico/ui_settings/set", "patch": {"live_rtsp_base": "rtsp://10.6.0.6:8554", "live_sub_streams": ["gate_intercom"]},
           "confirm": True}
    await fresh.case("caller live_rtsp: preview with no address set, the caller's camera carries none", "admin", preview)
    await fresh.case("caller live_rtsp: ui settings, the address and the sub-stream list", "admin", put)
    await fresh.case("caller live_rtsp: preview, the caller's Frigate camera has main and sub (no live flag needed)", "admin", preview)
    await fresh.case("caller live_rtsp: ui settings, the list alone cleared (the address stays, no sub)", "admin",
                     {**put, "patch": {"live_sub_streams": []}})
    await fresh.case("caller live_rtsp: preview, the caller's camera has main only", "admin", preview)
    hass.states.async_set("camera.gate", "idle", {"friendly_name": "Gate camera"})
    await fresh.case("caller live_rtsp: preview, a caller camera that is not a Frigate camera carries none", "admin", preview)
    await fresh.case("caller live_rtsp: ui settings, back as it began (both cleared)", "admin",
                     {**put, "patch": {"live_rtsp_base": "", "live_sub_streams": []}})
    await fresh.case("caller live_rtsp: preview after clearing carries none again", "admin", preview)
    rec.cases.extend(fresh.cases)


CONNLOG_REPORT = {
    "v": 1, "id": "rep-0001", "app": "0.6.7-alpha", "sdk": 35, "t0": 1761000000000, "dur": 2330, "result": "connected",
    "trigger": {"kind": "net_change", "from": "wifi", "to": "cellular", "validated": True},
    "env": {"fg": False, "doze": False, "saver": False, "metered": True, "proc_ms": 183000, "cold": False},
    "net": [{"t": 0, "def": "wifi", "wifi": 1, "cell": 1, "vpn": 0, "val": 1},
            {"t": 410, "def": "cellular", "wifi": 0, "cell": 1, "vpn": 0, "val": 1}],
    "hint": {"mode": "TAILNET_FIRST", "why": "mobile data, no validated Wi-Fi: not the home LAN", "mobile": 1},
    "ev": [{"t": 6, "e": "tailnet_started"}, {"t": 13, "e": "direct_failed", "cls": "NAME_NOT_RESOLVED"},
           {"t": 2300, "e": "tailnet_open"}, {"t": 2330, "e": "connected"}],
    "tsnet": "needslogin+13ms starting+31ms running+287ms", "path": "tailnet",
}


async def _record_connlog(hass, hass_ws_client, tokens, rec) -> None:
    """The connection log (DEV-705), appended last on connections of its own: `portico/connlog/report` is the phone's
    alone, `get` and `clear` the administrator's. The store runs on a fixed clock so `received_ms` is stable."""
    from custom_components.portico import connlog

    store = connlog.ConnLogStore(hass, now_ms=lambda: 1761000100000)
    await store.async_load()
    hass.data[connlog.DATA_KEY] = store
    fresh = _Recorder({"phone": await hass_ws_client(hass, access_token=tokens["phone"]),
                       "screen": await hass_ws_client(hass, access_token=tokens["screen"]),
                       "user": await hass_ws_client(hass, access_token=tokens["user"]),
                       "admin": await hass_ws_client(hass)})
    report = {"type": "portico/connlog/report", "reports": [CONNLOG_REPORT]}
    for who, label in (("user", "ordinary user"), ("screen", "declared screen"), ("admin", "administrator (no phone)")):
        await fresh.case(f"connlog report: {label}", who, report)
    await fresh.case("connlog report: phone, one report", "phone", report)
    second = {**CONNLOG_REPORT, "id": "rep-0002", "dur": 800, "trigger": {"kind": "reconnect"}, "result": "connected"}
    await fresh.case("connlog report: phone, a duplicate, a new one and a v2 report (rejected)", "phone",
                     {"type": "portico/connlog/report", "reports": [CONNLOG_REPORT, second, {"v": 2, "id": "rep-0003"}]})
    await fresh.case("connlog report: phone, no reports field (invalid_format)", "phone", {"type": "portico/connlog/report"})
    # DEV-807: a panel's heartbeat. The recorded phone is not a panel, so it is refused `not_a_panel`; the panel's own answers are in
    # test_panel_house.py (a phone made a panel here would change every later case).
    beat = {"type": "portico/wall/heartbeat", "app_version": "0.6.0-alpha", "panel_mode": True}
    for who, label in (("user", "ordinary user"), ("screen", "declared screen"), ("admin", "administrator (no phone)")):
        await fresh.case(f"wall heartbeat: {label}", who, beat)
    await fresh.case("wall heartbeat: phone, not a panel (not_a_panel)", "phone", beat)
    await fresh.case("wall heartbeat: phone, a field out of range (invalid_format)", "phone", {**beat, "front_reason": "sideways"})
    # DEV-807 follow-up: a panel app's answer to `portico.call_everyone`. The recorded phone is not a panel (`not_a_panel`); the answers a
    # panel's own login gets are in test_panel_call.py.
    answer = {"type": "portico/panel/call_result", "request_id": "0123456789abcdef", "ok": True}
    for who, label in (("user", "ordinary user"), ("screen", "declared screen"), ("admin", "administrator (no phone)")):
        await fresh.case(f"panel call result: {label}", who, answer)
    await fresh.case("panel call result: phone, not a panel (not_a_panel)", "phone", answer)
    await fresh.case("panel call result: phone, a bad request id (invalid_format)", "phone", {**answer, "request_id": "x"})
    for who, label in (("phone", "enrolled phone"), ("screen", "declared screen"), ("user", "ordinary user")):
        await fresh.case(f"connlog get: {label}", who, {"type": "portico/connlog/get"})
        await fresh.case(f"connlog clear: {label}", who, {"type": "portico/connlog/clear"})
    await fresh.case("connlog get: admin", "admin", {"type": "portico/connlog/get"})
    await fresh.case("connlog get: admin, after seq 1, limit 1", "admin",
                     {"type": "portico/connlog/get", "since_seq": 1, "limit": 1})
    await fresh.case("connlog get: admin, limit too big (invalid_format)", "admin",
                     {"type": "portico/connlog/get", "limit": 501})
    await fresh.case("connlog clear: admin", "admin", {"type": "portico/connlog/clear"})
    await fresh.case("connlog get: admin, after clear", "admin", {"type": "portico/connlog/get"})
    rec.cases.extend(fresh.cases)


CONNLOG_JOURNAL_LINES = [
    "live start cam=gate_i…(4) size=big stream=main ttff=840ms",
    "live fail cam=gate_i…(4) size=big why=connect retry=1 next=yes",
    "audio start answer call=gso-18…(31) up=pcmu/8000/20ms down=pcmu/8000/20ms sent=100 updrop=0 recv=99 downdrop=0 mic=low",
]


def _journal_item(item_id: str, lines: list, t0: int = 1761000000000) -> dict:
    return {"kind": "journal", "id": item_id, "app": "0.6.16-alpha",
            "lines": [{"t": t0 + i * 1000, "line": text} for i, text in enumerate(lines)]}


async def _record_connlog_journal(hass, hass_ws_client, tokens, rec) -> None:
    """The app's `live` and `audio` Journal lines on the connection log's own command (DEV-713 / DEV-705), appended last, on
    connections of its own and on a store of its own whose clock the recording moves by hand. The earlier connlog cases
    (no `kind`: a connection report) are untouched and stay in front."""
    from custom_components.portico import connlog

    clock = [1761000300000]
    store = connlog.ConnLogStore(hass, now_ms=lambda: clock[0])
    await store.async_load()
    hass.data[connlog.DATA_KEY] = store
    fresh = _Recorder({"phone": await hass_ws_client(hass, access_token=tokens["phone"]),
                       "screen": await hass_ws_client(hass, access_token=tokens["screen"]),
                       "user": await hass_ws_client(hass, access_token=tokens["user"]),
                       "admin": await hass_ws_client(hass)})
    send = lambda *reports: {"type": "portico/connlog/report", "reports": list(reports)}
    good = _journal_item("jrn-0001", CONNLOG_JOURNAL_LINES)
    for who, label in (("user", "ordinary user"), ("screen", "declared screen"), ("admin", "administrator (no phone)")):
        await fresh.case(f"connlog journal report: {label}", who, send(good))
    await fresh.case("connlog journal report: phone, three lines", "phone", send(good))
    await fresh.case("connlog journal report: phone, the same item again (a duplicate)", "phone", send(good))
    hostile = _journal_item("jrn-0002", [
        "live start cam=door_i…(4) size=small stream=sub ttff=410ms", "bogus applied call=gso-18…(31)",
        "live start url=https://ha.example.net/x", "audio y token=abc", "live a\nlive b", "live start café",
        "audio end answer call=gso-18…(31) up=? down=? sent=? updrop=? recv=? downdrop=? mic=? why=remote_hangup",
    ], t0=1761000100000)
    clock[0] += 60000
    await fresh.case("connlog journal report: phone, hostile lines dropped and counted", "phone", send(hostile))
    await fresh.case("connlog journal report: phone, nothing acceptable in it (rejected)", "phone",
                     send(_journal_item("jrn-0003", ["bogus x", "other y"]), {"kind": "journal", "id": "jrn-0004"}))
    await fresh.case("connlog journal report: phone, a connection report and an item in one message", "phone",
                     send({**CONNLOG_REPORT, "id": "rep-0101"}, _journal_item("jrn-0005", ["live thermal paused status=severe"])))
    await fresh.case("connlog journal report: phone, a `kind` of connection is a connection report", "phone",
                     send({**CONNLOG_REPORT, "id": "rep-0102", "kind": "connection"}))
    for who, label in (("phone", "enrolled phone"), ("screen", "declared screen"), ("user", "ordinary user")):
        await fresh.case(f"connlog journal get: {label}", who, {"type": "portico/connlog/get", "kind": "journal"})
    await fresh.case("connlog journal get: admin, the default reply carries the journal entries apart", "admin", {"type": "portico/connlog/get"})
    await fresh.case("connlog journal get: admin, kind journal", "admin", {"type": "portico/connlog/get", "kind": "journal"})
    await fresh.case("connlog journal get: admin, kind connection (no journal)", "admin", {"type": "portico/connlog/get", "kind": "connection"})
    await fresh.case("connlog journal get: admin, journal after seq 1, limit 1", "admin",
                     {"type": "portico/connlog/get", "kind": "journal", "journal_since_seq": 1, "limit": 1})
    await fresh.case("connlog journal get: admin, since_ms keeps only the later entries", "admin",
                     {"type": "portico/connlog/get", "kind": "journal", "since_ms": 1761000360000})
    await fresh.case("connlog journal get: admin, by phone name", "admin",
                     {"type": "portico/connlog/get", "phone": "Alice's Pixel", "kind": "journal", "limit": 1})
    await fresh.case("connlog journal get: admin, by an unknown phone name", "admin",
                     {"type": "portico/connlog/get", "phone": "Nobody's phone", "kind": "journal"})
    await fresh.case("connlog journal get: admin, kind not known (invalid_format)", "admin", {"type": "portico/connlog/get", "kind": "other"})
    await fresh.case("connlog journal clear: admin", "admin", {"type": "portico/connlog/clear"})
    await fresh.case("connlog journal get: admin, after clear", "admin", {"type": "portico/connlog/get", "kind": "journal"})
    # DEV-762 (H1): ring-decision Journal lines, and the crash / exit records in the `diag` ring
    clock[0] += 60000
    await fresh.case("connlog diag report: phone, ring-decision Journal lines are accepted", "phone",
                     send(_journal_item("jrn-0010", [
                         "push ring door=01J9EN…(26) call=gso-17…(33) seq=1 t=08:48:23.830 age=160ms known=1 fg=bg fsi=yes chan=ok/4 notif=on dnd=off -> Ring",
                         "ui Ring door=01J9EN…(26) call=gso-17…(33) post=ring+fsi notif=ok ringchan=ok fsi=yes",
                         "hold switched door=01J9EN…(26) call=gso-17…(33) ms=30000", "house ring call=hso-17…(10)",
                         "push ring url=https://ha.example.net/x"])))
    crash = {"kind": "crash", "id": "crash-0001", "app": "0.6.19-alpha", "sdk": 35, "t": 1761000400000, "up": 8123, "model": "Pixel 9",
             "rel": "16", "thread": "main", "chain": ["java.lang.IllegalStateException"],
             "frames": ["com.meda.portico.call.RingScreen.onCreate:88", "com.meda.portico.App.<init>:12"]}
    exit_ = {"kind": "exit", "id": "exit-0001", "app": "0.6.19-alpha", "sdk": 35, "t": 1761000500000, "up": 99, "age": 123456,
             "model": "Pixel 9", "rel": "16", "reason": "crash_native", "importance": "foreground", "phase": "startup",
             "trace": ["panic: nil pointer dereference", "main.run(0x1234)"]}
    for who, label in (("user", "ordinary user"), ("screen", "declared screen"), ("admin", "administrator (no phone)")):
        await fresh.case(f"connlog diag report: {label}", who, send(crash))
    await fresh.case("connlog diag report: phone, a crash and an exit", "phone", send(crash, exit_))
    await fresh.case("connlog diag report: phone, the same crash again (a duplicate)", "phone", send(crash))
    await fresh.case("connlog diag report: phone, the same signature is merged, a hostile one is refused", "phone",
                     send({**crash, "id": "crash-0002", "t": 1761000600000},
                          {**crash, "id": "crash-0003", "frames": ["https://ha.example.net/x"], "chain": ["10.6.0.6"]}))
    for who, label in (("phone", "enrolled phone"), ("screen", "declared screen"), ("user", "ordinary user")):
        await fresh.case(f"connlog diag get: {label}", who, {"type": "portico/connlog/get", "kind": "diag"})
    await fresh.case("connlog diag get: admin, kind diag", "admin", {"type": "portico/connlog/get", "kind": "diag"})
    await fresh.case("connlog diag get: admin, kind crash", "admin", {"type": "portico/connlog/get", "kind": "crash"})
    await fresh.case("connlog diag get: admin, kind exit", "admin", {"type": "portico/connlog/get", "kind": "exit"})
    await fresh.case("connlog diag get: admin, diag after seq 1", "admin", {"type": "portico/connlog/get", "kind": "diag", "diag_since_seq": 1})
    await fresh.case("connlog diag get: admin, kind journal has no diag block", "admin", {"type": "portico/connlog/get", "kind": "journal", "limit": 1})
    await fresh.case("connlog diag clear: admin", "admin", {"type": "portico/connlog/clear"})
    await fresh.case("connlog diag get: admin, after clear", "admin", {"type": "portico/connlog/get", "kind": "diag"})
    rec.cases.extend(fresh.cases)


def _blank_times(node):
    """`expires_at` is the clock plus ten minutes, and the new phone's `enrolled_at` and `code_expires_in` are the clock
    too: not values to record."""
    if isinstance(node, dict):
        fresh = node.get("entry_id") == "<phone entry id>"
        return {k: (_PLACEHOLDER_TIME if (k in ("expires_at", "as_of") or (fresh and k in ("enrolled_at", "code_expires_in"))) and v
                    else _blank_times(v)) for k, v in node.items()}
    if isinstance(node, list):
        return [_blank_times(v) for v in node]
    return node


async def _record(hass, hass_storage, hass_ws_client, google) -> dict:
    phone_user, phone_token = await _phone_user(hass, name="Portico - test phone")
    plain_user, plain_token = await _phone_user(hass, name="Portico - test plain user")
    screen_user, screen_token = await _phone_user(hass, name="Portico - test kitchen screen")
    # the template's home page, and the kitchen screen declared for its user: `content/get` serves the screen from it
    hass_storage["portico.content"] = {
        "version": 1, "minor_version": 1, "key": "portico.content",
        "data": {"schema_version": 3, "template": {"pages": SCREEN_PAGES, "rules": [], "roster": ["*"]},
                 "overrides": {}, "endpoints": {},
                 "dashboards": {SCREEN: {"name": "Kitchen screen", "ha_user_id": screen_user.id, "room": "Kitchen",
                                         "cameras": []}}}}
    # The enrollment lives on a door entry; this one is disabled, so no transport starts and the only doors are
    # the fake provider's (the phone is enrolled whether or not the entry is live).
    MockConfigEntry(
        domain=DOMAIN, entry_id=GATE_ENTRY, title="Vehicle gate", disabled_by=ConfigEntryDisabler.USER,
        data={"driver": "gate_sip_observer", "gate_sip_url": GATE_ID, "interval": 1.0, "device_id": GATE_ID,
              "enrolled_phones": [{"ha_user_id": phone_user.id, "name": PHONE_NAME, "enroll_id": PHONE_ENDPOINT,
                                   "enrolled_at": "2026-10-06T09:00:00+00:00"}]},
        unique_id=f"gate_sip_observer:{GATE_ID}",
    ).add_to_hass(hass)
    assert await async_setup_component(hass, DOMAIN, {})     # the UI part, before the push ring notification (Firebase) is configured
    # The recording keeps its original order: every case before `_record_house` is recorded with no house registry (the shape
    # of a request while the registry is absent), and `_record_house` builds it. House calls are always on in production
    # (D-20261122-aafb); the registry is stopped here only so those earlier frames do not move.
    from custom_components.portico.house import async_stop_house

    async_stop_house(hass)
    await hass.async_block_till_done()

    fake = FakeProvider()
    fake.add_door(GATE_ID, name="Vehicle gate", driver="gate_sip_observer",
                  capabilities={"DirectCapable": False, "HasRelay": True}, camera_entity_id="camera.gate")
    fake.add_door(FRONT_ID, name="Front door", capabilities={"DirectCapable": True, "HasRelay": True})
    # The recording stands a FakeProvider in for the doors. `async_setup` registers the built-in door provider too (no
    # doors here); it is unregistered so `portico/version`'s `providers` list, and with it contract/ws_v3.json, is
    # byte-for-byte what the app has always replayed.
    hass.data[DOMAIN][DATA_PROVIDERS].unregister("portico_intercom")
    hass.data[DOMAIN][DATA_PROVIDERS].register(fake)
    hass.states.async_set("camera.gate", "idle", {"friendly_name": "Gate camera"})
    hass.states.async_set("camera.garage", "unavailable", {"friendly_name": "Garage camera"})
    hass.states.async_set("lock.front", "locked", {"friendly_name": "Front door lock"})
    hass.states.async_set("scene.evening", "2026-10-06T08:00:00+00:00", {"friendly_name": "Evening"})
    opened = async_mock_service(hass, "lock", "open")
    activated = async_mock_service(hass, "scene", "turn_on")
    store = get_content_store(hass)
    await store.async_ensure_loaded()

    rec = _Recorder({"phone": await hass_ws_client(hass, access_token=phone_token),
                     "screen": await hass_ws_client(hass, access_token=screen_token),
                     "user": await hass_ws_client(hass, access_token=plain_token),
                     "admin": await hass_ws_client(hass)})

    # --- before the push ring notification (Firebase) is set up -----------------------------------------------------------------
    await rec.case("push register: push not configured", "phone", _register())
    push = _push_entry(hass, hass_storage, google)
    assert await hass.config_entries.async_setup(push.entry_id)
    await hass.async_block_till_done()

    # --- portico/version (WS-02): open to every logged-in user --------------------------------------------------
    for caller in ("phone", "screen", "user", "admin"):
        await rec.case(f"version: {caller}", caller, {"type": "portico/version"})

    # --- portico/doors: the facts, no secrets, no entry id -----------------------------------------------------------
    await rec.case("doors: enrolled phone", "phone", {"type": "portico/doors"})
    await rec.case("doors: declared screen", "screen", {"type": "portico/doors"})
    await rec.case("doors: admin", "admin", {"type": "portico/doors"})
    await rec.case("doors: ordinary user", "user", {"type": "portico/doors"})
    await rec.case("doors/secrets: not a command", "phone", {"type": "portico/doors/secrets"})
    await rec.case("devices: contract 2's name is gone", "phone", {"type": "portico/devices"})

    # --- dial: administrators and enrolled phones --------------------------------------------------------------
    await rec.case("dial: ordinary user", "user", {"type": "portico/dial", "door_id": FRONT_ID})
    await rec.case("dial: declared screen", "screen", {"type": "portico/dial", "door_id": FRONT_ID})
    await rec.case("dial: admin", "admin", {"type": "portico/dial", "door_id": FRONT_ID})
    await rec.case("dial: enrolled phone, unknown door", "phone", {"type": "portico/dial", "door_id": "nope"})
    await rec.case("dial: contract 2's device_id is refused by the schema", "phone",
                   {"type": "portico/dial", "device_id": FRONT_ID})

    # --- open_relay, hangup ------------------------------------------------------------------------------------------
    await rec.case("open_relay: ordinary user", "user", {"type": "portico/open_relay", "door_id": GATE_ID})
    await rec.case("open_relay: declared screen", "screen", {"type": "portico/open_relay", "door_id": GATE_ID})
    await rec.case("open_relay: enrolled phone, unknown door", "phone", {"type": "portico/open_relay", "door_id": "nope"})
    await rec.case("hangup: ordinary user", "user", {"type": "portico/hangup", "call_id": "nope"})
    await rec.case("hangup: nothing owns the call", "phone", {"type": "portico/hangup", "call_id": "nope"})

    # --- a ring and its commands -------------------------------------------------------------------------------------
    answer = {"type": "portico/answer", "door_id": GATE_ID, "call_id": "gate-call-1", "client_kind": "app"}
    await rec.case("answer: nothing is ringing (not_available)", "phone", answer)
    fake.ring(GATE_ID, "gate-call-1")
    await hass.async_block_till_done()
    # the ordinary user may answer too (the owner's decision of 2026-10-21), so it answers its own ring, on the front
    # door: a ring on the gate that it answered would leave nothing for the "granted" case below
    fake.ring(FRONT_ID, "front-call-user")
    await hass.async_block_till_done()
    await rec.case("answer: ordinary user", "user", {**answer, "door_id": FRONT_ID, "call_id": "front-call-user"})
    fake.state("front-call-user", "ended")
    await hass.async_block_till_done()
    await rec.case("answer: unknown door", "phone", {**answer, "door_id": "nope"})
    await rec.case("answer: granted", "phone", answer)
    await rec.case("answer: the call is taken (already_answered)", "screen", answer)
    await rec.case("keepalive: the answered call", "phone", {"type": "portico/keepalive", "call_id": "gate-call-1"})
    await rec.case("keepalive: ordinary user", "user", {"type": "portico/keepalive", "call_id": "gate-call-1"})
    offer = {"type": "portico/webrtc_offer", "call_id": "gate-call-1", "door_id": GATE_ID, "sdp": "FAKE-offer"}
    await rec.case("webrtc_offer: the answered call, the door named", "phone", offer)
    await rec.case("webrtc_offer: a door that does not own the call", "phone", {**offer, "door_id": FRONT_ID})
    await rec.case("webrtc_offer: unknown door", "phone", {**offer, "door_id": "nope"})
    await rec.case("webrtc_offer: ordinary user", "user", offer)
    await rec.case("webrtc_offer: no door (contract 2's shape)", "phone",
                   {"type": "portico/webrtc_offer", "call_id": "gate-call-1", "sdp": "FAKE-offer"})
    await rec.case("webrtc_answer: the answered call", "screen",
                   {"type": "portico/webrtc_answer", "call_id": "gate-call-1", "sdp": "FAKE-answer"})
    await rec.case("webrtc_answer: ordinary user", "user",
                   {"type": "portico/webrtc_answer", "call_id": "gate-call-1", "sdp": "FAKE-answer"})
    await rec.case("webrtc_ice: the answered call", "admin",
                   {"type": "portico/webrtc_ice", "call_id": "gate-call-1", "candidate": "FAKE-candidate"})
    await rec.case("webrtc_ice: ordinary user", "user",
                   {"type": "portico/webrtc_ice", "call_id": "gate-call-1", "candidate": "FAKE-candidate"})
    await rec.case("hangup: the answered call", "phone",
                   {"type": "portico/hangup", "call_id": "gate-call-1", "reason": "hangup"})
    fake.state("gate-call-1", "ended")
    await hass.async_block_till_done()
    # a door that refuses the claim
    fake.refuse_answers = True
    fake.ring(FRONT_ID, "front-call-0")
    await hass.async_block_till_done()
    await rec.case("answer: the door refuses (refused)", "phone",
                   {**answer, "door_id": FRONT_ID, "call_id": "front-call-0"})
    fake.refuse_answers = False
    fake.state("front-call-0", "ended")
    await hass.async_block_till_done()

    # --- away media: an enrolled phone only --------------------------------------------------------------------------
    await rec.case("media_open: ordinary user", "user",
                   {"type": "portico/media_open", "door_id": GATE_ID, "call_id": "gate-call-1"})
    await rec.case("media_open: enrolled phone, away media off", "phone",
                   {"type": "portico/media_open", "door_id": GATE_ID, "call_id": "gate-call-1"})
    await rec.case("media_open: contract 2's device_id is refused by the schema", "phone",
                   {"type": "portico/media_open", "device_id": GATE_ID, "call_id": "gate-call-1"})
    await rec.case("media_frame: ordinary user", "user",
                   {"type": "portico/media_frame", "call_id": "gate-call-1", "d": "QUJD"})
    await rec.case("media_frame: enrolled phone, no channel", "phone",
                   {"type": "portico/media_frame", "call_id": "gate-call-1", "d": "QUJD"})
    await rec.case("media_close: ordinary user", "user", {"type": "portico/media_close", "call_id": "gate-call-1"})
    await rec.case("media_close: enrolled phone", "phone", {"type": "portico/media_close", "call_id": "gate-call-1"})

    # --- the push ring notification registry (push registration is refused below app 0.5.0) -------------------------------------
    await rec.case("push register: app 0.4.3 is too old", "phone", _register(app_version="0.4.3-alpha"))
    await rec.case("push register: no app version is too old", "phone",
                   {k: v for k, v in _register().items() if k != "app_version"})
    await rec.case("push register: not an enrolled phone", "user", _register())
    first = await rec.case("push register: ok", "phone", _register())
    # The admin turns the phone's push ring notification on (the push options flow, not a WebSocket command); a rotation keeps
    # the row and that switch.
    await _manager(hass).registry.async_set_enabled(first[0]["result"]["reg_id"], True)
    await rec.case("push register: rotation (same installation_id, new registration)", "phone",
                   _register(kind="token", registration=REGISTRATION_ROTATED))
    await rec.case("push register: blank registration", "phone", _register(registration="   "))
    await rec.case("push register: placeholder registration", "phone", _register(registration="unknown"))
    await rec.case("push register: another Firebase project", "phone", _register(fcm_project_id="another-project"))
    await rec.case("push register: another package", "phone", _register(package="com.meda.ha_intercom"))
    await rec.case("push register: invalid_format (unknown kind)", "phone", _register(kind="topic"))
    await rec.case("push register: invalid_format (empty installation_id)", "phone", _register(installation_id=""))
    missing = _register()
    del missing["fcm_project_id"]
    await rec.case("push register: invalid_format (missing field)", "phone", missing)
    await rec.case("push status: the caller's own rows", "phone", {"type": "portico/push/status"})
    await rec.case("push status: ordinary user", "user", {"type": "portico/push/status"})
    await rec.case("push unregister: ok", "phone", {"type": "portico/push/unregister", "installation_id": INSTALLATION})
    await rec.case("push unregister: not_found", "phone",
                   {"type": "portico/push/unregister", "installation_id": INSTALLATION})
    await rec.case("push unregister: ordinary user", "user",
                   {"type": "portico/push/unregister", "installation_id": INSTALLATION})
    await rec.case("push status: no rows", "phone", {"type": "portico/push/status"})

    # --- content: the endpoint's document, nothing flat ---------------------------------------------------------------
    await rec.case("content get: enrolled phone, no profile yet", "phone", {"type": "portico/content/get"})
    await rec.case("content get: ordinary user", "user", {"type": "portico/content/get"})
    await store.async_set(phone_user.id, CONTENT_PROFILE)
    await rec.case("content get: enrolled phone, a profile", "phone", {"type": "portico/content/get"})
    await rec.case("content get: a declared screen", "screen", {"type": "portico/content/get"})
    await rec.case("content get: a declared screen names its own endpoint", "screen",
                   {"type": "portico/content/get", "endpoint": SCREEN})
    await rec.case("content get: a declared screen names another's endpoint", "screen",
                   {"type": "portico/content/get", "endpoint": PHONE_ENDPOINT})
    await rec.case("content get: a phone names another's endpoint", "phone",
                   {"type": "portico/content/get", "endpoint": SCREEN})
    await rec.case("content get: an admin names the phone's endpoint", "admin",
                   {"type": "portico/content/get", "endpoint": PHONE_ENDPOINT})
    await rec.case("content get: an admin names an unknown endpoint", "admin",
                   {"type": "portico/content/get", "endpoint": "nope"})
    await rec.case("content get: an admin names none", "admin", {"type": "portico/content/get"})
    await rec.case("content get: contract 2's profile key is refused by the schema", "admin",
                   {"type": "portico/content/get", "profile": PHONE_NAME})
    await rec.case("content get: contract 2's pages flag is refused by the schema", "phone",
                   {"type": "portico/content/get", "pages": True})
    await rec.case("content run: ok", "phone", {"type": "portico/content/run", "action_id": "open-front"})
    await hass.async_block_till_done()
    assert len(opened) == 1 and opened[0].context.user_id == phone_user.id
    await rec.case("content run: the same action again at once", "phone",
                   {"type": "portico/content/run", "action_id": "open-front"})
    await rec.case("content run: not in the profile", "phone", {"type": "portico/content/run", "action_id": "lock.unlock"})
    await rec.case("content run: a declared screen", "screen", {"type": "portico/content/run", "action_id": "evening"})
    await hass.async_block_till_done()
    assert len(activated) == 1 and activated[0].context.user_id == screen_user.id
    await rec.case("content run: ordinary user", "user", {"type": "portico/content/run", "action_id": "evening"})
    await rec.case("content snapshot: ok", "phone", {"type": "portico/content/snapshot", "camera_id": "gate"})
    await rec.case("content snapshot: a camera that is unavailable", "phone",
                   {"type": "portico/content/snapshot", "camera_id": "garage"})
    await rec.case("content snapshot: not in the profile", "phone", {"type": "portico/content/snapshot", "camera_id": "front"})
    await rec.case("content snapshot: width out of range (invalid_format)", "phone",
                   {"type": "portico/content/snapshot", "camera_id": "gate", "width": 10})
    await rec.case("content snapshot: ordinary user", "user", {"type": "portico/content/snapshot", "camera_id": "gate"})
    await rec.case("content list: contract 2's command is gone", "admin", {"type": "portico/content/list"})

    # --- the editor (M6 E2): refusals, the draft preview, a stale save, the kiosk hello ------------------------------
    await rec.case("content document: ordinary user", "user", {"type": "portico/content/document"})
    await rec.case("content document: declared screen", "screen", {"type": "portico/content/document"})
    await rec.case("content document: admin", "admin", {"type": "portico/content/document"})
    draft = {"template": {"pages": SCREEN_PAGES, "rules": [], "roster": ["*"]}, "overrides": {}, "dashboards": {},
             "endpoints": {}}
    await rec.case("content preview: ordinary user", "user",
                   {"type": "portico/content/preview", "document": draft, "endpoint": PHONE_ENDPOINT})
    await rec.case("content preview: admin", "admin",
                   {"type": "portico/content/preview", "document": draft, "endpoint": PHONE_ENDPOINT})
    await rec.case("content save: ordinary user", "user",
                   {"type": "portico/content/save", "document": draft, "base_doc_rev": "stale"})
    await rec.case("content save: admin, a stale revision", "admin",
                   {"type": "portico/content/save", "document": draft, "base_doc_rev": "stale"})
    await rec.case("kiosk hello: a declared screen", "screen", {"type": "portico/kiosk/hello", "endpoint": SCREEN})
    await rec.case("kiosk hello: ordinary user", "user", {"type": "portico/kiosk/hello"})

    # --- the two subscriptions, last, so no earlier frame's message id moves ------------------------------------
    calls = await rec.case("calls subscribe: result, then one snapshot per door", "phone",
                           {"type": "portico/calls/subscribe"}, frames=3)
    assert len(calls) == 3
    calls_case = rec.cases[-1]
    fake.ring(FRONT_ID, "front-call-1")
    await hass.async_block_till_done()
    await rec.more("phone", 1, calls_case)
    fake.state("front-call-1", "ended")
    await hass.async_block_till_done()
    await rec.more("phone", 1, calls_case)       # `ended` with the call id and `prev_state`; no `idle` echo
    # an ordinary user may subscribe (the owner's decision of 2026-10-21): the result and one snapshot per door, read
    # here so the snapshots are not left for the next case on this connection to receive
    await rec.case("calls subscribe: ordinary user", "user", {"type": "portico/calls/subscribe"}, frames=3)

    phone_sub = await rec.case("content subscribe: enrolled phone", "phone", {"type": "portico/content/subscribe"})
    assert phone_sub[0]["success"]
    phone_case = rec.cases[-1]
    await rec.case("content subscribe: declared screen", "screen", {"type": "portico/content/subscribe"})
    screen_case = rec.cases[-1]
    await rec.case("content subscribe: admin names none (every change)", "admin", {"type": "portico/content/subscribe"})
    admin_case = rec.cases[-1]
    await rec.case("content subscribe: a phone names another's endpoint", "phone",
                   {"type": "portico/content/subscribe", "endpoint": SCREEN})
    await rec.case("content subscribe: admin names an unknown endpoint", "admin",
                   {"type": "portico/content/subscribe", "endpoint": "nope"})
    await rec.case("content subscribe: ordinary user", "user", {"type": "portico/content/subscribe"})
    # the phone's own endpoint changes: the phone hears it with the rev, an admin with `rev: null`, the screen not at all
    await store.async_set(phone_user.id, {**CONTENT_PROFILE, "actions": CONTENT_PROFILE["actions"][:1]})
    await hass.async_block_till_done()
    await rec.more("phone", 1, phone_case)
    await rec.more("admin", 1, admin_case)
    # the install-wide home name: each hears its own endpoint and rev, the admin `endpoint: null, rev: null`
    await store.async_set_home_name("My Home")
    await hass.async_block_till_done()
    await rec.more("phone", 1, phone_case)
    await rec.more("screen", 1, screen_case)
    await rec.more("admin", 1, admin_case)

    # --- blocks (DEV-658 B2): appended after every earlier case, on fresh connections, so no earlier frame or message
    # id moved. The earlier cases differ from the pre-blocks recording only by the `blocks` added to each page (and,
    # in `content/document`, schema_version 4 and `blocks`); the PR body says how that was checked.
    await _record_blocks(hass, hass_ws_client, store, phone_token, rec)

    # --- the picker's spec chips: appended last, so no earlier frame moved ---------------------------------------------
    await _record_cameras_info(hass, hass_ws_client, {"phone": phone_token, "screen": screen_token, "user": plain_token}, rec)

    # --- talk to the gate (M7 P3-B): recorded before the house cases (house calls are off here), appended after them ----------
    talk_cases = await _record_talk(hass, hass_ws_client, {"phone": phone_token, "screen": screen_token}, fake, store)

    # --- house calls (M7 3a): appended last, off and then on ------------------------------------------------------------
    await _record_house(hass, hass_ws_client, {"phone": phone_token, "screen": screen_token, "user": plain_token}, rec,
                        phone_user.id)
    rec.cases.extend(talk_cases)

    # --- the Phones tab (UI split P2): appended last, on connections of its own -------------------------------------------
    await _record_phones(hass, hass_ws_client, {"phone": phone_token, "screen": screen_token, "user": plain_token}, rec, push)

    # --- the House calls switch (UI split P6b): appended after the Phones tab, on connections of its own ------------------------

    # --- the connection log (DEV-705): appended after the House calls switch, on connections of its own -------------------------
    await _record_connlog(hass, hass_ws_client, {"phone": phone_token, "screen": screen_token, "user": plain_token}, rec)

    # --- the Settings tab (UI split P4 to P6): appended after the connection log, on connections of its own ------------------------
    await _record_settings_tab(hass, hass_ws_client, {"phone": phone_token, "screen": screen_token, "user": plain_token}, rec)

    # --- the claims switch (UI split P9): appended last, on connections of its own ------------------------------------------------

    # --- the House calls switch reaching open streams: appended last, on connections of its own ----------------------------------

    # --- live video of a camera tile (live.py): appended last, on connections of its own -------------------------------------------
    await _record_live(hass, hass_ws_client, {"phone": phone_token, "screen": screen_token, "user": plain_token}, rec,
                       screen_user.id)

    # --- show_fps: appended after the live video cases ----------------------------------------------------------------------------
    await _record_show_fps(hass, hass_ws_client, rec)

    # --- live_rtsp (DEV-713 S1b-1): appended after show_fps, on a connection of its own -------------------------------------------
    await _record_live_rtsp(hass, hass_ws_client, rec)

    # --- live_rtsp on the caller's cameras (DEV-713 S2-1): appended last, on a connection of its own -------------------------------
    await _record_caller_live_rtsp(hass, hass_ws_client, rec)

    # --- the Journal lines on the connection log (DEV-713 / DEV-705): appended last, on connections of its own -----------------------
    await _record_connlog_journal(hass, hass_ws_client, {"phone": phone_token, "screen": screen_token, "user": plain_token}, rec)

    # --- the Elements page (DEV-760 E2): appended last, on connections of its own ----------------------------------------------
    await _record_elements(hass, hass_ws_client, {"phone": phone_token, "screen": screen_token, "user": plain_token}, rec)
    await _record_pages_migrate(hass, hass_ws_client, {"phone": phone_token, "screen": screen_token, "user": plain_token}, rec)

    cases =await _scrub_user_ids(hass, rec.cases)
    for case in cases:
        case["auth"] = sorted(auth.COMMANDS.get(case["send"]["type"], ()))
    return {"_meta": {**META, "auth": {c: sorted(r) for c, r in sorted(auth.COMMANDS.items())}}, "cases": cases}


async def test_ws_v3_contract_matches_the_handlers(hass, hass_storage, hass_ws_client, google,
                                                   deterministic, content_fakes):
    recorded = await _record(hass, hass_storage, hass_ws_client, google)
    text = json.dumps(recorded, indent=2, ensure_ascii=False) + "\n"
    for secret in ("access_token", "private_key"):
        assert secret not in text

    if os.environ.get(REGENERATE_ENV) == "1":
        CONTRACT.write_text(text, encoding="utf-8")
    assert CONTRACT.exists(), f"{CONTRACT} is missing; run with {REGENERATE_ENV}=1"
    checked_in = json.loads(CONTRACT.read_text(encoding="utf-8"))
    assert checked_in == recorded, (
        f"{CONTRACT.name} differs from what the handlers emit. If the change is intended, "
        f"regenerate it with {REGENERATE_ENV}=1, review the diff, and tell portico-app."
    )


def _data() -> dict:
    return json.loads(CONTRACT.read_text(encoding="utf-8"))


def _app_commands() -> list[str]:
    """The file is bare: one command a line, sorted, no comments (the app reads it line by line)."""
    return APP_COMMANDS.read_text(encoding="utf-8").splitlines()


def test_the_fixture_states_the_authorization_table():
    """WS-10: the table in the file is the table in auth.py, and every case names its command's row."""
    data = _data()
    assert data["_meta"]["auth"] == {c: sorted(r) for c, r in sorted(auth.COMMANDS.items())}
    assert data["_meta"]["version"] == 3
    for case in data["cases"]:
        assert case["auth"] == sorted(auth.COMMANDS.get(case["send"]["type"], ())), case["name"]


def test_every_case_agrees_with_the_table():
    """A caller outside the command's classes is refused; a caller inside it never is."""
    for case in _data()["cases"]:
        row = set(case["auth"])
        if not row:                                       # a command that is not served (contract 2's name)
            assert case["receive"][0]["error"]["code"] == "unknown_command", case["name"]
            continue
        refused = any(
            f.get("success") is False and f["error"]["code"] in REFUSAL_CODES for f in case["receive"]
        )
        in_row = bool(row & CALLER_CLASSES[case["as"]])
        if case["name"] in HANDLER_REFUSALS:
            assert in_row and refused, case["name"]
            continue
        assert refused == (not in_row), case["name"]


def test_every_command_in_the_table_has_a_case_for_each_side_of_its_row():
    """The fixture is the acceptance test of the auth table: for every command, a case from a caller inside the row
    (unless the command is the app-only media pair, covered by ws_media_v2.json) and, when some class is outside it, one
    from a caller outside."""
    seen_in: dict[str, set[str]] = {}
    for case in _data()["cases"]:
        seen_in.setdefault(case["send"]["type"], set()).add(case["as"])
    for command, row in auth.COMMANDS.items():
        callers = seen_in.get(command, set())
        assert callers, f"no case for {command}"
        assert any(set(row) & CALLER_CLASSES[c] for c in callers), f"{command}: no case from inside its row"
        if auth.ANY not in row:
            assert any(not (set(row) & CALLER_CLASSES[c]) for c in callers), f"{command}: no case from outside its row"


def test_version_says_contract_3_to_every_caller():
    cases = [c for c in _data()["cases"] if c["send"]["type"] == "portico/version"]
    assert {c["as"] for c in cases} == {"phone", "screen", "user", "admin"}
    for case in cases:
        assert case["receive"][0]["result"] == {
            "contract": 3, "ui_version": 1, "providers": [{"domain": "fake", "api_version": 1}]}, case["name"]


def test_fixture_covers_every_command_in_the_app_list():
    """The app sends exactly the commands in contract/app_commands_v3.txt (its own test checks its code against the same
    file); every one has a case here, or for the media pair in ws_media_v2.json."""
    recorded = {case["send"]["type"] for case in _data()["cases"]}
    media = {step["send"]["type"] for case in json.loads(MEDIA_CONTRACT.read_text(encoding="utf-8"))["cases"]
             for step in case["steps"] if "send" in step}
    commands = _app_commands()
    assert commands and commands == sorted(set(commands)), "app_commands_v3.txt: one command a line, sorted, no repeats"
    assert all(re.fullmatch(r"portico/[a-z_/]+", c) for c in commands), "app_commands_v3.txt: commands only, no comments"
    for command in commands:
        assert command in recorded or command in media, command
        assert command in auth.COMMANDS, command


async def test_every_command_in_the_app_list_is_served(hass):
    from homeassistant.components import websocket_api

    from .test_signaling import _setup_signaling

    await _setup_signaling(hass)
    registered = {c for c in hass.data[websocket_api.DOMAIN] if c.startswith("portico/")}
    assert set(_app_commands()) <= registered


def test_the_app_list_names_no_contract_2_command():
    for gone in ("portico/devices", "portico/subscribe", "portico/content/list", "portico/doors/secrets",
                 "portico/house/subscribe", "portico/house/call", "portico/house/roster", "portico/house/keepalive"):
        assert gone not in _app_commands() and gone not in auth.COMMANDS


def _assert_no_flat_pages(node, name, path="result"):
    """B8: no page anywhere in a recorded reply (`content/get`, `content/document`, `content/preview`, the resolved
    documents inside `content/document`) carries `cameras` or `actions` beside its `blocks`."""
    if isinstance(node, dict):
        if "blocks" in node:
            assert "cameras" not in node and "actions" not in node, (name, path)
        for key, value in node.items():
            _assert_no_flat_pages(value, name, f"{path}.{key}")
    elif isinstance(node, list):
        for n, value in enumerate(node):
            _assert_no_flat_pages(value, name, f"{path}[{n}]")


def test_the_recorded_replies_have_pages_of_blocks_for_the_flat_check_to_look_at():
    """The check above is not vacuous: the file holds `content/get`, `content/document` and `content/preview` replies
    whose pages have `blocks`."""
    seen = set()

    def walk(node, kind):
        if isinstance(node, dict):
            if "blocks" in node and "label" in node:
                seen.add(kind)
            for value in node.values():
                walk(value, kind)
        elif isinstance(node, list):
            for value in node:
                walk(value, kind)

    for case in _data()["cases"]:
        for frame in case["receive"]:
            if frame.get("success"):
                walk(frame.get("result"), case["send"]["type"])
    assert {"portico/content/get", "portico/content/document", "portico/content/preview"} <= seen


def test_the_recorded_frames_carry_no_contract_2_keys():
    """No alias leaked into a reply: no `entry_id`, no `device_id` in a reply, no secrets, no flat content fields."""
    for case in _data()["cases"]:
        for frame in case["receive"]:
            blob = json.dumps(frame.get("result") if "result" in frame else frame.get("event"))
            for key in ('"entry_id"', '"secrets"', '"devices"', '"sip_password"'):
                # contract 2 leaked a DOOR's config entry id. The Phones tab (UI split P2) keys a PHONE by its entry id
                # on purpose; what it must never carry is the `enroll_id`, checked below.
                if key == '"entry_id"' and case["send"]["type"].startswith("portico/phones/"):
                    continue
                assert key not in blob, (case["name"], key)
            if case["send"]["type"].startswith("portico/phones/"):
                assert "enroll_id" not in blob and "ep-alice" not in blob, case["name"]
            if case["send"]["type"] == "portico/content/get" and frame.get("success"):
                for key in ("phone", "cameras", "actions"):
                    assert key not in frame["result"], (case["name"], key)
            _assert_no_flat_pages(frame.get("result"), case["name"])
            if frame.get("type") == "event" and frame["event"].get("type") == "call_state_changed":
                assert "device_id" not in frame["event"] and "door_id" in frame["event"], case["name"]


def test_ws_contract_covers_every_push_error_code():
    """Every error code the push handlers can send is in the file, `app_too_old` (contract 3) included."""
    codes = {f["error"]["code"] for c in _data()["cases"] for f in c["receive"] if f.get("success") is False}
    assert codes >= {"push_not_configured", "unauthorized", "invalid_registration", "project_mismatch",
                     "package_mismatch", "invalid_format", "not_found", "app_too_old"}


def test_ws_contract_covers_the_answer_and_door_error_codes():
    """contract 3's door codes: `unknown_door`, and the answer's `already_answered` (kept), `refused`, `not_available`."""
    codes = {f["error"]["code"] for c in _data()["cases"] for f in c["receive"] if f.get("success") is False}
    assert codes >= {"unknown_door", "already_answered", "refused", "not_available", "unknown_call",
                     "unknown_endpoint", "unknown_command", "conflict", "away_media_disabled"}
    assert "unknown_device" not in codes and "claims_disabled" not in codes
