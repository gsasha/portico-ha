"""The `live_rtsp` stream hint of a live camera tile (DEV-713 S1b-1, live_rtsp.py): what the phone app needs to pull a camera's
go2rtc stream over RTSP when it is away from home.

What is pinned: the address option is strict (`rtsp://host[:port]`, no credentials, path, query or IPv6); camera names use a
strict charset; the hint is computed per request from the camera entity's Frigate name and the settings entry's two options and
is NEVER stored; it is emitted only with an address set, only on a `live: true` tile of a Frigate camera, `sub` only for a
camera listed, never with an entity id in it; only the cameras the endpoint's own resolved document holds carry it; the
endpoint's `rev` changes when the options change and not when a camera blinks; a document or registry without any of it
resolves exactly as before.

DEV-713 S2-1: the CALLER's cameras (`callers.<id>.cameras[]`, the ring and in-call screens) carry it on the same terms with no
`live: true` (they have no such flag), only the ones the endpoint is shown, and `content/live` grants exactly the `c-` ids listed
(test_live.py).
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from .. import live_rtsp
from ..content import get_content_store, normalize_block
from ..content_resolve import caller_camera_entities, resolve_block, resolve_caller, resolve_endpoint, short_id
from . import test_content_rules as rules
from .test_content_blocks import _blk, _cam, _page
from .test_content_wire_blocks import _doc_for_rev, _Registry, _States
from .test_editor_commands import _send
from .test_enrollment_flow import _push_entry

pytestmark = pytest.mark.asyncio

BASE = "rtsp://10.6.0.6:8554"
FRIGATE = {"camera_name": "gate_intercom", "client_id": "frigate", "friendly_name": "Gate"}


# ---- the option's validation, pure ----------------------------------------------------------------------------------------

@pytest.mark.parametrize("raw,canonical", [
    ("rtsp://10.6.0.6:8554", BASE), ("rtsp://10.6.0.6:8554/", BASE), ("  rtsp://10.6.0.6:8554  ", BASE),
    ("RTSP://10.6.0.6:8554", BASE), ("rtsp://10.6.0.6", "rtsp://10.6.0.6"), ("rtsp://go2rtc.lan:554", "rtsp://go2rtc.lan:554"),
    ("rtsp://Go2RTC", "rtsp://go2rtc"), ("rtsp://10.6.0.6:08554", BASE), ("", ""), ("   ", ""),
])
async def test_a_good_address_is_kept_in_one_canonical_form(raw, canonical):
    assert live_rtsp.normalize_base(raw) == canonical


@pytest.mark.parametrize("bad", [
    "rtsp://user:pw@10.6.0.6:8554", "rtsp://user@10.6.0.6", "rtsp://:pw@10.6.0.6", "rtsp://10.6.0.6:8554@evil.example",
    "rtsp://10.6.0.6:8554/gate", "rtsp://10.6.0.6:8554/?x=1", "rtsp://10.6.0.6:8554#f", "rtsp://10.6.0.6:8554;a=b",
    "http://10.6.0.6:8554", "rtsps://10.6.0.6:322", "rtspx://10.6.0.6", "10.6.0.6:8554", "//10.6.0.6:8554", "rtsp://",
    "rtsp://[::1]:8554", "rtsp://10.6.0.6:", "rtsp://10.6.0.6:0", "rtsp://10.6.0.6:65536", "rtsp://10.6.0.6:-1",
    "rtsp://10.6.0.256", "rtsp://10.6.0", "rtsp://10.6.0.6.7", "rtsp://-bad.example", "rtsp://bad-.example", "rtsp://a..b",
    "rtsp://exa mple", "rtsp://10.6.0.6\n:8554", "rtsp://10.6.0.6:8554\x00", "rtsp://café.example", "rtsp://" + "a" * 130,
    "rtsp://" + ".".join(["a" * 63] * 5), None, 5, ["rtsp://10.6.0.6"], {"a": 1}, True,
])
async def test_an_address_that_is_not_rtsp_host_and_port_alone_is_refused(bad):
    assert live_rtsp.normalize_base(bad) is None


async def test_the_longest_accepted_address_is_128_characters():
    host = "a" * 61 + "." + "b" * 56                     # 7 + 118 = 125
    assert len("rtsp://" + host) <= live_rtsp.MAX_BASE_LENGTH
    assert live_rtsp.normalize_base("rtsp://" + host) == "rtsp://" + host
    assert live_rtsp.normalize_base("rtsp://" + host + "." + "c" * 3) is None        # 129 characters


@pytest.mark.parametrize("name", ["gate_intercom", "a", "Facade-Lower", "x" * 64, "0"])
async def test_a_stream_name_uses_letters_digits_underscore_and_hyphen(name):
    assert live_rtsp.valid_stream_name(name)


@pytest.mark.parametrize("name", ["", "x" * 65, "gate intercom", "gate/intercom", "gate.intercom", "gate?x", "../gate", "gate\n",
                                  "gäte", None, 3, ["a"]])
async def test_anything_else_is_not_a_stream_name(name):
    assert not live_rtsp.valid_stream_name(name)


async def test_the_sub_stream_list_is_names_only_listed_once():
    assert live_rtsp.normalize_sub_streams([]) == []
    assert live_rtsp.normalize_sub_streams(["a", "b", "a"]) == ["a", "b"]
    for bad in ("a", ["a b"], ["a", 3], [""], ["a/b"], [f"n{i}" for i in range(65)], None, {"a": 1}):
        assert live_rtsp.normalize_sub_streams(bad) is None
    assert len(live_rtsp.normalize_sub_streams([f"n{i}" for i in range(64)])) == 64


async def test_stored_options_that_do_not_validate_read_as_off():
    assert live_rtsp.read_settings(None) == ("", [])
    assert live_rtsp.read_settings({}) == ("", [])
    assert live_rtsp.read_settings({"live_rtsp_base": BASE, "live_sub_streams": ["a"]}) == (BASE, ["a"])
    assert live_rtsp.read_settings({"live_rtsp_base": "rtsp://u:p@h", "live_sub_streams": "a"}) == ("", [])
    assert live_rtsp.read_settings({"live_rtsp_base": 7}) == ("", [])


async def test_only_a_frigate_camera_has_a_name():
    assert live_rtsp.camera_name_of(FRIGATE) == "gate_intercom"
    assert live_rtsp.camera_name_of({"camera_name": "gate"}) is None, "client_id is the other half of the Frigate marker"
    assert live_rtsp.camera_name_of({"client_id": "frigate"}) is None
    assert live_rtsp.camera_name_of({"camera_name": "a/b", "client_id": "frigate"}) is None
    assert live_rtsp.camera_name_of({"camera_name": 5, "client_id": "frigate"}) is None
    assert live_rtsp.camera_name_of({"friendly_name": "Doorbell"}) is None


async def test_the_hint_is_main_and_sub_only_for_a_listed_camera():
    assert live_rtsp.hint(BASE, [], "gate_intercom") == {"main": f"{BASE}/gate_intercom"}
    assert live_rtsp.hint(BASE, ["gate_intercom"], "gate_intercom") == {
        "main": f"{BASE}/gate_intercom", "sub": f"{BASE}/gate_intercom_sub"}
    assert live_rtsp.hint(BASE, ["gate_intercom"], "doorbell") == {"main": f"{BASE}/doorbell"}
    assert live_rtsp.hint("", ["a"], "a") is None, "no address: off"
    assert live_rtsp.hint(BASE, [], None) is None and live_rtsp.hint(BASE, [], "a/b") is None


# ---- the resolver, pure ----------------------------------------------------------------------------------------------------

class _HintRegistry(_Registry):
    """A registry that offers the hint the way the store does: the entity and its state in, the object or None out."""

    def __init__(self, names):
        super().__init__()
        self.names = names

    def live_rtsp(self, entity_id, state):
        name = self.names.get(entity_id)
        return live_rtsp.hint(BASE, ["gate_intercom"], name)


def _block_cameras(registry, *cameras):
    return resolve_block(_blk(cameras=list(cameras)), states=_States(**{"camera.gate": "idle", "camera.yard": "idle"}),
                         registry=registry)["cameras"]


async def test_resolve_adds_live_rtsp_to_a_live_tile_the_registry_has_a_hint_for_and_to_no_other():
    registry = _HintRegistry({"camera.gate": "gate_intercom", "camera.yard": "yard"})
    live, plain, unknown = _block_cameras(registry, {**_cam(), "live": True}, _cam("yard", entity="camera.yard"),
                                          {**_cam("x", entity="camera.x"), "live": True})
    assert live["live_rtsp"] == {"main": f"{BASE}/gate_intercom", "sub": f"{BASE}/gate_intercom_sub"}
    assert "live_rtsp" not in plain, "a tile that is not live carries none, whatever the camera"
    assert "live_rtsp" not in unknown, "a camera the registry has no hint for carries none"


async def test_a_registry_without_the_hint_and_a_document_without_live_resolve_exactly_as_before():
    with_live = _block_cameras(_Registry(), {**_cam(), "live": True})[0]
    assert set(with_live) == {"id", "label", "entity_id", "size", "available", "name", "live"}
    plain = _block_cameras(_HintRegistry({"camera.gate": "gate_intercom"}), _cam())[0]
    assert set(plain) == {"id", "label", "entity_id", "size", "available", "name"}


async def test_the_hint_is_part_of_rev_so_changing_the_options_moves_it():
    def rev(names):
        document = _doc_for_rev(_blk(cameras=[{**_cam(), "live": True}]))
        return resolve_endpoint(document, "ep", states=_States(), registry=_HintRegistry(names))["rev"]

    assert rev({"camera.gate": "gate_intercom"}) != rev({})
    assert rev({"camera.gate": "gate_intercom"}) != rev({"camera.gate": "doorbell"})
    assert rev({"camera.gate": "gate_intercom"}) == rev({"camera.gate": "gate_intercom"})


# ---- it is never stored ---------------------------------------------------------------------------------------------------

async def test_a_document_that_carries_a_live_rtsp_loses_it_on_validation():
    sent = {**_cam(), "live": True, "live_rtsp": {"main": "rtsp://evil/x"}}
    (kept,) = normalize_block(_blk(cameras=[sent]), set(), set())["cameras"]
    assert kept == {**_cam(), "live": True}


# ---- through the real store and the real websocket -----------------------------------------------------------------------

def _states(hass, **extra):
    hass.states.async_set("camera.gate", "idle", dict(FRIGATE))                           # a Frigate camera
    hass.states.async_set("camera.yard", "idle", {"camera_name": "yard", "client_id": "frigate"})
    hass.states.async_set("camera.tablet", "idle", {"friendly_name": "Tablet"})            # not Frigate
    hass.states.async_set("camera.half", "idle", {"camera_name": "half"})                  # a camera_name with no client_id
    hass.states.async_set("camera.odd", "idle", {"camera_name": "od/d", "client_id": "frigate"})
    hass.states.async_set("camera.secret", "idle", {"camera_name": "secret_cam", "client_id": "frigate"})
    for entity, attrs in extra.items():
        hass.states.async_set(entity.replace("__", "."), "idle", attrs)


def _template():
    return _page([_blk(cameras=[
        {**_cam(), "live": True},                                                    # Frigate, live
        {**_cam("yard", entity="camera.yard"), "live": True},                        # Frigate, live, not in the sub list
        _cam("still", entity="camera.gate"),                                         # Frigate, not live
        {**_cam("tablet", entity="camera.tablet"), "live": True},                    # live but not Frigate
        {**_cam("half", entity="camera.half"), "live": True},
        {**_cam("odd", entity="camera.odd"), "live": True}])], pid="home")


@pytest.fixture
async def phone(hass, hass_ws_client):
    from .test_endpoints import _phone_entry, _provide
    from .test_push_registry import _phone_user
    from .test_signaling import _setup_signaling

    user, token = await _phone_user(hass, name="Rtsp phone user")
    _phone_entry(hass, [{"ha_user_id": user.id, "name": "Rtsp phone", "enroll_id": "ep-rtsp"}])
    await _setup_signaling(hass)
    _provide(hass, "fake", "gate-1", name="Gate", camera_entity_id="camera.gate")
    _states(hass)
    store = get_content_store(hass)
    await store.async_ensure_loaded()
    await store.async_save_document({"schema_version": 4, "template": {"pages": [_template()], "rules": [], "roster": ["*"]},
                                     "overrides": {}, "dashboards": {}, "endpoints": {}}, store.doc_rev)
    client = await hass_ws_client(hass, access_token=token)
    return SimpleNamespace(hass=hass, store=store, client=client, token=token, entry=None, n=0)


def _options(world, **options):
    """The settings (push) entry, made on first use; its options are exactly `options`."""
    if world.entry is None:
        world.entry = _push_entry(world.hass, firebase=None)
    world.hass.config_entries.async_update_entry(world.entry, options=options)


async def _get(world):
    world.n += 1
    reply = await _send(world.client, "portico/content/get", msg_id=world.n)
    assert reply["success"], reply
    return reply["result"]


def _tiles(result):
    return {c["id"]: c for c in result["pages"][0]["blocks"][0]["cameras"]}


async def test_nothing_is_emitted_while_the_address_is_empty(phone):
    before = await _get(phone)
    assert all("live_rtsp" not in c for c in _tiles(before).values())
    _options(phone, live_sub_streams=["gate_intercom"])                       # a sub list alone switches nothing on
    assert all("live_rtsp" not in c for c in _tiles(await _get(phone)).values())
    _options(phone, live_rtsp_base="", live_sub_streams=["gate_intercom"])
    after = await _get(phone)
    assert after == before, "an empty address is byte-for-byte the document without the options"


async def test_with_an_address_a_live_frigate_tile_gets_main_and_sub_only_when_listed(phone):
    _options(phone, live_rtsp_base=BASE, live_sub_streams=["gate_intercom"])
    tiles = _tiles(await _get(phone))
    assert tiles["gate"]["live_rtsp"] == {"main": f"{BASE}/gate_intercom", "sub": f"{BASE}/gate_intercom_sub"}
    assert tiles["yard"]["live_rtsp"] == {"main": f"{BASE}/yard"}, "not in the list: no sub, the app uses main"
    for cid in ("still", "tablet", "half", "odd"):
        assert "live_rtsp" not in tiles[cid], cid
    assert json.dumps(tiles["gate"]["live_rtsp"]).count("camera.") == 0, "never an entity id"


async def test_the_options_decide_the_streams_and_the_list_alone_adds_sub(phone):
    _options(phone, live_rtsp_base="rtsp://go2rtc.lan")
    assert _tiles(await _get(phone))["gate"]["live_rtsp"] == {"main": "rtsp://go2rtc.lan/gate_intercom"}
    _options(phone, live_rtsp_base="rtsp://go2rtc.lan", live_sub_streams=["yard"])
    tiles = _tiles(await _get(phone))
    assert "sub" not in tiles["gate"]["live_rtsp"] and tiles["yard"]["live_rtsp"]["sub"] == "rtsp://go2rtc.lan/yard_sub"


async def test_options_written_by_hand_that_do_not_validate_switch_it_off(phone):
    for bad in ("rtsp://user:pw@10.6.0.6:8554", "http://10.6.0.6:8554", "rtsp://10.6.0.6:8554/gate", 5):
        _options(phone, live_rtsp_base=bad, live_sub_streams=["gate_intercom"])
        assert all("live_rtsp" not in c for c in _tiles(await _get(phone)).values()), bad


async def test_only_the_cameras_the_endpoints_own_document_holds_carry_a_hint(phone):
    """The grant: the resolved document of THIS endpoint. A camera that is only in another page set (the template's, once the
    phone has an override of its own) is not in the reply at all, and its stream name appears nowhere in it."""
    document = phone.store.document()
    other = _page([_blk("o", "Own", cameras=[{**_cam("yard", entity="camera.yard"), "live": True}])], pid="home")
    document["overrides"] = {"ep-rtsp": {"pages": [other], "rules": [], "roster_hide": []}}
    _states(phone.hass)
    await phone.store.async_save_document(document, phone.store.doc_rev)
    _options(phone, live_rtsp_base=BASE)
    result = await _get(phone)
    assert set(_tiles(result)) == {"yard"} and _tiles(result)["yard"]["live_rtsp"] == {"main": f"{BASE}/yard"}
    assert "gate_intercom" not in json.dumps(result["pages"]) and "secret_cam" not in json.dumps(result)
    # the gate door's own camera is a CALLER camera (the ring screen shows it), so its hint is in `callers`, and only there
    (door_camera,) = _door_cameras(result)
    assert door_camera["live_rtsp"] == {"main": f"{BASE}/gate_intercom"}


async def test_a_camera_that_blinks_keeps_its_hint_and_the_rev(phone):
    _options(phone, live_rtsp_base=BASE)
    up = await _get(phone)
    phone.hass.states.async_set("camera.gate", "unavailable", {"friendly_name": "Gate"})        # Frigate went away: attributes gone
    phone.hass.states.async_set("camera.yard", "unavailable", {})
    down = await _get(phone)
    assert _tiles(down)["gate"]["live_rtsp"] == _tiles(up)["gate"]["live_rtsp"]
    assert down["rev"] == up["rev"], "a camera blinking does not make every phone refetch"


async def test_the_rev_moves_when_the_address_or_the_list_changes(phone):
    revs = []
    for options in ({}, {"live_rtsp_base": BASE}, {"live_rtsp_base": BASE, "live_sub_streams": ["gate_intercom"]},
                    {"live_rtsp_base": "rtsp://go2rtc.lan", "live_sub_streams": ["gate_intercom"]}, {}):
        _options(phone, **options)
        revs.append((await _get(phone))["rev"])
    assert len(set(revs[:4])) == 4 and revs[4] == revs[0], revs


async def test_a_document_never_stores_it_and_content_document_and_preview_carry_it(phone, hass_admin_user, hass_ws_client):
    _options(phone, live_rtsp_base=BASE, live_sub_streams=["gate_intercom"])
    admin = await hass_ws_client(phone.hass)
    document_reply = await _send(admin, "portico/content/document", msg_id=1)
    assert document_reply["success"], document_reply
    result = document_reply["result"]
    assert "live_rtsp" not in json.dumps(result["document"]), "the stored document has none"
    tiles = _tiles(result["resolved"]["ep-rtsp"])
    assert tiles["gate"]["live_rtsp"] == {"main": f"{BASE}/gate_intercom", "sub": f"{BASE}/gate_intercom_sub"}
    assert "live_rtsp" not in tiles["tablet"]
    # a draft that carries a hand-made one: the preview resolves it fresh and the save path keeps nothing of it
    draft = json.loads(json.dumps(result["document"]))
    draft["template"]["pages"][0]["blocks"][0]["cameras"][3]["live_rtsp"] = {"main": "rtsp://evil.example/x"}
    preview = await _send(admin, "portico/content/preview", msg_id=2, document=draft, endpoint="ep-rtsp")
    assert preview["success"], preview
    pv = _tiles(preview["result"])
    assert pv["gate"]["live_rtsp"] == tiles["gate"]["live_rtsp"] and "live_rtsp" not in pv["tablet"]
    assert "evil.example" not in json.dumps(preview["result"])
    assert preview["result"]["rev"] == result["resolved"]["ep-rtsp"]["rev"]
    saved = await _send(admin, "portico/content/save", msg_id=3, document=draft, base_doc_rev=result["doc_rev"])
    assert saved["success"], saved
    assert "live_rtsp" not in json.dumps(phone.store.document()), "a save drops it"


async def test_the_document_without_the_options_is_the_golden_one(phone):
    """The same camera item as before the field existed: the keys `live` always had, and not one more."""
    tiles = _tiles(await _get(phone))
    assert set(tiles["gate"]) == {"id", "label", "entity_id", "size", "available", "name", "live"}
    assert set(tiles["still"]) == {"id", "label", "entity_id", "size", "available", "name"}


async def test_the_content_store_says_nothing_for_a_missing_entity_or_a_settings_entry_that_does_not_exist(phone):
    store = phone.store
    assert store.live_rtsp("camera.gate", phone.hass.states.get("camera.gate")) is None      # no settings entry at all
    _options(phone, live_rtsp_base=BASE)
    assert store.live_rtsp("camera.never_seen", None) is None
    assert store.live_rtsp("camera.gate", phone.hass.states.get("camera.gate")) == {"main": f"{BASE}/gate_intercom"}
    assert store.live_rtsp("camera.tablet", phone.hass.states.get("camera.tablet")) is None
    phone.hass.states.async_set("camera.gate", "idle", {"friendly_name": "Replaced by another integration"})
    assert store.live_rtsp("camera.gate", phone.hass.states.get("camera.gate")) is None, "an answering non-Frigate camera has no name"
    phone.hass.states.async_set("camera.gate", "unavailable", {})
    assert store.live_rtsp("camera.gate", phone.hass.states.get("camera.gate")) is None, "and the remembered name went with it"


# ---- the caller's cameras: the ring and in-call screens (DEV-713 S2-1) ------------------------------------------------------

class _HintCallerRegistry(rules.FakeRegistry):
    """The rules tests' registry plus the hint, the way the store offers it; `asked` records every entity it was asked about."""

    def __init__(self, names):
        super().__init__([rules.ALICE, rules.DINA, rules.GATE, rules.SIDE], doors=[rules.GATE, rules.SIDE])
        self.names, self.asked = names, []

    def live_rtsp(self, entity_id, state):
        self.asked.append(entity_id)
        return live_rtsp.hint(BASE, ["gate_intercom"], self.names.get(entity_id))


NAMES = {"camera.gate": "gate_intercom", "camera.driveway": "driveway"}


def _caller_of(doc, registry, caller=rules.GATE, callee="alice"):
    return resolve_caller(doc, callee, caller, states=rules.STATES, registry=registry)


async def test_a_caller_camera_carries_the_hint_with_no_live_flag_and_no_entity_id():
    registry = _HintCallerRegistry(NAMES)
    doc = rules._doc(rules=[rules._rule("r", "kind:door", cameras=["camera.gate", "camera.driveway", "camera.kitchen"])])
    cameras = _caller_of(doc, registry)["cameras"]
    assert [c["live_rtsp"] for c in cameras[:2]] == [
        {"main": f"{BASE}/gate_intercom", "sub": f"{BASE}/gate_intercom_sub"}, {"main": f"{BASE}/driveway"}]
    assert "live_rtsp" not in cameras[2], "a camera the registry has no hint for (not Frigate) carries none"
    assert all("live" not in c for c in cameras), "a caller camera has no per-camera live flag, and none is invented"
    assert all(set(c) - {"live_rtsp"} == {"id", "label", "name", "available"} for c in cameras)
    assert "camera." not in json.dumps([c["live_rtsp"] for c in cameras[:2]])


async def test_a_registry_without_the_hint_resolves_the_caller_exactly_as_before():
    doc = rules._doc(rules=[rules._rule("r", "kind:door", cameras=["camera.gate", "camera.driveway"])])
    plain = _caller_of(doc, rules.REGISTRY)
    assert all(set(c) == {"id", "label", "name", "available"} for c in plain["cameras"])
    with_hint = _caller_of(doc, _HintCallerRegistry(NAMES))
    for cam in with_hint["cameras"]:
        cam.pop("live_rtsp")
    assert with_hint == plain, "the hint is the only difference"
    off = _HintCallerRegistry({})
    assert _caller_of(doc, off) == plain, "an option that is off (the store answers None for every camera) adds nothing"


async def test_only_the_cameras_the_endpoint_is_shown_are_asked_about_and_carry_it():
    registry = _HintCallerRegistry(NAMES)
    doc = rules._doc(rules=[rules._rule("r", "kind:door", cameras=["lock.gate", "camera.driveway", 7])])
    cameras = _caller_of(doc, registry)["cameras"]
    assert [c["live_rtsp"]["main"] for c in cameras] == [f"{BASE}/driveway"]
    assert registry.asked == ["camera.driveway"], "an entity that is not a camera is dropped before any hint is asked for"
    registry.asked.clear()
    assert _caller_of(rules._doc(rules=[rules._rule("r", "kind:door", cameras=[])]), registry)["cameras"] == []
    assert registry.asked == []
    # the whole document: other callers' cameras only, never the callee's own entry, never the unknown caller `*`
    resolved = resolve_endpoint(rules._doc(), "alice", states=rules.STATES, registry=registry)
    assert set(resolved["callers"]) == {"dina", rules.GATE_ID, rules.SIDE_ID, "*"}
    assert resolved["callers"]["*"]["cameras"] == [] and resolved["callers"]["dina"]["cameras"] == []
    assert resolved["callers"][rules.GATE_ID]["cameras"][0]["live_rtsp"]["main"] == f"{BASE}/gate_intercom"
    assert "live_rtsp" not in resolved["callers"][rules.SIDE_ID]["cameras"][0], "camera.side has no Frigate name"
    narrowed = resolve_endpoint(rules._doc(), "alice", states=rules.STATES, registry=registry, caller=rules.SIDE_ID)
    assert set(narrowed["callers"]) == {rules.SIDE_ID, "*"}


async def test_the_grant_names_exactly_the_cameras_the_caller_block_lists():
    """`caller_camera_entities` (the grant of `content/live` and `content/snapshot` for a `c-` id) and `resolve_caller` (the ids the
    ring screen holds) agree, a declaration that holds a non-camera entity and a rule of none included."""
    declared = rules._ep("fake:odd", "door", "Odd", cameras=("lock.gate", "camera.gate", "camera.driveway"))
    for doc in (rules._doc(), rules._doc(rules=[rules._rule("r", "kind:door", cameras=["camera.kitchen", "camera.gate"])]),
                rules._doc(rules=[rules._rule("r", "kind:door", cameras=[])]),
                rules._doc(rules=[rules._rule("r", "kind:door", cameras=None)])):
        shown = [c["id"] for c in _caller_of(doc, _HintCallerRegistry(NAMES), caller=declared)["cameras"]]
        granted = [short_id("c", e) for e in caller_camera_entities(doc, "alice", declared)]
        assert shown == granted, doc["template"]["rules"]
    assert caller_camera_entities(rules._doc(), "alice", declared) == ["camera.gate", "camera.driveway"]
    assert caller_camera_entities(rules._doc(), "alice", None) == []


async def test_the_caller_hint_is_part_of_rev():
    def rev(names):
        return resolve_endpoint(rules._doc(), "alice", states=rules.STATES, registry=_HintCallerRegistry(names))["rev"]

    assert rev(NAMES) != rev({})
    assert rev(NAMES) != rev({"camera.gate": "doorbell"})
    assert rev(NAMES) == rev(NAMES)
    assert rev({}) == resolve_endpoint(rules._doc(), "alice", states=rules.STATES, registry=rules.REGISTRY)["rev"], \
        "with no hint anywhere the document, rev included, is what it was"


def _door_cameras(result):
    (door,) = [c for c in result["callers"].values() if c["kind"] == "door"]
    return door["cameras"]


async def test_through_the_real_store_the_caller_camera_follows_the_options(phone):
    before = await _get(phone)
    (cam,) = _door_cameras(before)
    assert cam["id"] == short_id("c", "camera.gate") and "live_rtsp" not in cam
    _options(phone, live_sub_streams=["gate_intercom"])                      # a sub list alone switches nothing on
    assert await _get(phone) == before
    _options(phone, live_rtsp_base=BASE, live_sub_streams=["gate_intercom"])
    result = await _get(phone)
    (cam,) = _door_cameras(result)
    assert cam["live_rtsp"] == {"main": f"{BASE}/gate_intercom", "sub": f"{BASE}/gate_intercom_sub"}
    assert "live" not in cam and "camera.gate" not in json.dumps(result["callers"]), "no flag, never an entity id"
    assert result["rev"] != before["rev"]
    _options(phone, live_rtsp_base=BASE)
    assert _door_cameras(await _get(phone))[0]["live_rtsp"] == {"main": f"{BASE}/gate_intercom"}
    _options(phone, live_rtsp_base="", live_sub_streams=["gate_intercom"])
    assert await _get(phone) == before, "an empty address is byte-for-byte the document without the options"


async def test_the_caller_camera_of_a_non_frigate_entity_and_of_a_blink(phone):
    _options(phone, live_rtsp_base=BASE)
    up = await _get(phone)
    phone.hass.states.async_set("camera.gate", "unavailable", {"friendly_name": "Gate"})        # Frigate went away
    down = await _get(phone)
    assert _door_cameras(down)[0]["live_rtsp"] == _door_cameras(up)[0]["live_rtsp"]
    assert down["rev"] == up["rev"], "a camera blinking does not make every phone refetch"
    phone.hass.states.async_set("camera.gate", "idle", {"friendly_name": "Gate"})                       # answers, not Frigate
    assert "live_rtsp" not in _door_cameras(await _get(phone))[0]


async def test_a_rule_that_shows_no_camera_shows_no_hint_and_the_hint_is_never_stored(phone, hass_ws_client):
    document = phone.store.document()
    document["template"]["rules"] = [{"id": "r1", "caller": "kind:door", "cameras": ["camera.yard"], "ring_buttons": [],
                                      "call_buttons": []}]
    await phone.store.async_save_document(document, phone.store.doc_rev)
    _options(phone, live_rtsp_base=BASE)
    (cam,) = _door_cameras(await _get(phone))
    assert cam["id"] == short_id("c", "camera.yard") and cam["live_rtsp"] == {"main": f"{BASE}/yard"}
    assert "gate_intercom" not in json.dumps((await _get(phone))["callers"]), "a camera the caller block no longer lists is not named"
    admin = await hass_ws_client(phone.hass)
    reply = await _send(admin, "portico/content/document", msg_id=1)
    assert reply["success"]
    assert "live_rtsp" not in json.dumps(reply["result"]["document"]), "the stored document has none"
    resolved = reply["result"]["resolved"]["ep-rtsp"]
    (door,) = [c for c in resolved["callers"].values() if c["kind"] == "door"]
    assert door["cameras"][0]["live_rtsp"] == {"main": f"{BASE}/yard"}, "content/document carries it too"
    document["template"]["rules"][0]["cameras"] = []
    await phone.store.async_save_document(document, phone.store.doc_rev)
    assert _door_cameras(await _get(phone)) == []
    assert "yard" not in json.dumps((await _get(phone))["callers"])


# ---- DEV-715: a camera's own away addresses ----------------------------------------------------------------------------------

async def test_an_away_stream_address_is_a_full_rtsp_url_without_credentials_or_query():
    assert live_rtsp.normalize_stream_url("RTSP://Cam.lan:554/ch1/main") == "rtsp://cam.lan:554/ch1/main"
    assert live_rtsp.normalize_stream_url("rtsp://10.6.0.9") == "rtsp://10.6.0.9"
    for bad in ("rtsp://u:p@10.6.0.9/a", "rtsp://10.6.0.9/a?x=1", "rtsp://10.6.0.9/a#f", "http://10.6.0.9/a", "rtsp://10.6.0.256/a",
                "rtsp://10.6.0.9:554@evil/a", "rtsp://10.6.0.9/a b", "rtsp://" + "a" * 300, "", None, 3):
        assert live_rtsp.normalize_stream_url(bad) is None, bad


async def test_explicit_streams_win_over_the_frigate_convention_and_need_no_base(phone):
    explicit = {"camera.yard": {"main": "rtsp://10.6.0.9:554/ch1", "sub": "rtsp://10.6.0.9:554/ch2"}}
    _options(phone, live_away_streams=explicit)                                  # no live_rtsp_base at all
    tiles = _tiles(await _get(phone))
    assert tiles["yard"]["live_rtsp"] == explicit["camera.yard"]
    assert "live_rtsp" not in tiles["gate"], "the others stay off while the base is empty"
    _options(phone, live_rtsp_base=BASE, live_sub_streams=["gate_intercom"], live_away_streams=explicit)
    tiles = _tiles(await _get(phone))
    assert tiles["yard"]["live_rtsp"] == explicit["camera.yard"], "explicit wins over <base>/<camera_name>"
    assert tiles["gate"]["live_rtsp"] == {"main": f"{BASE}/gate_intercom", "sub": f"{BASE}/gate_intercom_sub"}
    assert "live_rtsp" not in tiles["still"], "a tile that is not live still gets nothing"


async def test_a_hand_written_bad_away_map_reads_as_none(phone):
    _options(phone, live_away_streams={"camera.yard": {"main": "rtsp://u:p@10.6.0.9/a"}})
    assert all("live_rtsp" not in c for c in _tiles(await _get(phone)).values())
