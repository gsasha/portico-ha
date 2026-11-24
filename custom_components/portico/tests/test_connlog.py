"""connlog.py (DEV-705): the validator, the bounded store, and the three commands as each kind of caller. HA-harness session."""
from __future__ import annotations

import json
import types

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from .. import auth, connlog
from ..config_flow import DOMAIN
from .test_push_registry import _phone_user
from .test_signaling import _setup_signaling

pytestmark = pytest.mark.asyncio


def _report(rid: str = "r1", **extra) -> dict:
    return {
        "v": 1, "id": rid, "app": "0.6.7-alpha", "sdk": 35, "t0": 1_761_000_000_000, "dur": 2330, "result": "connected",
        "trigger": {"kind": "net_change", "from": "wifi", "to": "cellular", "validated": True},
        "env": {"fg": False, "doze": False, "saver": False, "metered": True, "proc_ms": 183000, "cold": False},
        "net": [{"t": 0, "def": "wifi", "wifi": 1, "cell": 1, "vpn": 0, "val": 1},
                {"t": 410, "def": "cellular", "wifi": 0, "cell": 1, "vpn": 0, "val": 1}],
        "hint": {"mode": "TAILNET_FIRST", "why": "mobile data, no validated Wi-Fi: not the home LAN", "mobile": 1},
        "ev": [{"t": 6, "e": "tailnet_started"}, {"t": 13, "e": "direct_failed", "cls": "NAME_NOT_RESOLVED"},
               {"t": 2300, "e": "tailnet_open"}, {"t": 2330, "e": "connected"}],
        "tsnet": "needslogin+13ms starting+31ms running+287ms", "path": "tailnet", **extra,
    }


# ---- the validator (no HA needed) ----

def test_a_good_report_passes_unchanged():
    r = _report()
    assert connlog.sanitize_report(r) == r


@pytest.mark.parametrize("bad", [None, "x", [], {}, {"v": 2, "id": "a"}, {"v": 1}, {"v": 1, "id": ""},
                                 {"v": 1, "id": "has space"}, {"v": 1, "id": "x" * 65}, {"v": True, "id": "a"}])
def test_what_is_not_a_v1_report_with_a_usable_id_is_rejected(bad):
    assert connlog.sanitize_report(bad) is None


def test_unknown_top_level_keys_and_bad_inner_keys_are_dropped():
    r = _report(secret="tskey-auth-xyz", ssid="HomeNet")
    r["env"]["Bad Key"] = 1
    r["env"]["ip"] = "10.6.0.5"
    out = connlog.sanitize_report(r)
    assert "secret" not in out and "ssid" not in out
    assert "Bad Key" not in out["env"]
    assert out["env"]["ip"] == "10.6.0.5"          # a lower-case key is a key; a VALUE that is an address is not special
    assert out["id"] == "r1"


def test_urls_and_long_tokens_are_redacted_and_control_characters_removed():
    r = _report()
    r["hint"]["why"] = "see https://ha.example.net/api/websocket?x=1"
    r["tsnet"] = "A" * 60
    r["path"] = "tail\x00net\n"
    out = connlog.sanitize_report(r)
    assert out["hint"]["why"] == "<redacted>"
    assert out["tsnet"] == "<redacted>"
    assert out["path"] == "tail net"


def test_numbers_are_bounded_and_nan_is_dropped():
    r = _report(dur=10**30, dropped=float("nan"))
    out = connlog.sanitize_report(r)
    assert out["dur"] == connlog.MAX_NUM
    assert "dropped" not in out
    assert connlog.sanitize_report(_report(dur=2330.7))["dur"] == 2330


def test_lists_are_capped_depth_is_capped_and_big_reports_are_rejected():
    r = _report()
    r["ev"] = [{"t": i, "e": "x"} for i in range(500)]
    assert len(connlog.sanitize_report(r)["ev"]) == connlog.LIST_CAPS["ev"]
    def depth(v):
        return 1 + max((depth(x) for x in (v.values() if isinstance(v, dict) else v)), default=0) if isinstance(v, (dict, list)) else 0

    deep = connlog.sanitize_report(_report(retries=[[[[[[1]]]]]]))
    assert depth(deep) <= connlog.MAX_DEPTH + 1
    huge = _report()
    huge["ev"] = [{"t": i, "e": "ab cd " * 31, "cls": "ef gh " * 31} for i in range(60)]   # spaces: not a long token
    assert connlog.sanitize_report(huge) is None


# ---- the store ----

class _Clock:
    def __init__(self, t: int = 1_800_000_000_000) -> None:
        self.t = t

    def __call__(self) -> int:
        return self.t


async def _store(hass, clock=None) -> connlog.ConnLogStore:
    s = connlog.ConnLogStore(hass, now_ms=clock or _Clock())
    await s.async_load()
    return s


async def test_add_counts_stored_duplicates_and_rejected(hass):
    s = await _store(hass)
    assert s.add([_report("a"), _report("b"), {"v": 9}], user_id="u1", phone="Pixel") == {"stored": 2, "duplicates": 0, "rejected": 1}
    assert s.add([_report("a"), _report("c")], user_id="u1", phone="Pixel") == {"stored": 1, "duplicates": 1, "rejected": 0}


async def test_get_pages_by_cursor(hass):
    s = await _store(hass)
    s.add([_report(f"r{i}") for i in range(5)], user_id="u", phone="P")
    first = s.get(0, 2)
    assert [e["seq"] for e in first["reports"]] == [1, 2] and first["next_seq"] == 2 and first["total"] == 5
    second = s.get(first["next_seq"], 10)
    assert [e["seq"] for e in second["reports"]] == [3, 4, 5]
    assert s.get(5, 10)["reports"] == [] and s.get(5, 10)["next_seq"] == 5
    assert set(first["reports"][0]) == {"seq", "received_ms", "phone", "report"}      # never the HA user id


async def test_the_store_is_bounded_by_age_count_and_bytes(hass, monkeypatch):
    clock = _Clock()
    s = await _store(hass, clock)
    s.add([_report("old")], user_id="u", phone="P")
    clock.t += connlog.MAX_AGE_MS + 1
    s.add([_report("new")], user_id="u", phone="P")
    assert [e["report"]["id"] for e in s._kept] == ["new"]

    monkeypatch.setattr(connlog, "MAX_REPORTS", 3)
    s2 = await _store(hass)
    s2.add([_report(f"c{i}") for i in range(6)], user_id="u", phone="P")
    assert [e["report"]["id"] for e in s2._kept] == ["c3", "c4", "c5"]
    assert "c0" not in s2._ids                     # a dropped id may come back

    monkeypatch.setattr(connlog, "MAX_REPORTS", 3000)
    monkeypatch.setattr(connlog, "MAX_BYTES", 2500)
    s3 = await _store(hass)
    s3.add([_report(f"b{i}") for i in range(10)], user_id="u", phone="P")
    assert s3._bytes <= 2500 and 0 < len(s3._kept) < 10


async def test_it_survives_a_restart(hass, hass_storage):
    s = await _store(hass)
    s.add([_report("a"), _report("b")], user_id="u", phone="P")
    await s._store.async_save(s._data())
    again = await _store(hass)
    assert [e["report"]["id"] for e in again._kept] == ["a", "b"]
    assert again.add([_report("a"), _report("c")], user_id="u", phone="P") == {"stored": 1, "duplicates": 1, "rejected": 0}
    assert again.get(0, 10)["reports"][-1]["seq"] == 3        # the sequence continues, it never repeats


async def test_a_garbled_store_costs_the_reports_not_the_server(hass, hass_storage):
    hass_storage[connlog.STORE_KEY] = {"version": 1, "key": connlog.STORE_KEY, "data": {"entries": ["x", {"seq": "no"}, 5]}}
    s = await _store(hass)
    assert s.get(0, 10)["reports"] == []


async def test_the_rate_limit_is_per_user_and_per_window(hass):
    clock = _Clock()
    s = await _store(hass, clock)
    assert s.rate_ok("u1", connlog.RATE_MAX_REPORTS)
    assert not s.rate_ok("u1", 1)
    assert s.rate_ok("u2", 1)
    clock.t += connlog.RATE_WINDOW_MS + 1
    assert s.rate_ok("u1", 1)


async def test_clear_empties_it(hass):
    s = await _store(hass)
    s.add([_report("a"), _report("b")], user_id="u", phone="P")
    assert s.clear() == 2 and s.get(0, 10)["total"] == 0
    assert s.add([_report("a")], user_id="u", phone="P")["stored"] == 1     # an id forgotten by clear can come back


# ---- the commands, as each kind of caller ----

@pytest.fixture
async def callers(hass, hass_ws_client):
    admin_ws = await hass_ws_client(hass)
    phone, phone_token = await _phone_user(hass, name="phone")
    plain, plain_token = await _phone_user(hass, name="plain")
    MockConfigEntry(domain=DOMAIN, data={"driver": "dahua_sip", "host": "h", "enrolled_phones": [
        {"ha_user_id": phone.id, "name": "Alice's Pixel"}]}, unique_id="dahua_sip:h").add_to_hass(hass)
    await _setup_signaling(hass)
    return types.SimpleNamespace(
        admin=admin_ws,
        phone=await hass_ws_client(hass, access_token=phone_token),
        user=await hass_ws_client(hass, access_token=plain_token),
    )


async def _call(ws, mid: int, body: dict) -> dict:
    await ws.send_json({"id": mid, **body})
    return await ws.receive_json()


async def test_a_phone_reports_and_the_administrator_reads_it_back_with_the_phone_name(callers):
    sent = await _call(callers.phone, 1, {"type": "portico/connlog/report", "reports": [_report("a"), _report("b")]})
    assert sent["success"] and sent["result"] == {"stored": 2, "duplicates": 0, "rejected": 0}
    again = await _call(callers.phone, 2, {"type": "portico/connlog/report", "reports": [_report("a"), {"v": 3}]})
    assert again["result"] == {"stored": 0, "duplicates": 1, "rejected": 1}
    got = await _call(callers.admin, 1, {"type": "portico/connlog/get"})
    assert got["success"] and got["result"]["total"] == 2
    entry = got["result"]["reports"][0]
    assert entry["phone"] == "Alice's Pixel" and entry["report"]["id"] == "a" and entry["seq"] == 1
    assert "user_id" not in json.dumps(got["result"])


async def test_only_a_phone_may_report_and_only_an_administrator_may_read_or_clear(callers):
    body = {"type": "portico/connlog/report", "reports": [_report("a")]}
    for who in (callers.user, callers.admin):                      # an ordinary login and an administrator who is no phone
        r = await _call(who, 10, body)
        assert not r["success"] and r["error"]["code"] == "unauthorized"
    mid = 20
    for command in ("portico/connlog/get", "portico/connlog/clear"):
        for who in (callers.phone, callers.user):
            mid += 1
            r = await _call(who, mid, {"type": command})
            assert not r["success"] and r["error"]["code"] == "unauthorized", command
    assert (await _call(callers.admin, 12, {"type": "portico/connlog/get"}))["result"]["total"] == 0


async def test_message_limits(callers):
    r = await _call(callers.phone, 1, {"type": "portico/connlog/report", "reports": [_report(f"r{i}") for i in range(51)]})
    assert not r["success"] and r["error"]["code"] == "invalid_format"
    r = await _call(callers.phone, 2, {"type": "portico/connlog/report", "reports": ["not a dict"]})
    assert not r["success"] and r["error"]["code"] == "invalid_format"
    big = _report("big")
    big["ev"] = [{"t": 1, "e": "xy z " * 38} for _ in range(60)]
    r = await _call(callers.phone, 3, {"type": "portico/connlog/report", "reports": [big] * 50})
    assert not r["success"] and r["error"]["code"] == "too_large"


async def test_the_rate_limit_answers_rate_limited(hass, callers, monkeypatch):
    monkeypatch.setattr(connlog, "RATE_MAX_REPORTS", 3)
    ok = await _call(callers.phone, 1, {"type": "portico/connlog/report", "reports": [_report("a"), _report("b")]})
    assert ok["success"]
    r = await _call(callers.phone, 2, {"type": "portico/connlog/report", "reports": [_report("c"), _report("d")]})
    assert not r["success"] and r["error"]["code"] == "rate_limited"


async def test_get_paging_and_clear(callers):
    await _call(callers.phone, 1, {"type": "portico/connlog/report", "reports": [_report(f"r{i}") for i in range(5)]})
    page = await _call(callers.admin, 1, {"type": "portico/connlog/get", "since_seq": 2, "limit": 2})
    assert [e["seq"] for e in page["result"]["reports"]] == [3, 4] and page["result"]["next_seq"] == 4
    bad = await _call(callers.admin, 2, {"type": "portico/connlog/get", "limit": 501})
    assert not bad["success"] and bad["error"]["code"] == "invalid_format"
    cleared = await _call(callers.admin, 3, {"type": "portico/connlog/clear"})
    assert cleared["result"] == {"removed": 5}
    assert (await _call(callers.admin, 4, {"type": "portico/connlog/get"}))["result"]["total"] == 0


def test_the_commands_are_in_the_authorization_table():
    assert auth.COMMANDS["portico/connlog/report"] == frozenset({auth.PHONE})
    assert auth.COMMANDS["portico/connlog/get"] == frozenset({auth.ADMIN})
    assert auth.COMMANDS["portico/connlog/clear"] == frozenset({auth.ADMIN})


# ---- the Journal lines (kind "journal"): `live` and `audio` lines of the app's Journal ----

LINE_240 = "live " + "ab " * 77 + "abcd"      # spaces keep it from being a long token: the size is what is tested
LIVE_START = "live start cam=gate_i…(4) size=big stream=main ttff=840ms"
AUDIO_START = "audio start answer call=gso-18…(31) up=pcmu/8000/20ms down=pcmu/8000/20ms sent=100 updrop=0 recv=99 downdrop=0 mic=low"


def _journal(jid: str = "j1", lines=None, **extra) -> dict:
    lines = [LIVE_START, AUDIO_START] if lines is None else lines
    return {"kind": "journal", "id": jid, "app": "0.6.16-alpha",
            "lines": [{"t": 1_761_000_000_000 + i * 1000, "line": text} for i, text in enumerate(lines)], **extra}


# every real line shape the app writes (PushJournalFormat) passes
@pytest.mark.parametrize("line", [
    LIVE_START, AUDIO_START,
    "live fail cam=gate_i…(4) size=big why=connect retry=1 next=yes",
    "live stop cam=gate_i…(4) size=small route=rtsp stream=gate_intercom_sub why=hidden secs=12",
    "live thermal paused status=severe", "live thermal resumed status=light",
    "live fault at=sync err=IllegalStateException",
    "audio end answer call=gso-18…(31) up=pcmu/8000/20ms down=? sent=100 updrop=0 recv=99 downdrop=0 mic=low muted=yes why=remote_hangup",
    "live", "audio x",
    LINE_240,                                                       # exactly 240 characters
])
def test_the_apps_real_live_and_audio_lines_are_accepted(line):
    assert len(line) <= 240 and connlog.journal_line_ok(line)


# the strict table: the first word allow-list, the character set, the size, and every secret shape
@pytest.mark.parametrize("line", [
    "livex start", "Live start", "LIVE start", " live start", "pushx ring", "Push ring", "WS event=ringing", "unknown ring door=ab…(3)", "ring door=ab…(3)",
    "12:00:00.123 live start cam=x…(1)",                            # the app strips the time of day: the first word must be a known word
    "08:48:23.990 push ring door=01J9EN…(26)",
    "", LINE_240 + "x",                                             # 241 characters
    "live start\ncam=x", "live start\tcam", "live\x00start", "live start\x7f", "live start\x1b[31m",
    "live start caf\u00e9", "live start \u0645", "live start \U0001F600", "live start\u202egnp", "live start\u00a0cam", "live start\u2028x",
    "live start url=https://ha.example.net/x", "live start url=rtsp://10.6.0.6:8554/gate", "audio start ws://x", "live x://y",
    "live start user@host", "audio start a@b",
    "live start auth=Bearer abc", "audio start BEARER abc", "live start key=tskey-auth-abc", "live start TSKEY-x",
    "live start host=10.6.0.6", "live start ip=192.168.1.20", "live start v6=fe80:0:0:0:1", "live start mac=aa:bb:cc:dd:ee:ff", "live start x=::1",
    "live start host=ha.example.net", "audio start host=gate.ts.net",
    "live start id=" + "a1" * 12, "live start id=" + "0123456789abcdef" * 2,
    "live start blob=" + "A" * 40, "audio start blob=" + "Zm9vYmFy" * 6, "live start t=" + "a-b_c+d/e=" * 5,
    "live start token=abc", "live start Password: x", "live start secret=1", "live start api_key=1", "live start authorization=x", "live start cookie=1",
])
def test_every_other_line_is_refused(line):
    assert not connlog.journal_line_ok(line)


@pytest.mark.parametrize("bad", [None, 5, 1.5, [], {}, b"live start", ("live",)])
def test_a_line_that_is_not_a_string_is_refused(bad):
    assert not connlog.journal_line_ok(bad)


def test_a_journal_item_is_cut_to_fifty_lines_and_counts_every_drop():
    item, dropped = connlog.sanitize_journal(_journal(lines=["live start cam=x…(1)"] * 60))
    assert len(item["lines"]) == 50 and dropped == 10
    item, dropped = connlog.sanitize_journal(_journal(lines=["live ok"] * 3 + ["bogus x"] + [LINE_240 + "x"]))
    assert [x["line"] for x in item["lines"]] == ["live ok"] * 3 and dropped == 2
    assert connlog.sanitize_journal(_journal(lines=["bogus x", "other y"])) == (None, 2)       # nothing survived: no item
    assert connlog.sanitize_journal(_journal(lines=[])) == (None, 0)


def test_a_journal_line_needs_an_integer_time_and_a_string():
    raw = _journal(lines=["live a"])
    raw["lines"] += [{"t": True, "line": "live b"}, {"t": "5", "line": "live c"}, {"t": 1.5, "line": "live d"}, {"t": -1, "line": "live e"},
                     {"t": 10**14, "line": "live f"}, {"line": "live g"}, {"t": 5}, "live h", None, 7, {"t": 0, "line": "live zero"}]
    item, dropped = connlog.sanitize_journal(raw)
    assert [x["line"] for x in item["lines"]] == ["live a", "live zero"] and dropped == 10


@pytest.mark.parametrize("bad", [None, "x", [], {}, {"id": "a"}, {"id": "a", "lines": "live x"}, {"id": "a", "lines": {"t": 1}},
                                 {"id": "", "lines": [{"t": 1, "line": "live"}]}, {"id": "has space", "lines": [{"t": 1, "line": "live"}]},
                                 {"id": "x" * 65, "lines": [{"t": 1, "line": "live"}]}, {"id": 7, "lines": [{"t": 1, "line": "live"}]}])
def test_a_journal_item_with_no_usable_id_or_lines_is_refused(bad):
    assert connlog.sanitize_journal(bad)[0] is None


def test_a_journal_item_can_name_neither_a_phone_nor_a_user_and_the_app_is_checked():
    item, _ = connlog.sanitize_journal(_journal(phone="Other phone", user_id="u9", ssid="Home", extra={"x": 1}))
    assert set(item) == {"id", "app", "lines"}
    assert connlog.sanitize_journal(_journal(app="0.6.16-alpha"))[0]["app"] == "0.6.16-alpha"
    for app in ("", "x" * 33, "a b", "https://x", None, 5, "v\n1"):
        assert connlog.sanitize_journal(_journal(app=app))[0]["app"] == ""


# ---- golden: a connection report is handled exactly as before ----

def test_a_connection_report_ignores_the_new_fields_and_a_kind_of_connection_is_the_same_report():
    base = connlog.sanitize_report(_report())
    assert connlog.sanitize_report(_report(kind="connection")) == base
    assert connlog.sanitize_report(_report(kind="other", lines=[{"t": 1, "line": "live x"}])) == base
    assert connlog.sanitize_report(_report(kind=["journal"])) == base           # only the string "journal" selects the other kind


async def test_a_connection_only_message_gets_the_old_reply_and_the_old_get_shape(hass):
    s = await _store(hass)
    assert s.add([_report("a")], user_id="u", phone="P") == {"stored": 1, "duplicates": 0, "rejected": 0}
    assert set(s.get(0, 10)) == {"reports", "next_seq", "total", "bytes"}         # no `journal` key until a line is kept
    assert s._data().keys() == {"next_seq", "entries"}                            # and the saved file is the old shape


# ---- the store: the Journal ring ----

async def test_journal_items_are_stored_counted_and_deduplicated_apart_from_connection_reports(hass):
    s = await _store(hass)
    r = s.add([_report("a"), _journal("j1"), _journal("j1"), _journal("j2", lines=["bogus x"]), {"kind": "journal", "id": "j3"}],
              user_id="u", phone="Pixel")
    assert r == {"stored": 2, "duplicates": 1, "rejected": 2, "journal": {"lines": 2, "dropped": 1}}
    got = s.get(0, 10)
    assert [e["report"]["id"] for e in got["reports"]] == ["a"] and got["total"] == 1            # the connection list holds only the report
    j = got["journal"]
    assert j["total"] == 1 and j["lines"] == 2 and j["dropped"] == 1 and j["next_seq"] == 1
    assert j["entries"] == [{"seq": 1, "received_ms": 1_800_000_000_000, "phone": "Pixel", "app": "0.6.16-alpha", "id": "j1",
                             "lines": [{"t": 1_761_000_000_000, "line": LIVE_START}, {"t": 1_761_000_001_000, "line": AUDIO_START}]}]
    assert "user_id" not in json.dumps(got)


async def test_chatty_journal_lines_never_evict_connection_reports_and_the_reverse(hass, monkeypatch):
    monkeypatch.setattr(connlog, "MAX_REPORTS", 3)
    monkeypatch.setattr(connlog, "MAX_JOURNAL_LINES", 10)
    s = await _store(hass)
    s.add([_report(f"c{i}") for i in range(3)], user_id="u", phone="P")
    for i in range(20):
        s.add([_journal(f"j{i}", lines=["live x"] * 5)], user_id="u", phone="P")
    assert [e["report"]["id"] for e in s._kept] == ["c0", "c1", "c2"]                            # untouched by 100 journal lines
    assert [e["id"] for e in s._journal] == ["j18", "j19"] and s._journal_lines == 10
    s.add([_report(f"d{i}") for i in range(5)], user_id="u", phone="P")
    assert [e["report"]["id"] for e in s._kept] == ["d2", "d3", "d4"]
    assert [e["id"] for e in s._journal] == ["j18", "j19"]                                       # and the reverse
    assert "j0" not in s._journal_ids and "j19" in s._journal_ids                                 # an evicted id may come back


async def test_the_journal_ring_is_bounded_by_age_and_bytes(hass, monkeypatch):
    clock = _Clock()
    s = await _store(hass, clock)
    s.add([_journal("old")], user_id="u", phone="P")
    clock.t += connlog.MAX_AGE_MS + 1
    s.add([_journal("new")], user_id="u", phone="P")
    assert [e["id"] for e in s._journal] == ["new"]
    monkeypatch.setattr(connlog, "MAX_JOURNAL_BYTES", 600)
    s2 = await _store(hass)
    s2.add([_journal(f"b{i}") for i in range(10)], user_id="u", phone="P")
    assert s2._journal_bytes <= 600 and 0 < len(s2._journal) < 10
    assert s2._journal_bytes == sum(e["size"] for e in s2._journal) and s2._journal_lines == 2 * len(s2._journal)


async def test_the_journal_ring_survives_a_restart_and_a_garbled_one_costs_only_the_ring(hass, hass_storage):
    s = await _store(hass)
    s.add([_report("a"), _journal("j1"), _journal("j2", lines=["bogus x", "live y"])], user_id="u", phone="P")
    await s._store.async_save(s._data())
    again = await _store(hass)
    assert [e["id"] for e in again._journal] == ["j1", "j2"] and again._journal_dropped == 1 and again._journal_lines == 3
    assert again.add([_journal("j1"), _journal("j3")], user_id="u", phone="P")["duplicates"] == 1
    assert again.get(0, 10)["journal"]["entries"][-1]["seq"] == 3                                  # the sequence continues
    assert [e["report"]["id"] for e in again._kept] == ["a"]
    hass_storage[connlog.STORE_KEY] = {"version": 1, "key": connlog.STORE_KEY, "data": {
        "entries": [], "journal": ["x", {"seq": "no"}, {"seq": 1, "id": "k", "lines": "no"}, 5], "journal_dropped": -4}}
    broken = await _store(hass)
    assert broken._journal == [] and broken._journal_dropped == 0
    hass_storage[connlog.STORE_KEY] = {"version": 1, "key": connlog.STORE_KEY, "data": {"entries": [], "journal": "nope"}}
    assert (await _store(hass))._journal == []


async def test_get_filters_by_kind_phone_and_time_and_pages_the_journal(hass):
    clock = _Clock()
    s = await _store(hass, clock)
    s.add([_report("a")], user_id="u1", phone="Pixel")
    s.add([_journal("j1")], user_id="u1", phone="Pixel")
    clock.t += 5000
    s.add([_report("b"), _journal("j2", lines=["live a"]), _journal("j3", lines=["live b"])], user_id="u2", phone="Galaxy")
    ids = lambda r: [e["report"]["id"] for e in r["reports"]]
    jids = lambda r: [e["id"] for e in r["journal"]["entries"]]

    both = s.get(0, 10)
    assert ids(both) == ["a", "b"] and jids(both) == ["j1", "j2", "j3"]
    only_c = s.get(0, 10, kind="connection")
    assert ids(only_c) == ["a", "b"] and "journal" not in only_c and set(only_c) == {"reports", "next_seq", "total", "bytes"}
    only_j = s.get(0, 10, kind="journal")
    assert only_j["reports"] == [] and jids(only_j) == ["j1", "j2", "j3"] and only_j["next_seq"] == 0
    assert ids(s.get(0, 10, phone="Galaxy")) == ["b"] and jids(s.get(0, 10, phone="Galaxy")) == ["j2", "j3"]
    assert ids(s.get(0, 10, phone="Nobody")) == [] and jids(s.get(0, 10, phone="Nobody")) == []
    late = s.get(0, 10, since_ms=clock.t)
    assert ids(late) == ["b"] and jids(late) == ["j2", "j3"]
    assert jids(s.get(0, 10, since_ms=clock.t + 1)) == [] and jids(s.get(0, 10, kind="journal", phone="Pixel", since_ms=1)) == ["j1"]
    page = s.get(0, 2, kind="journal")
    assert jids(page) == ["j1", "j2"] and page["journal"]["next_seq"] == 2
    assert jids(s.get(0, 2, kind="journal", journal_since_seq=2)) == ["j3"]
    assert s.get(0, 10, kind="journal", journal_since_seq=3)["journal"]["next_seq"] == 3
    empty = (await _store(hass)).get(0, 10, kind="journal")
    assert empty["journal"] == {"entries": [], "next_seq": 0, "total": 0, "lines": 0, "bytes": 0, "dropped": 0}


async def test_the_rate_limit_counts_journal_lines_apart_from_reports(hass):
    clock = _Clock()
    s = await _store(hass, clock)
    assert s.rate_ok("u1", 1, connlog.RATE_MAX_LINES)
    assert not s.rate_ok("u1", 1, 1)                       # lines used up
    assert s.rate_ok("u1", 1, 0)                           # reports have room; a refused call counted nothing
    assert s.rate_ok("u2", 1, 1)
    clock.t += connlog.RATE_WINDOW_MS + 1
    assert s.rate_ok("u1", 1, 5)


async def test_clear_journal_empties_the_ring_and_its_count(hass):
    s = await _store(hass)
    s.add([_report("a"), _journal("j1"), _journal("j2", lines=["bogus x"])], user_id="u", phone="P")
    assert s.clear_journal() == 1 and s.get(0, 10)["total"] == 1
    assert s.get(0, 10, kind="journal")["journal"]["dropped"] == 0 and s.get(0, 10)["reports"][0]["report"]["id"] == "a"
    assert s.add([_journal("j1")], user_id="u", phone="P")["stored"] == 1                  # an id forgotten by clear can come back


# ---- over the socket ----

async def test_a_phone_reports_journal_lines_and_the_administrator_reads_them_back(callers):
    sent = await _call(callers.phone, 1, {"type": "portico/connlog/report", "reports": [_report("a"), _journal("j1")]})
    assert sent["success"] and sent["result"] == {"stored": 2, "duplicates": 0, "rejected": 0, "journal": {"lines": 2, "dropped": 0}}
    got = await _call(callers.admin, 1, {"type": "portico/connlog/get"})
    entry = got["result"]["journal"]["entries"][0]
    assert entry["phone"] == "Alice's Pixel" and entry["app"] == "0.6.16-alpha" and entry["lines"][0]["line"] == LIVE_START
    assert entry["received_ms"] > 0 and got["result"]["total"] == 1
    only = await _call(callers.admin, 2, {"type": "portico/connlog/get", "kind": "journal", "phone": "Alice's Pixel"})
    assert only["result"]["reports"] == [] and len(only["result"]["journal"]["entries"]) == 1
    none = await _call(callers.admin, 3, {"type": "portico/connlog/get", "kind": "connection"})
    assert "journal" not in none["result"] and len(none["result"]["reports"]) == 1


async def test_a_phone_can_only_report_for_itself_and_is_told_nothing_but_counts(callers):
    hostile = _journal("j1", phone="Somebody else", user_id="someone", name="x")
    hostile["lines"][0]["phone"] = "Somebody else"
    r = await _call(callers.phone, 1, {"type": "portico/connlog/report", "reports": [hostile]})
    assert set(r["result"]) == {"stored", "duplicates", "rejected", "journal"}
    assert (await _call(callers.admin, 1, {"type": "portico/connlog/get", "kind": "journal"}))["result"]["journal"]["entries"][0]["phone"] == "Alice's Pixel"
    body = {"type": "portico/connlog/report", "reports": [_journal("j2")]}
    for who in (callers.user, callers.admin):                   # an ordinary login and an administrator who is no phone
        denied = await _call(who, 9, body)
        assert not denied["success"] and denied["error"]["code"] == "unauthorized"
    for who in (callers.phone, callers.user):                   # and no one but an administrator reads or filters
        denied = await _call(who, 10, {"type": "portico/connlog/get", "kind": "journal"})
        assert not denied["success"] and denied["error"]["code"] == "unauthorized"
    total = (await _call(callers.admin, 11, {"type": "portico/connlog/get", "kind": "journal"}))["result"]["journal"]
    assert total["total"] == 1


async def test_hostile_lines_are_dropped_and_counted_and_never_stored(callers):
    lines = [
        LIVE_START,
        "live start url=https://ha.example.net/api?x=1", "live x token=abc", "audio y " + "A" * 60,
        "live " + "ab " * 3000, "live start\x00\x1b[2Jevil", "live start caf\u00e9 \U0001F600", "live a\nlive b",
        "bogus secret", "live start ip=10.6.0.6", "live k=tskey-auth-1234",
    ]
    r = await _call(callers.phone, 1, {"type": "portico/connlog/report", "reports": [_journal("hostile", lines=lines)]})
    assert r["success"] and r["result"]["stored"] == 1 and r["result"]["journal"] == {"lines": 1, "dropped": len(lines) - 1}
    j = (await _call(callers.admin, 1, {"type": "portico/connlog/get"}))["result"]["journal"]
    assert [x["line"] for x in j["entries"][0]["lines"]] == [LIVE_START] and j["dropped"] == len(lines) - 1
    allbad = await _call(callers.phone, 2, {"type": "portico/connlog/report", "reports": [_journal("allbad", lines=["bogus x", "other y"])]})
    assert allbad["result"] == {"stored": 0, "duplicates": 0, "rejected": 1, "journal": {"lines": 0, "dropped": 2}}
    j2 = (await _call(callers.admin, 2, {"type": "portico/connlog/get"}))["result"]["journal"]
    assert j2["total"] == 1 and j2["dropped"] == len(lines) + 1


async def test_journal_message_limits_and_the_line_rate_limit(callers, monkeypatch):
    r = await _call(callers.phone, 1, {"type": "portico/connlog/report", "reports": [_journal(f"j{i}") for i in range(51)]})
    assert not r["success"] and r["error"]["code"] == "invalid_format"
    too_big = _journal("big", lines=["live " + "x" * 235] * 50)
    r = await _call(callers.phone, 2, {"type": "portico/connlog/report", "reports": [too_big] * 11})
    assert not r["success"] and r["error"]["code"] == "too_large"
    monkeypatch.setattr(connlog, "RATE_MAX_LINES", 60)
    ok = await _call(callers.phone, 3, {"type": "portico/connlog/report", "reports": [_journal("a", lines=["live a"] * 50)]})
    assert ok["success"]
    limited = await _call(callers.phone, 4, {"type": "portico/connlog/report", "reports": [_journal("b", lines=["live a"] * 11)]})
    assert not limited["success"] and limited["error"]["code"] == "rate_limited"
    still = await _call(callers.phone, 5, {"type": "portico/connlog/report", "reports": [_journal("c", lines=["live a"] * 10), _report("r")]})
    assert still["success"]


async def test_get_filter_arguments_are_checked_and_clear_empties_both(callers):
    await _call(callers.phone, 1, {"type": "portico/connlog/report", "reports": [_report("a"), _journal("j1")]})
    for mid, bad in enumerate(({"kind": "other"}, {"kind": 1}, {"since_ms": -1}, {"since_ms": "x"}, {"phone": ""}, {"phone": "x" * 61},
                               {"journal_since_seq": -1}), start=20):
        r = await _call(callers.admin, mid, {"type": "portico/connlog/get", **bad})
        assert not r["success"] and r["error"]["code"] == "invalid_format", bad
    cleared = await _call(callers.admin, 30, {"type": "portico/connlog/clear"})
    assert cleared["result"] == {"removed": 1, "removed_journal": 1}
    after = (await _call(callers.admin, 31, {"type": "portico/connlog/get"}))["result"]
    assert after["total"] == 0 and "journal" not in after
    assert (await _call(callers.admin, 32, {"type": "portico/connlog/clear"}))["result"] == {"removed": 0}


# ---- DEV-762 (H1): the ring-decision words ----

# the line shapes of PushJournalFormatTest / HouseRingCoordinatorTest / HouseDndTest, with the time of day the app strips
RING_LINES = [
    "push ring door=01J9EN…(26) call=gso-17…(33) seq=1 t=08:48:23.830 age=160ms known=1 until=08:49:23.990 fg=bg fsi=yes chan=ok/4 notif=on dnd=off -> Ring",
    "push test t=08:48:23.830 age=160ms known=1 fg=bg fsi=yes chan=ok/4 notif=on dnd=off -> Quiet",
    "push ignored(unknown_version) -> Silent(ignored)",
    "push dropped kind=ring why=paused", "push dropped kind=? why=?",
    "ws ring door=01J9EN…(26) call=gso-17…(33) seq=1 t=08:48:23.830 age=160ms known=1 -> Ring",
    "ws event=ringing door=01J9EN…(26) call=gso-17…(33)", "ws event=idle door=01J9EN…(26)",
    "ui Ring door=01J9EN…(26) call=gso-17…(33) post=ring+fsi notif=ok ringchan=ok fsi=yes",
    "ui Dismiss door=01J9EN…(26) call=gso-17…(33) post=answered_elsewhere stop=yes",
    "ui Ring door=01J9EN…(26) call=gso-17…(33) not-alerted why=ring_switch_off",
    "ui stop why=decline door=01J9EN…(26) call=gso-17…(33) stopped",
    "hold switched door=01J9EN…(26) call=gso-17…(33) ms=30000", "hold resume-refused call=gso-17…(33) result=not_held",
    "hold focus-lost n=2", "hold held-ended door=01J9EN…(26) call=gso-17…(33) result=remote_hangup ms=0",
    "house ring call=hso-17…(10)", "house answer call=hso-17…(10)", "house over call=hso-17…(10) result=answered_here", "house dnd n=1",
    "frames call=hso-17…(10) first=120ms n=25", "net connect via=direct", "layout tile cols=2 rows=3",
]


@pytest.mark.parametrize("line", RING_LINES)
def test_the_apps_real_ring_decision_lines_are_accepted(line):
    assert len(line) <= 240 and connlog.journal_line_ok(line)


def test_every_ring_word_is_accepted_and_the_set_is_exactly_the_designs():
    assert connlog.JOURNAL_WORDS == {"live", "audio", "push", "ws", "ui", "hold", "house", "frames", "net", "layout", "wall"}


# the same secret shapes the strict table uses, now behind each new word: the widened list opens no door
@pytest.mark.parametrize("word", ["push", "ws", "ui", "hold", "house", "frames", "net", "layout"])
@pytest.mark.parametrize("tail", [
    "url=https://ha.example.net/x", "x=rtsp://10.6.0.6:8554/gate", "who=user@host", "auth=Bearer abc", "key=tskey-auth-abc",
    "host=10.6.0.6", "v6=fe80:0:0:0:1", "mac=aa:bb:cc:dd:ee:ff", "host=ha.example.net", "id=" + "a1" * 12, "blob=" + "A" * 40,
    "token=abc", "Password: x", "api_key=1", "cookie=1", "caf\u00e9", "\u202e", "a\nb",
])
def test_secret_looking_text_is_still_refused_behind_every_new_word(word, tail):
    assert not connlog.journal_line_ok(f"{word} event=ringing {tail}")


def test_ring_lines_are_stored_apart_from_connection_reports_and_hostile_ones_are_counted():
    # the validator, through sanitize_journal: a mixed item keeps the ring lines, drops the hostile one
    item = _journal("j9", lines=[RING_LINES[0], "push ring url=https://x.example.net/a", RING_LINES[8]])
    out, dropped = connlog.sanitize_journal(item)
    assert dropped == 1 and [e["line"] for e in out["lines"]] == [RING_LINES[0], RING_LINES[8]]


# ---- DEV-762 (H1): crash and exit records ----

def _crash(cid: str = "c1", **extra) -> dict:
    return {"kind": "crash", "id": cid, "app": "0.6.19-alpha", "sdk": 35, "t": 1_761_000_000_000, "up": 8123,
            "model": "Pixel 9", "rel": "16", "thread": "main",
            "chain": ["java.lang.IllegalStateException", "java.lang.NullPointerException"],
            "frames": ["com.meda.portico.call.RingScreen.onCreate:88", "com.meda.portico.App.<init>:12"], **extra}


def _exit(eid: str = "e1", **extra) -> dict:
    return {"kind": "exit", "id": eid, "app": "0.6.19-alpha", "sdk": 35, "t": 1_761_000_100_000, "up": 99, "age": 123456,
            "model": "Pixel 9", "rel": "16", "reason": "crash_native", "importance": "foreground", "phase": "startup", **extra}


def test_a_good_crash_and_a_good_exit_pass_unchanged():
    for rec in (_crash(), _exit(), _exit("e2", trace=["panic: nil pointer dereference", "main.run(0x1234)", "runtime.gopark(0x0)"])):
        assert connlog.sanitize_diag(rec) == rec


def test_the_crash_and_exit_key_sets_are_closed_and_a_message_has_no_way_in():
    out = connlog.sanitize_diag(_crash(message="failed to open rtsp://10.6.0.6/x", user="alice", phone="P", extra={"a": 1}))
    assert set(out) == {"kind", "id", "t", "app", "sdk", "up", "model", "rel", "thread", "chain", "frames"}
    out = connlog.sanitize_diag(_exit(frames=["a.b:1"], chain=["a.b"], thread="main"))
    assert not {"frames", "chain", "thread"} & set(out)
    assert "age" not in connlog.sanitize_diag(_crash(age=5))


@pytest.mark.parametrize("bad", [
    None, "x", [], {}, {"kind": "crash"}, {"kind": "journal", "id": "a", "t": 1}, {"kind": "crash", "id": "a", "t": 1},                 # nothing to say
    {"kind": "crash", "id": "has space", "t": 1, "frames": ["a.b:1"]}, {"kind": "crash", "id": "a", "t": True, "frames": ["a.b:1"]},
    {"kind": "crash", "id": "a", "t": -1, "frames": ["a.b:1"]}, {"kind": "crash", "id": "a", "t": "1", "frames": ["a.b:1"]},
    {"kind": "exit", "id": "a", "t": 1}, {"kind": "exit", "id": "a", "t": 1, "reason": "has\nnewline"}, {"kind": "exit", "id": "a", "t": 1, "reason": ""},
    {"kind": "exit", "id": "a", "t": 1, "reason": "x" * 65},
])
def test_what_says_nothing_or_is_malformed_is_refused(bad):
    assert connlog.sanitize_diag(bad) is None


@pytest.mark.parametrize("value", [
    "https://ha.example.net/x", "rtsp://10.6.0.6/x", "user@host", "Bearer abc", "tskey-auth-abc", "10.6.0.6", "fe80:0:0:0:1",
    "aa:bb:cc:dd:ee:ff", "ha.example.net", "gate.ts.net", "a" * 24 + "0f", "A" * 40, "token=abc", "Password: x", "caf\u00e9", "a b\x00c", "x/y",
])
def test_secret_shaped_values_are_dropped_from_every_field_of_both_kinds(value):
    crash = connlog.sanitize_diag(_crash(thread=value, model=value, rel=value, chain=[value, "a.B"], frames=[value, "a.B.c:1"]))
    assert crash["chain"] == ["a.B"] and crash["frames"] == ["a.B.c:1"]
    assert not {"thread", "model", "rel"} & set(crash)
    assert connlog.sanitize_diag(_exit(reason=value)) is None
    ex = connlog.sanitize_diag(_exit(importance=value, phase=value, model=value, trace=[value, "main.run(0x1)"]))
    assert ex["trace"] == ["main.run(0x1)"] and not {"importance", "phase", "model"} & set(ex)
    assert value not in json.dumps([crash, ex])


def test_frames_are_capped_and_a_non_list_is_empty():
    out = connlog.sanitize_diag(_crash(frames=[f"a.B.c{i}:{i}" for i in range(40)], chain=[f"a.B{i}" for i in range(20)]))
    assert len(out["frames"]) == 12 and len(out["chain"]) == 8
    assert connlog.sanitize_diag(_crash(frames="a.B.c:1", chain=["a.B"]))["frames"] == []
    out = connlog.sanitize_diag(_exit(trace=[f"main.f{i}(0x1)" for i in range(50)]))
    assert len(out["trace"]) == 30
    assert connlog.sanitize_diag(_exit(trace="panic"))["reason"] == "crash_native"


def test_an_oversize_record_is_refused(monkeypatch):
    monkeypatch.setattr(connlog, "MAX_DIAG_ITEM_BYTES", 1000)
    assert connlog.sanitize_diag(_exit(trace=["main.run " + "ab " * 30] * 30)) is None
    assert connlog.sanitize_diag(_exit(trace=["main.run " + "ab " * 30] * 3)) is not None


async def test_crash_and_exit_are_stored_in_their_own_ring_counted_and_deduplicated(hass):
    s = await _store(hass)
    r = s.add([_report("a"), _journal("j1"), _crash("c1"), _crash("c1"), _exit("e1"), {"kind": "crash", "id": "c2"}], user_id="u", phone="Pixel")
    assert r == {"stored": 4, "duplicates": 1, "rejected": 1, "journal": {"lines": 2, "dropped": 0},
                 "diag": {"records": 2, "merged": 0, "dropped": 1, "rate_limited": 0}}
    got = s.get(0, 10)
    assert got["total"] == 1 and got["journal"]["total"] == 1                       # the other two rings hold only their own
    d = got["diag"]
    assert d["total"] == 2 and d["next_seq"] == 2 and d["dropped"] == 1
    assert [e["record"]["kind"] for e in d["entries"]] == ["crash", "exit"]
    assert d["entries"][0] == {"seq": 1, "received_ms": 1_800_000_000_000, "last_ms": 1_800_000_000_000, "count": 1, "phone": "Pixel",
                               "id": "c1", "record": _crash("c1")}
    assert "user_id" not in json.dumps(got)


async def test_the_same_signature_within_an_hour_is_stored_once_with_a_count(hass):
    clock = _Clock()
    s = await _store(hass, clock)
    r = s.add([_crash("c1"), _crash("c2"), _crash("c3", frames=["a.B.other:1"])], user_id="u", phone="P")
    assert r["stored"] == 3 and r["diag"] == {"records": 2, "merged": 1, "dropped": 0, "rate_limited": 0}
    e = s.get(0, 10, kind="crash")["diag"]["entries"]
    assert [x["count"] for x in e] == [2, 1]
    assert s.add([_crash("c2")], user_id="u", phone="P")["duplicates"] == 1         # a merged id is still a known id
    clock.t += 3600 * 1000 + 1                                                      # an hour later the same signature is a new entry
    assert s.add([_crash("c9")], user_id="u", phone="P")["diag"]["records"] == 1
    assert s.add([_crash("c10")], user_id="v", phone="P")["diag"]["records"] == 1   # and another user's is never merged


async def test_diag_is_rate_limited_per_user_by_dropping_the_excess_not_by_an_error(hass):
    s = await _store(hass)
    items = [_crash(f"c{i}", frames=[f"a.B.m{i}:1"]) for i in range(25)]
    r = s.add(items, user_id="u", phone="P")
    assert r["stored"] == 20 and r["rejected"] == 5 and r["diag"]["rate_limited"] == 5
    assert s.add([_crash("z", frames=["a.B.z:1"])], user_id="other", phone="P")["stored"] == 1


async def test_the_diag_ring_is_bounded_by_count_and_bytes_and_age(hass, monkeypatch):
    monkeypatch.setattr(connlog, "MAX_DIAG", 3)
    monkeypatch.setattr(connlog, "RATE_MAX_DIAG", 100)
    s = await _store(hass)
    s.add([_crash(f"c{i}", frames=[f"a.B.m{i}:1"]) for i in range(5)], user_id="u", phone="P")
    assert [e["id"] for e in s.get(0, 10)["diag"]["entries"]] == ["c2", "c3", "c4"]
    monkeypatch.setattr(connlog, "MAX_DIAG_BYTES", 1)
    s.add([_crash("big", frames=["a.B.big:1"])], user_id="u", phone="P")
    assert s._diag == [] and s._diag_bytes == 0


async def test_crash_loop_never_evicts_journal_lines_or_connection_reports(hass, monkeypatch):
    monkeypatch.setattr(connlog, "MAX_DIAG", 2)
    monkeypatch.setattr(connlog, "RATE_MAX_DIAG", 100)
    s = await _store(hass)
    s.add([_report("a"), _journal("j1")], user_id="u", phone="P")
    s.add([_crash(f"c{i}", frames=[f"a.B.m{i}:1"]) for i in range(10)], user_id="u", phone="P")
    got = s.get(0, 10)
    assert got["total"] == 1 and got["journal"]["total"] == 1 and got["diag"]["total"] == 2


async def test_get_kind_crash_exit_diag_and_the_old_kinds(hass):
    s = await _store(hass)
    s.add([_report("a"), _journal("j1"), _crash("c1"), _exit("e1")], user_id="u", phone="P")
    assert [e["record"]["kind"] for e in s.get(0, 10, kind="crash")["diag"]["entries"]] == ["crash"]
    assert [e["record"]["kind"] for e in s.get(0, 10, kind="exit")["diag"]["entries"]] == ["exit"]
    for kind in ("crash", "exit", "diag"):
        r = s.get(0, 10, kind=kind)
        assert r["reports"] == [] and "journal" not in r
    assert len(s.get(0, 10, kind="diag")["diag"]["entries"]) == 2
    assert "diag" not in s.get(0, 10, kind="journal") and "diag" not in s.get(0, 10, kind="connection")
    assert s.get(1, 10, kind="diag", )["diag"]["entries"] == s.get(0, 10, kind="diag")["diag"]["entries"]   # since_seq is the connection cursor
    assert [e["seq"] for e in s.get(0, 10, kind="diag", diag_since_seq=1)["diag"]["entries"]] == [2]
    assert s.get(0, 10, kind="diag", phone="nobody")["diag"]["entries"] == []
    assert s.get(0, 10, kind="diag", since_ms=1_800_000_000_001)["diag"]["entries"] == []


async def test_a_diag_item_is_a_report_to_an_old_reader_and_old_data_is_byte_identical(hass):
    # nothing but connection reports and Journal items: the saved file and the reply are exactly what they were before H1
    s = await _store(hass)
    s.add([_report("a"), _journal("j1")], user_id="u", phone="P")
    assert s._data().keys() == {"next_seq", "entries", "journal", "journal_next_seq", "journal_dropped"}
    assert set(s.get(0, 10)) == {"reports", "next_seq", "total", "bytes", "journal"}
    before = json.dumps(s._data(), sort_keys=True)
    s.add([_crash("c1")], user_id="u", phone="P")
    after = s._data()
    assert after.keys() == {"next_seq", "entries", "journal", "journal_next_seq", "journal_dropped", "diag", "diag_next_seq", "diag_dropped"}
    assert json.dumps({k: after[k] for k in after if not k.startswith("diag")}, sort_keys=True) == before


async def test_a_file_written_before_h1_loads_and_saves_unchanged(hass, hass_storage):
    old = {"next_seq": 2, "entries": [{"seq": 1, "received_ms": 1_800_000_000_000, "user_id": "u", "phone": "P", "size": 10,
                                       "report": {"v": 1, "id": "a"}}]}
    hass_storage[connlog.STORE_KEY] = {"version": 1, "minor_version": 1, "key": connlog.STORE_KEY, "data": json.loads(json.dumps(old))}
    s = await _store(hass)
    assert s._data() == old
    assert s.get(0, 10) == {"reports": [{"seq": 1, "received_ms": 1_800_000_000_000, "phone": "P", "report": {"v": 1, "id": "a"}}],
                            "next_seq": 1, "total": 1, "bytes": 10}


async def test_the_diag_ring_survives_a_restart_and_a_garbled_one_costs_only_the_ring(hass, hass_storage):
    s = await _store(hass)
    s.add([_report("a"), _crash("c1")], user_id="u", phone="P")
    data = json.loads(json.dumps(s._data()))
    hass_storage[connlog.STORE_KEY] = {"version": 1, "minor_version": 1, "key": connlog.STORE_KEY, "data": data}
    again = connlog.ConnLogStore(hass)
    await again.async_load()
    assert again.get(0, 10)["diag"]["entries"][0]["id"] == "c1" and again.get(0, 10)["total"] == 1
    data["diag"] = "garbage"
    again = connlog.ConnLogStore(hass)
    await again.async_load()
    hass_storage[connlog.STORE_KEY]["data"] = data
    again = connlog.ConnLogStore(hass)
    await again.async_load()
    assert again.get(0, 10)["total"] == 1 and "diag" not in again.get(0, 10)


async def test_clear_empties_the_diag_ring_and_says_so(hass):
    s = await _store(hass)
    s.add([_crash("c1")], user_id="u", phone="P")
    assert s.clear_diag() == 1 and s.get(0, 10).get("diag", {"total": 0})["total"] == 0


async def test_a_phone_reports_a_crash_and_the_administrator_reads_it_back(callers):
    phone, admin = callers.phone, callers.admin
    r = await _call(phone, 1, {"type": "portico/connlog/report", "reports": [_crash("c1"), _exit("e1")]})
    assert r["success"] and r["result"]["stored"] == 2 and r["result"]["diag"]["records"] == 2
    got = (await _call(admin, 2, {"type": "portico/connlog/get", "kind": "crash"}))["result"]
    assert got["reports"] == [] and [e["id"] for e in got["diag"]["entries"]] == ["c1"]
    got = (await _call(admin, 3, {"type": "portico/connlog/get", "kind": "diag", "diag_since_seq": 1}))["result"]
    assert [e["id"] for e in got["diag"]["entries"]] == ["e1"]
    bad = await _call(admin, 4, {"type": "portico/connlog/get", "kind": "nope"})
    assert not bad["success"] and bad["error"]["code"] == "invalid_format"
    cleared = (await _call(admin, 5, {"type": "portico/connlog/clear"}))["result"]
    assert cleared["removed_diag"] == 2


async def test_only_a_phone_may_report_a_crash(callers):
    for ws in (callers.admin, callers.user):
        r = await _call(ws, 1, {"type": "portico/connlog/report", "reports": [_crash("c1")]})
        assert not r["success"]


def test_journal_ring_keeps_a_day_of_two_phones_and_the_rate_limit_is_unchanged():
    """DEV-764 (D-20261117-f567): the ring holds >= 24 h at ~600 lines/day/phone with a wide margin; the per-user rate stays."""
    assert connlog.MAX_JOURNAL_LINES >= 20000 and connlog.MAX_JOURNAL_BYTES >= 4 * 1024 * 1024
    assert connlog.RATE_MAX_LINES == 3000
