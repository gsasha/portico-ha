"""The driver part's side of the house-call bridge (M7 3b-S): feature detection, the `-house-calls` flag, the hub verbs and the
`house_leg_state` routing. INERT: a hub whose wheel is older than 0.6.0 (or whose option is off) behaves exactly as before.

HA-harness session. The wheel's version, the process (its argv, and whether it would EXIT on a flag it does not know) and the
IPC client are fakes; the hub, the doors and the provider are real. Real audio is not involved and cannot be tested here."""
from __future__ import annotations

import asyncio
import re

import pytest
from homeassistant.helpers import issue_registry as ir

from custom_components.portico.door import hub as portico_hub
from custom_components.portico.door.const import DOMAIN, DRIVER_ID
from custom_components.portico.door.provider_impl import DATA_TRANSPORT_PROVIDER
from custom_components.portico.door.transport import TransportClient, TransportError

from .test_observer import (  # noqa: F401 - fixtures by name
    DAHUA_HOST,
    GATE_SIP_URL,
    FakeGoTransport,
    _custom_integrations,
    _dahua_entry,
    _FakeProcess,
    _observer_entry,
    fake_transport,
    get_hub,
    wait_for,
)

pytestmark = pytest.mark.asyncio


class FakeHouseGo(FakeGoTransport):
    """The fake binary's IPC client, plus the two house verbs as a 0.6.0 hub with `-house-calls` answers them."""

    def __init__(self, socket_path: str = "") -> None:
        super().__init__(socket_path)
        self.opened: list[tuple[str, list]] = []
        self.closed: list[str] = []

    async def house_open(self, call_id: str, legs: list) -> dict:
        self.requests.append(("house_open", (call_id,)))
        self.opened.append((call_id, legs))
        return {"session": "house-0001", "legs": [
            {"side": leg["side"], "kind": leg["kind"], "state": "attached", "leg_id": f"house-0001-{leg['side']}",
             "codec": leg.get("codec"), "frame_bytes": 320, "frame_ms": 40} for leg in legs]}

    async def house_close(self, call_id: str) -> dict:
        self.requests.append(("house_close", (call_id,)))
        self.closed.append(call_id)
        return {"ok": True, "closed": True}


class _StrictProcess(_FakeProcess):
    """A binary that EXITS at once on an argv flag it does not know, as a Go `flag` set does (0.5.1: `-socket` only)."""

    def __init__(self, argv: tuple, known: set[str]) -> None:
        super().__init__()
        self.argv = argv
        unknown = [a for a in argv[1:] if a.startswith("-") and a not in known]
        if unknown:
            self.returncode = 2
            self._exit.set()


def _install(monkeypatch, version: str | None, *, known=frozenset({"-socket"})):
    """A hub whose wheel reports `version` and whose binary knows exactly `known` flags."""
    monkeypatch.setattr(portico_hub, "TransportClient", FakeHouseGo)
    if version is None:
        def _missing(name):
            import importlib.metadata as md
            raise md.PackageNotFoundError(name)

        monkeypatch.setattr(portico_hub.importlib.metadata, "version", _missing)
    else:
        monkeypatch.setattr(portico_hub.importlib.metadata, "version", lambda name: version)

    async def _exec(*args, **kwargs):
        process = _StrictProcess(args, set(known))
        FakeGoTransport.spawned.append((args, process))
        return process

    monkeypatch.setattr(portico_hub.asyncio, "create_subprocess_exec", _exec)


def _flagged(fake_transport) -> list[bool]:
    return [portico_hub.HOUSE_CALLS_FLAG in argv for argv, _p in fake_transport.spawned]


async def _start(hass, *, house_calls=None):
    extra = {} if house_calls is None else {"options": {"house_calls": house_calls}}
    entry = _observer_entry(hass)
    if extra:
        hass.config_entries.async_update_entry(entry, options=extra["options"])
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


# ---- the flag: only a 0.6.0+ wheel AND the option, never an old binary ---------------------------------------------------------------

async def test_the_default_hub_starts_exactly_as_it_always_did(hass, fake_transport, monkeypatch):
    _install(monkeypatch, "0.6.0", known={"-socket", "-house-calls"})
    await _start(hass)
    ((argv, process),) = fake_transport.spawned
    assert argv[1] == "-socket" and len(argv) == 3, argv
    assert process.returncode is None
    hub = get_hub(hass)
    assert hub.house_calls_active is False and _flagged(fake_transport) == [False]


@pytest.mark.parametrize("version", ["0.5.0", "0.5.1", "0.5.9", "0.6.0rc1", "0.6.0.dev1", None])
async def test_an_old_or_unreadable_wheel_never_gets_the_flag_even_with_the_option_on(hass, fake_transport, monkeypatch, version):
    _install(monkeypatch, version)                       # a binary that EXITS on an unknown flag
    await _start(hass, house_calls=True)
    ((argv, process),) = fake_transport.spawned
    assert portico_hub.HOUSE_CALLS_FLAG not in argv and process.returncode is None, argv
    hub = get_hub(hass)
    assert hub.house_calls_active is False and hub.house_wheel_ok() is False
    assert ir.async_get(hass).async_get_issue(DOMAIN, "wheel_too_old") is None


@pytest.mark.parametrize("version", ["0.6.0", "0.6.1", "0.10.0", "1.0.0"])
async def test_a_new_wheel_with_the_option_on_gets_the_flag_and_the_hub_reports_it(hass, fake_transport, monkeypatch, version):
    _install(monkeypatch, version, known={"-socket", "-house-calls"})
    await _start(hass, house_calls=True)
    ((argv, process),) = fake_transport.spawned
    assert argv[-1] == portico_hub.HOUSE_CALLS_FLAG and process.returncode is None
    assert get_hub(hass).house_calls_active is True


@pytest.mark.parametrize("option", [False, None, "yes", 1, "true"])
async def test_only_a_literal_true_turns_it_on(hass, fake_transport, monkeypatch, option):
    _install(monkeypatch, "0.6.0", known={"-socket", "-house-calls"})
    await _start(hass, house_calls=option)
    assert _flagged(fake_transport) == [False]


async def test_a_new_wheel_with_the_option_off_is_not_flagged_and_house_verbs_are_not_supported(hass, fake_transport, monkeypatch):
    _install(monkeypatch, "0.6.0", known={"-socket", "-house-calls"})
    await _start(hass, house_calls=False)
    hub = get_hub(hass)
    with pytest.raises(TransportError) as err:
        await hub.house_open("hso-1", [])
    assert err.value.code == "not_supported"
    assert fake_transport.instances[0].requests == [] or all(r[0] != "house_open" for r in fake_transport.instances[0].requests)


async def test_the_option_on_a_second_door_counts_at_the_next_hub_start_not_before(hass, fake_transport, monkeypatch):
    """The flag is a property of the process. A door that joins a running hub cannot change it; the hub that restarts
    (or starts after the last door left) reads every attached door's option."""
    _install(monkeypatch, "0.6.0", known={"-socket", "-house-calls"})
    observer = await _start(hass)                                    # option absent: unflagged process
    dahua = _dahua_entry(hass)
    hass.config_entries.async_update_entry(dahua, options={"house_calls": True})
    assert await hass.config_entries.async_setup(dahua.entry_id)
    await hass.async_block_till_done()
    assert len(fake_transport.spawned) == 1 and _flagged(fake_transport) == [False]
    assert get_hub(hass).house_calls_active is False
    # the hub's one restart now reads the doors attached: the dahua door's option is on
    fake_transport.spawned[0][1].terminate()
    await wait_for(hass, lambda: len(fake_transport.spawned) == 2, "the restart")
    assert _flagged(fake_transport) == [False, True]
    assert observer.state.value == "loaded"


async def test_the_attaching_door_counts_for_the_process_it_starts(hass, fake_transport, monkeypatch):
    _install(monkeypatch, "0.6.0", known={"-socket", "-house-calls"})
    await _start(hass, house_calls=True)                             # first door starts the process: its own option counts
    assert _flagged(fake_transport) == [True]


async def test_a_crash_restart_decides_the_flag_again_and_the_bridge_is_off_in_between(hass, fake_transport, monkeypatch):
    _install(monkeypatch, "0.6.0", known={"-socket", "-house-calls"})
    await _start(hass, house_calls=True)
    hub = get_hub(hass)
    assert hub.house_calls_active
    fake_transport.spawned[0][1].terminate()
    await wait_for(hass, lambda: len(fake_transport.spawned) == 2 and hub.house_calls_active, "the restart")
    assert _flagged(fake_transport) == [True, True]


async def test_the_floor_stayed_and_the_pin_is_the_0_6_5_wheel(hass):
    """The pin PRs moved the wheel to 0.6.0, then 0.6.1, 0.6.2, then 0.6.3, then 0.6.4, then 0.6.5, then 0.6.6, then 0.6.7, then 0.6.8, then 0.6.9, then 0.6.10, then 0.7.0, then 0.7.1, then 0.7.2, then 0.7.3, then 0.7.4 and left MIN_WHEEL_VERSION at 0.5.0: every house path is feature-detected, so an
    old wheel must keep serving every door (a raised floor would raise wheel_too_old on all of them)."""
    from custom_components.portico.door.wheel import WHEEL_REQUIREMENT

    assert portico_hub.MIN_WHEEL_VERSION == "0.5.0" and portico_hub.HOUSE_CALLS_MIN_WHEEL == "0.6.0"
    pins = WHEEL_REQUIREMENT           # (the pin is a constant in code since the fold: it left the manifest, wheel.py)
    # Either release tag family: Go `transport-v<ver>/` or Rust `transport-rs-v<ver>/` (the cut-over pin PR changes the
    # version here, not the shape of the check). Token match, not substring: "0.6.1" is a prefix of "0.6.10".
    def _says(version):
        return bool(re.search(rf"/transport(?:-rs)?-v{re.escape(version)}/", pins)) or f"portico_transport-{version}-" in pins

    assert _says("0.7.4") and "portico_transport-0.7.4-" in pins, pins
    for old in ("0.5.1", "0.6.0", "0.6.1", "0.6.2", "0.6.3", "0.6.4", "0.6.5", "0.6.6", "0.6.7", "0.6.8", "0.6.9", "0.6.10", "0.7.0", "0.7.1", "0.7.2", "0.7.3"):
        assert not _says(old), (old, pins)


# ---- the verbs ----------------------------------------------------------------------------------------------------------------------

async def test_house_open_and_close_reach_the_hub_only_when_it_runs_the_bridge(hass, fake_transport, monkeypatch):
    _install(monkeypatch, "0.6.0", known={"-socket", "-house-calls"})
    await _start(hass, house_calls=True)
    hub = get_hub(hass)
    legs = [{"side": "from", "kind": "frames", "codec": "pcmu", "owner": "a"},
            {"side": "to", "kind": "frames", "codec": "pcmu", "owner": "b"}]
    result = await hub.house_open("hso-1", legs)
    assert result["session"] == "house-0001"
    go = fake_transport.instances[0]
    assert go.opened == [("hso-1", legs)]
    assert (await hub.house_close("hso-1")) == {"ok": True, "closed": True} and go.closed == ["hso-1"]


async def test_a_hub_without_a_process_is_not_supported(hass, fake_transport, monkeypatch):
    _install(monkeypatch, "0.6.0", known={"-socket", "-house-calls"})
    hub = get_hub(hass)                                              # no door attached: no process
    for call in (lambda: hub.house_open("hso-1", []), lambda: hub.house_close("hso-1")):
        with pytest.raises(TransportError) as err:
            await call()
        assert err.value.code == "not_supported"


async def test_the_provider_reports_the_capability_and_only_then_forwards(hass, fake_transport, monkeypatch):
    _install(monkeypatch, "0.5.1")
    await _start(hass, house_calls=True)
    provider = hass.data[DRIVER_ID][DATA_TRANSPORT_PROVIDER]
    assert provider.house_bridge_supported() is False
    with pytest.raises(TransportError) as err:
        await provider.house_open("hso-1", [])
    assert err.value.code == "not_supported"
    with pytest.raises(TransportError) as err:
        await provider.house_close("hso-1")
    assert err.value.code == "not_supported"
    assert not [r for r in fake_transport.instances[0].requests if r[0].startswith("house")]


async def test_the_provider_with_a_new_wheel_opens_registers_legs_and_closes(hass, fake_transport, monkeypatch):
    _install(monkeypatch, "0.6.0", known={"-socket", "-house-calls"})
    await _start(hass, house_calls=True)
    provider = hass.data[DRIVER_ID][DATA_TRANSPORT_PROVIDER]
    go = fake_transport.instances[0]
    assert provider.house_bridge_supported() is True
    legs = [{"side": "from", "kind": "frames", "codec": "pcmu"}, {"side": "to", "kind": "frames", "codec": "pcmu"}]
    result = await provider.house_open("hso-1", legs)
    leg_ids = [leg["leg_id"] for leg in result["legs"]]
    got: list = []
    closed: list = []
    provider.house_media_open(leg_ids[0], got.append, closed.append)
    # the hub's downlink and `media_closed` for the leg reach the callbacks (the same notifications the door path uses)
    go.notify("media_frame", {"call_id": leg_ids[0], "d": "AAAA"})
    go.notify("media_frame", {"call_id": leg_ids[1], "d": "unregistered"})
    assert got == ["AAAA"]
    go.notify("media_closed", {"call_id": leg_ids[0], "reason": "idle"})
    assert closed == ["idle"]
    # uplink goes to the hub's client under the leg id, never through a door
    sent: list = []
    go.media_frame_nowait = lambda call_id, data: sent.append((call_id, data))
    provider.house_media_frame(leg_ids[1], "BBBB")
    assert sent == [(leg_ids[1], "BBBB")]
    # closing forgets what the open remembered
    provider.house_media_open(leg_ids[1], got.append, closed.append)
    await provider.house_close("hso-1")
    assert leg_ids[1] not in provider._media and "hso-1" not in provider._house_legs
    assert "hso-1" not in provider._call_doors


async def test_a_house_id_never_enters_the_door_tables(hass, fake_transport, monkeypatch):
    _install(monkeypatch, "0.6.0", known={"-socket", "-house-calls"})
    await _start(hass, house_calls=True)
    provider = hass.data[DRIVER_ID][DATA_TRANSPORT_PROVIDER]
    await provider.house_open("hso-1", [{"side": "from", "kind": "frames", "codec": "pcmu"},
                                         {"side": "to", "kind": "frames", "codec": "pcmu"}])
    hub = get_hub(hass)
    assert "hso-1" not in provider._call_doors and "hso-1" not in hub._call_doors
    for door_verb in (lambda: provider.hangup("hso-1", "x"), lambda: provider.keepalive("hso-1"),
                      lambda: provider.webrtc_offer("hso-1", "sdp"), lambda: provider.media_open("hso-1", "pcmu", print, print)):
        with pytest.raises(TransportError) as err:
            await door_verb()
        assert err.value.code == "unknown_call"


# ---- house_leg_state ------------------------------------------------------------------------------------------------------------------

async def test_house_leg_states_reach_the_listeners_by_call_id_and_never_a_door(hass, fake_transport, monkeypatch, caplog):
    _install(monkeypatch, "0.6.0", known={"-socket", "-house-calls"})
    entry = await _start(hass, house_calls=True)
    provider = hass.data[DRIVER_ID][DATA_TRANSPORT_PROVIDER]
    seen: list = []
    unsub = provider.subscribe_house(seen.append)
    go = fake_transport.instances[0]
    go.notify("house_leg_state", {"call_id": "hso-1", "leg": "to", "state": "first_frame"})
    go.notify("house_leg_state", {"call_id": "hso-1", "leg": "from", "state": "sdp", "sdp": "v=0"})
    go.notify("house_leg_state", {"leg": "to", "state": "lost"})              # no call id: dropped
    assert seen == [{"call_id": "hso-1", "leg": "to", "state": "first_frame"},
                    {"call_id": "hso-1", "leg": "from", "state": "sdp", "sdp": "v=0"}]
    assert "dropping" not in caplog.text                                     # it is not a door notification gone astray
    runtime = entry.runtime_data
    assert runtime.call_state.state == "idle"
    unsub()
    go.notify("house_leg_state", {"call_id": "hso-2", "leg": "to", "state": "lost"})
    assert len(seen) == 2


async def test_a_raising_listener_does_not_starve_the_others(hass, fake_transport, monkeypatch):
    _install(monkeypatch, "0.6.0", known={"-socket", "-house-calls"})
    await _start(hass, house_calls=True)
    provider = hass.data[DRIVER_ID][DATA_TRANSPORT_PROVIDER]
    seen: list = []

    def _boom(_leg):
        raise RuntimeError("bad listener")

    provider.subscribe_house(_boom)
    provider.subscribe_house(seen.append)
    fake_transport.instances[0].notify("house_leg_state", {"call_id": "hso-1", "leg": "to", "state": "lost"})
    assert len(seen) == 1


async def test_the_process_going_away_tells_the_listeners_once_per_open_bridge(hass, fake_transport, monkeypatch):
    _install(monkeypatch, "0.6.0", known={"-socket", "-house-calls"})
    await _start(hass, house_calls=True)
    provider = hass.data[DRIVER_ID][DATA_TRANSPORT_PROVIDER]
    seen: list = []
    provider.subscribe_house(seen.append)
    legs = [{"side": "from", "kind": "frames", "codec": "pcmu"}, {"side": "to", "kind": "frames", "codec": "pcmu"}]
    await provider.house_open("hso-1", legs)
    await provider.house_open("hso-2", legs)
    await provider.house_close("hso-2")                                      # closed: nothing to lose
    fake_transport.spawned[0][1].terminate()
    await wait_for(hass, lambda: len(fake_transport.spawned) == 2, "the restart")
    assert seen == [{"call_id": "hso-1", "leg": "both", "state": "lost", "reason": "transport_gone"}]


async def test_a_hub_that_never_opened_a_bridge_says_nothing_when_its_process_goes_away(hass, fake_transport, monkeypatch):
    _install(monkeypatch, "0.6.0", known={"-socket", "-house-calls"})
    await _start(hass, house_calls=True)
    seen: list = []
    hass.data[DRIVER_ID][DATA_TRANSPORT_PROVIDER].subscribe_house(seen.append)
    fake_transport.spawned[0][1].terminate()
    await wait_for(hass, lambda: len(fake_transport.spawned) == 2, "the restart")
    assert seen == []


async def test_door_notifications_and_door_audio_are_routed_exactly_as_before(hass, fake_transport, monkeypatch):
    """The two door paths this PR sits beside: a ring by device id and a door's media frame by call id."""
    _install(monkeypatch, "0.6.0", known={"-socket", "-house-calls"})
    entry = await _start(hass, house_calls=True)
    provider = hass.data[DRIVER_ID][DATA_TRANSPORT_PROVIDER]
    go = fake_transport.instances[0]
    go.ring("gso-1", GATE_SIP_URL)
    assert entry.runtime_data.call_state.call_id == "gso-1" and entry.runtime_data.call_state.state == "ringing"
    got: list = []
    provider._media["gso-1"] = (got.append, got.append)
    go.notify("media_frame", {"call_id": "gso-1", "d": "door-audio"})
    assert got == ["door-audio"]


# ---- the IPC client's wire --------------------------------------------------------------------------------------------------------------

async def test_the_client_sends_the_two_verbs_in_the_hubs_shapes():
    sent: list = []
    client = TransportClient("/nonexistent")

    async def _request(method, params):
        sent.append((method, params))
        return {}

    client._request = _request
    await client.house_open("hso-1", [{"side": "from", "kind": "frames", "codec": "pcmu"}])
    await client.house_close("hso-1")
    assert sent == [("house_open", {"call_id": "hso-1", "legs": [{"side": "from", "kind": "frames", "codec": "pcmu"}]}),
                    ("house_close", {"call_id": "hso-1"})]


async def test_the_provider_seam_is_still_version_1_and_conforms():
    from custom_components.portico.door import provider_impl

    assert provider_impl.API_VERSION == 1 == provider_impl.TransportProvider.API_VERSION


# ---- the counters line (M7 3g) -----------------------------------------------------------------------------------------------------

async def test_the_house_call_counters_line_is_logged_at_info_and_everything_else_stays_debug(hass, fake_transport, caplog):
    import logging

    line = ("portico-transport: house call hso-1 ended reason=hangup cause=- dur=12.34s "
            "from[frames rx=300 tx=290 dropped=0 bad=0 concealed=2 maxgap=120ms] "
            "to[frames rx=290 tx=300 dropped=1 bad=0 concealed=0 maxgap=40ms]")
    other = "portico-transport: sip registered"

    class _Proc:
        stdout = asyncio.StreamReader()

    for text in (other, line, "house call without the prefix"):
        _Proc.stdout.feed_data(text.encode() + b"\n")
    _Proc.stdout.feed_eof()
    hub = get_hub(hass)
    with caplog.at_level(logging.INFO, logger=portico_hub.__name__):
        await hub._drain_subprocess_output(_Proc())
    infos = [r.getMessage() for r in caplog.records if r.name == portico_hub.__name__ and r.levelno == logging.INFO]
    assert infos == ["portico transport: " + line]
