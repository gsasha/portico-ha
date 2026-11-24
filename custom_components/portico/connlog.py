"""The connection log (DEV-705): the phone sends one report per connection sequence; this keeps them for the owner.

`portico_connlog_design.md` (estate repo, automation/) is the design. In short: the Portico app assembles a small JSON
report for each connection sequence (a trigger such as a network change, to its resolution: connected, no network, gave
up, superseded) with the timings of every step, and sends the queued reports over its authenticated socket. This module
stores them, bounded, in a private Store, and lets an administrator read them back.

  portico/connlog/report {reports: [...]}        phone only. At most 50 reports and 128 KB per message. Replies
                                                 {stored, duplicates, rejected}. The sender identity is the authenticated
                                                 HA user, never a field of the message.
  portico/connlog/get {since_seq?, limit?,       administrator only. The entries after a cursor (at most 500), the
                       kind?, phone?, since_ms?,  next cursor, the store's size; the Journal entries too, apart, under
                       journal_since_seq?}        `journal` (see the end of this docstring). The filters are additive.
  portico/connlog/clear {}                       administrator only. Empties the store.

What is stored is only what `sanitize_report` lets through: known keys, numbers, booleans, short strings with control
characters removed, bounded depth and length, nothing that looks like a URL or a long token. The phone is trusted to
send no identifying field (no SSID, address, URL or token: the design's section 3), and this module does not rely on
that: it is a second filter, and a report that still does not fit (4 KB) is rejected.

Bounds: 30 days, 3000 reports, 4 MB of stored report text, 600 reports per user per ten minutes. The Store is saved
with a delay, so a burst is one write.

A second kind of report rides the same command (DEV-713 / DEV-705): the app's `live` and `audio` Journal lines, so the owner
can read live-video and call-audio results without the user copying them. An item of `reports` with `"kind": "journal"` is
`{kind, id, app?, lines: [{t, line}]}` (at most 50 lines of at most 240 characters); an item with no `kind` is a connection
report, handled exactly as before. Lines go to their own bounded ring (`journal`), never into the connection list, so chatty
live lines cannot evict connection reports. `sanitize_journal` keeps only lines whose first word is `live` or `audio`, whose
characters are printable ASCII (plus the app's own U+2026 marker from `Redact.prefix`), and that do not look like a URL, an
address, a credential or a token; every other line is dropped and counted (`dropped`, kept in the Store). The sender is the
authenticated phone, never a field of the item, and nothing is ever returned to the phone but the counts.

DEV-762 (H1): the Journal word list also takes the ring-decision words (`push`, `ws`, `ui`, `hold`, `house`, `frames`, `net`,
`layout`); the line filter is unchanged. Two more item kinds ride the same command, `"kind": "crash"` and `"kind": "exit"`
(`sanitize_diag`: a closed key set, closed string shapes, no exception message, no URL or token), kept in a third ring, `diag`
(200 records, 256 KB, 30 days), at most 20 per user per ten minutes (the excess is dropped and counted, not an error), the
same signature within an hour stored once with a count. `portico/connlog/get` takes `kind` `crash`, `exit` or `diag` and
`diag_since_seq`, and answers an additive `diag` block once the ring holds anything. An older app, and every stored record
of the other kinds, are unchanged.

Nothing here logs a report body, a Journal line or a crash record.
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from . import auth
from .push.registration import enrolled_phone_name

_LOGGER = logging.getLogger(__name__)

STORE_KEY = "portico.connlog"
DATA_KEY = "portico_connlog"

MAX_AGE_MS = 30 * 24 * 3600 * 1000
MAX_REPORTS = 3000
MAX_BYTES = 4 * 1024 * 1024
MAX_REPORT_BYTES = 4096
MAX_MESSAGE_REPORTS = 50
MAX_MESSAGE_BYTES = 128 * 1024
MAX_GET = 500
RATE_WINDOW_MS = 10 * 60 * 1000
RATE_MAX_REPORTS = 600
SAVE_DELAY_S = 5

# The Journal ring (kind "journal"): its own caps, so chatty live lines never evict a connection report (and the reverse).
MAX_JOURNAL_LINES = 20000         # kept lines, all phones together (DEV-764: >= 24 h of two phones, D-20261117-f567)
MAX_JOURNAL_BYTES = 4 * 1024 * 1024   # stored in .storage/portico.connlog; a burst is one delayed (5 s) atomic rewrite
MAX_ITEM_LINES = 50               # per report item; the rest of a longer list is dropped and counted
MAX_LINE_CHARS = 240
RATE_MAX_LINES = 3000             # Journal lines per user per RATE_WINDOW_MS (an item also counts as one report)
# DEV-762 (H1): the ring-decision words the app writes beside the live/audio ones (PushJournalFormat: `via()` gives `push` or `ws`).
# The line filter (characters, size, `_SECRETS`) is unchanged; the words only choose which lines are looked at.
JOURNAL_WORDS = frozenset({"live", "audio", "push", "ws", "ui", "hold", "house", "frames", "net", "layout", "wall"})

# DEV-762 (H1): the `diag` ring, crash and exit records. Apart from the Journal ring and the connection list, so a crash
# loop cannot evict either (and the reverse).
DIAG_KINDS = frozenset({"crash", "exit"})
MAX_DIAG = 200                    # kept records, all phones together
MAX_DIAG_BYTES = 256 * 1024
MAX_DIAG_ITEM_BYTES = 8192        # one record, as stored
RATE_MAX_DIAG = 20                # diag records per user per RATE_WINDOW_MS; the excess is dropped and counted, not an error
DIAG_MERGE_MS = 3600 * 1000       # the same signature within an hour is stored once, with a count
MAX_FRAMES = 12
MAX_CHAIN = 8
MAX_TRACE = 30
MAX_DIAG_STR = 240

# The report's top-level keys (design section 3). Anything else is dropped.
TOP_KEYS = frozenset({
    "v", "id", "app", "sdk", "t0", "dur", "result", "trigger", "env", "net", "hint", "ev", "tsnet", "path",
    "retries", "dropped",
})
LIST_CAPS = {"ev": 60, "net": 20, "retries": 20}
MAX_DEPTH = 4
MAX_DICT_KEYS = 24
MAX_STR = 200
MAX_NUM = 10**13

_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
_KEY = re.compile(r"[a-z][a-z0-9_]{0,23}")
_URL = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://")
_LONG_TOKEN = re.compile(r"[A-Za-z0-9_\-=+/]{40,}")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def _clean_str(value: str) -> str:
    text = _CONTROL.sub(" ", value).strip()[:MAX_STR]
    if _URL.search(text) or _LONG_TOKEN.search(text):
        return "<redacted>"
    return text


def _clean(value: Any, depth: int) -> Any:
    """A bounded copy of a JSON-ish value, or None when it is not representable (dropped by the caller)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return max(-MAX_NUM, min(MAX_NUM, value))
    if isinstance(value, float):
        return None if value != value or value in (float("inf"), float("-inf")) else int(max(-MAX_NUM, min(MAX_NUM, value)))
    if isinstance(value, str):
        return _clean_str(value)
    if depth >= MAX_DEPTH:
        return None
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if len(out) >= MAX_DICT_KEYS:
                break
            if isinstance(key, str) and _KEY.fullmatch(key):
                cleaned = _clean(item, depth + 1)
                if cleaned is not None:
                    out[key] = cleaned
        return out
    if isinstance(value, list):
        items = [_clean(item, depth + 1) for item in value[:MAX_DICT_KEYS * 3]]
        return [item for item in items if item is not None]
    return None


def sanitize_report(raw: Any) -> dict | None:
    """The report as it will be stored, or None when it is not acceptable (not a v1 report, no usable id, too big)."""
    version = raw.get("v") if isinstance(raw, dict) else None
    if type(version) is not int or version != 1:   # not `True`, which equals 1 in Python
        return None
    report_id = raw.get("id")
    if not isinstance(report_id, str) or not _ID.fullmatch(report_id):
        return None
    out: dict[str, Any] = {}
    for key in TOP_KEYS:
        if key not in raw:
            continue
        cleaned = _clean(raw[key], 1)
        if cleaned is None:
            continue
        cap = LIST_CAPS.get(key)
        if cap is not None:
            if not isinstance(cleaned, list):
                continue
            cleaned = cleaned[:cap]
        out[key] = cleaned
    out["v"] = 1
    out["id"] = report_id
    if len(json.dumps(out, separators=(",", ":"))) > MAX_REPORT_BYTES:
        return None
    return out


_APP = re.compile(r"[A-Za-z0-9._+-]{1,32}")
# A Journal line is printable ASCII, plus U+2026: the app's `Redact.prefix` writes `call=gso-18…(31)` for every shortened id.
_LINE_CHARS = re.compile(r"[\x20-\x7e…]+")
# Defence in depth: the app sends names and numbers only, so a line that looks like any of these is dropped whole.
_SECRETS = (
    re.compile(r"://|@|::|tskey-|bearer", re.IGNORECASE),                               # URL, mail/user@host, IPv6 gap, key, token
    re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b"),                                         # IPv4
    re.compile(r"(?:[0-9A-Fa-f]{1,4}:){3,}"),                                           # IPv6 / MAC
    re.compile(r"[A-Za-z0-9-]+\.(?:[A-Za-z0-9-]+\.)*[A-Za-z]{2,}\b"),                  # a host name
    re.compile(r"[0-9A-Fa-f]{24,}"),                                                    # a long hex run
    re.compile(r"[A-Za-z0-9+/_=-]{40,}"),                                               # a long base64 / token run
    re.compile(r"(?:token|passw(?:or)?d|secret|api_?key|authorization|cookie)\s*[=:]", re.IGNORECASE),
)


def journal_line_ok(text: Any) -> bool:
    """Whether one Journal line may be kept: a string of 1..240 allowed characters, first word `live` or `audio`, nothing secret-shaped."""
    if not isinstance(text, str) or not 0 < len(text) <= MAX_LINE_CHARS or not _LINE_CHARS.fullmatch(text):
        return False
    if text.split(" ", 1)[0] not in JOURNAL_WORDS:
        return False
    return not any(p.search(text) for p in _SECRETS)


def sanitize_journal(raw: Any) -> tuple[dict | None, int]:
    """`(item, dropped)` for one `kind: journal` report item: the kept `{id, app, lines}` (None when it is not acceptable: no
    usable id, `lines` not a list, or no line survived), and how many lines were dropped. Every field but `id`, `app` and
    `lines` is ignored, so a sender can never name a phone or a user."""
    if not isinstance(raw, dict):
        return None, 0
    item_id = raw.get("id")
    lines = raw.get("lines")
    if not isinstance(item_id, str) or not _ID.fullmatch(item_id) or not isinstance(lines, list):
        return None, 0
    app = raw.get("app")
    kept: list[dict[str, Any]] = []
    for entry in lines[:MAX_ITEM_LINES]:
        t = entry.get("t") if isinstance(entry, dict) else None
        if type(t) is int and 0 <= t <= MAX_NUM and journal_line_ok(entry.get("line")):
            kept.append({"t": t, "line": entry["line"]})
    dropped = len(lines) - len(kept)
    if not kept:
        return None, dropped
    return {"id": item_id, "app": app if isinstance(app, str) and _APP.fullmatch(app) else "", "lines": kept}, dropped


# ---- DEV-762: crash and exit records ----
# A closed key set per kind; every string has a closed shape. The app sends no exception message and no free text, so
# nothing here needs to guess: what does not fit its shape is dropped (a scalar) or the record is refused (a required field).
_WORD = re.compile(r"[A-Za-z0-9_.$:()<> -]{1,64}")                           # model, release, thread, reason, importance, phase
_JAVA_NAME = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]{0,59}(?:\.[A-Za-z_$<][A-Za-z0-9_$>]{0,59}){0,12}")   # a class chain entry
_FRAME = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]{0,59}(?:\.[A-Za-z_$<][A-Za-z0-9_$>]{0,59}){1,12}(?::\d{1,6})?")   # Class.method:line
_TRACE_CHARS = re.compile(r"[A-Za-z0-9_.$:()<> -]+")
# A dotted name whose last label is one of these looks like a host name, whatever else it is. (`_SECRETS` flags every dotted
# name, which would drop every Java frame and Go function, so the host rule is a TLD list here; the app filters first.)
_HOSTISH = re.compile(r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.(?:com|net|org|io|dev|app|local|lan|home|internal|arpa|ts|il|ru|edu|gov|co|me|info|xyz|cloud|ai|us|uk|de|fr)(?![\w.$])", re.IGNORECASE)
_SECRETS_NO_HOST = tuple(p for p in _SECRETS if p.pattern != r"[A-Za-z0-9-]+\.(?:[A-Za-z0-9-]+\.)*[A-Za-z]{2,}\b")


def _diag_text_ok(text: Any, limit: int = MAX_DIAG_STR) -> bool:
    if not isinstance(text, str) or not 0 < len(text) <= limit:
        return False
    return not _HOSTISH.search(text) and not any(p.search(text) for p in _SECRETS_NO_HOST)


def _diag_word(raw: Any) -> str | None:
    return raw if isinstance(raw, str) and _WORD.fullmatch(raw) and _diag_text_ok(raw, 64) else None


def _diag_list(raw: Any, shape: re.Pattern, cap: int) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [x for x in raw[:cap] if isinstance(x, str) and shape.fullmatch(x) and _diag_text_ok(x)]


def sanitize_diag(raw: Any) -> dict | None:
    """The stored `{kind, id, app, t, ...}` for one `kind: crash` or `kind: exit` item, or None when it is not acceptable
    (not a dict, a bad kind or id, no usable time, nothing that says what happened, or too big). Unknown keys are ignored.
    crash: `thread`, `chain` (class names, at most 8), `frames` (`Class.method:line`, at most 12); exit: `reason`
    (required), `importance`, `phase`, `age`, `trace` (a Go panic tail, at most 30 lines of 240). Both: `app`, `sdk`, `t`
    (ms since the epoch, required), `up` (ms since process start), `model`, `rel`."""
    if not isinstance(raw, dict) or raw.get("kind") not in DIAG_KINDS:
        return None
    item_id = raw.get("id")
    t = raw.get("t")
    if not isinstance(item_id, str) or not _ID.fullmatch(item_id) or type(t) is not int or not 0 <= t <= MAX_NUM:
        return None
    kind = raw["kind"]
    out: dict[str, Any] = {"kind": kind, "id": item_id, "t": t}
    app = raw.get("app")
    out["app"] = app if isinstance(app, str) and _APP.fullmatch(app) else ""
    for key in ("sdk", "up", "age"):
        value = raw.get(key)
        if type(value) is int and 0 <= value <= MAX_NUM and (key != "age" or kind == "exit"):
            out[key] = value
    for key in ("model", "rel"):
        word = _diag_word(raw.get(key))
        if word is not None:
            out[key] = word
    if kind == "crash":
        thread = _diag_word(raw.get("thread"))
        if thread is not None:
            out["thread"] = thread
        chain = _diag_list(raw.get("chain"), _JAVA_NAME, MAX_CHAIN)
        frames = _diag_list(raw.get("frames"), _FRAME, MAX_FRAMES)
        if not chain and not frames:
            return None
        out["chain"], out["frames"] = chain, frames
    else:
        reason = _diag_word(raw.get("reason"))
        if reason is None:
            return None
        out["reason"] = reason
        for key in ("importance", "phase"):
            word = _diag_word(raw.get(key))
            if word is not None:
                out[key] = word
        trace = raw.get("trace")
        if isinstance(trace, list):
            kept = [x for x in trace[:MAX_TRACE] if isinstance(x, str) and _TRACE_CHARS.fullmatch(x) and _diag_text_ok(x)]
            if kept:
                out["trace"] = kept
    if len(json.dumps(out, separators=(",", ":"))) > MAX_DIAG_ITEM_BYTES:
        return None
    return out


def _diag_signature(record: dict) -> tuple:
    if record["kind"] == "crash":
        return ("crash", (record["chain"] or [""])[0], (record["frames"] or [""])[0], record["app"])
    return ("exit", record["reason"], record.get("phase", ""), record.get("importance", ""), record["app"])


class ConnLogStore:
    """The kept reports. Entries: {seq, received_ms, user_id, phone, size, report}. The Journal ring is apart:
    `_journal` entries {seq, received_ms, user_id, phone, app, id, size, lines: [{t, line}]}, its own sequence, caps and ids."""

    def __init__(self, hass: HomeAssistant, *, now_ms=None) -> None:
        self._hass = hass
        self._now = now_ms or (lambda: int(time.time() * 1000))
        self._store: Store[dict] = Store(hass, 1, STORE_KEY, private=True, atomic_writes=True)
        self._kept: list[dict[str, Any]] = []
        self._ids: set[str] = set()
        self._next_seq = 1
        self._bytes = 0
        self._recent: dict[str, list[int]] = {}
        self._recent_lines: dict[str, list[tuple[int, int]]] = {}
        self._journal: list[dict[str, Any]] = []
        self._journal_ids: set[str] = set()
        self._journal_next_seq = 1
        self._journal_bytes = 0
        self._journal_lines = 0
        self._journal_dropped = 0
        self._diag: list[dict[str, Any]] = []
        self._diag_ids: set[str] = set()
        self._diag_next_seq = 1
        self._diag_bytes = 0
        self._diag_dropped = 0
        self._recent_diag: dict[str, list[int]] = {}

    async def async_load(self) -> None:
        data = await self._store.async_load() or {}
        entries = [e for e in data.get("entries", []) if isinstance(e, dict) and isinstance(e.get("seq"), int)]
        self._kept = sorted(entries, key=lambda e: e["seq"])
        self._ids = {e["report"]["id"] for e in self._kept if isinstance(e.get("report"), dict) and "id" in e["report"]}
        self._bytes = sum(int(e.get("size", 0)) for e in self._kept)
        self._next_seq = max(int(data.get("next_seq", 1)), (self._kept[-1]["seq"] + 1) if self._kept else 1)
        self._prune()
        journal = [e for e in data.get("journal", []) if isinstance(e, dict) and isinstance(e.get("seq"), int)
                   and isinstance(e.get("id"), str) and isinstance(e.get("lines"), list)] if isinstance(data.get("journal"), list) else []
        self._journal = sorted(journal, key=lambda e: e["seq"])
        self._journal_ids = {e["id"] for e in self._journal}
        self._journal_bytes = sum(int(e.get("size", 0)) for e in self._journal)
        self._journal_lines = sum(len(e["lines"]) for e in self._journal)
        self._journal_next_seq = max(int(data.get("journal_next_seq", 1)), (self._journal[-1]["seq"] + 1) if self._journal else 1)
        dropped = data.get("journal_dropped", 0)
        self._journal_dropped = dropped if type(dropped) is int and dropped >= 0 else 0
        self._prune_journal()
        diag = [e for e in data.get("diag", []) if isinstance(e, dict) and isinstance(e.get("seq"), int)
                and isinstance(e.get("id"), str) and isinstance(e.get("record"), dict)] if isinstance(data.get("diag"), list) else []
        self._diag = sorted(diag, key=lambda e: e["seq"])
        self._diag_ids = {e["id"] for e in self._diag}
        self._diag_bytes = sum(int(e.get("size", 0)) for e in self._diag)
        self._diag_next_seq = max(int(data.get("diag_next_seq", 1)), (self._diag[-1]["seq"] + 1) if self._diag else 1)
        ddropped = data.get("diag_dropped", 0)
        self._diag_dropped = ddropped if type(ddropped) is int and ddropped >= 0 else 0
        self._prune_diag()

    def _data(self) -> dict:
        data: dict[str, Any] = {"next_seq": self._next_seq, "entries": self._kept}
        if self._journal or self._journal_dropped or self._journal_next_seq > 1:
            data.update(journal=self._journal, journal_next_seq=self._journal_next_seq, journal_dropped=self._journal_dropped)
        if self._diag or self._diag_dropped or self._diag_next_seq > 1:      # absent until the first record: old files stay byte-identical
            data.update(diag=self._diag, diag_next_seq=self._diag_next_seq, diag_dropped=self._diag_dropped)
        return data

    def _save(self) -> None:
        self._store.async_delay_save(self._data, SAVE_DELAY_S)

    def _prune(self) -> bool:
        cutoff = self._now() - MAX_AGE_MS
        changed = False
        while self._kept and (
            self._kept[0].get("received_ms", 0) < cutoff
            or len(self._kept) > MAX_REPORTS
            or self._bytes > MAX_BYTES
        ):
            dropped = self._kept.pop(0)
            self._bytes -= int(dropped.get("size", 0))
            rid = (dropped.get("report") or {}).get("id")
            self._ids.discard(rid)
            changed = True
        return changed

    def _prune_journal(self) -> bool:
        cutoff = self._now() - MAX_AGE_MS
        changed = False
        while self._journal and (
            self._journal[0].get("received_ms", 0) < cutoff
            or self._journal_lines > MAX_JOURNAL_LINES
            or self._journal_bytes > MAX_JOURNAL_BYTES
        ):
            dropped = self._journal.pop(0)
            self._journal_bytes -= int(dropped.get("size", 0))
            self._journal_lines -= len(dropped["lines"])
            self._journal_ids.discard(dropped["id"])
            changed = True
        return changed

    def _prune_diag(self) -> bool:
        cutoff = self._now() - MAX_AGE_MS
        changed = False
        while self._diag and (
            self._diag[0].get("received_ms", 0) < cutoff
            or len(self._diag) > MAX_DIAG
            or self._diag_bytes > MAX_DIAG_BYTES
        ):
            dropped = self._diag.pop(0)
            self._diag_bytes -= int(dropped.get("size", 0))
            self._diag_ids.discard(dropped["id"])
            changed = True
        return changed

    def _rate_diag_ok(self, user_id: str) -> bool:
        now = self._now()
        recent = [t for t in self._recent_diag.get(user_id, []) if now - t < RATE_WINDOW_MS]
        self._recent_diag[user_id] = recent
        if len(recent) >= RATE_MAX_DIAG:
            return False
        recent.append(now)
        return True

    def _add_diag(self, raw: dict, user_id: str, phone: str, now: int, tally: dict[str, int]) -> str:
        """One crash/exit item: "stored", "merged", "duplicate" or "rejected"."""
        item_id = raw.get("id")
        if isinstance(item_id, str) and item_id in self._diag_ids:
            return "duplicate"
        record = sanitize_diag(raw)
        if record is None:
            tally["dropped"] += 1
            return "rejected"
        if not self._rate_diag_ok(user_id):
            tally["rate_limited"] += 1
            return "rejected"
        sig = _diag_signature(record)
        for entry in reversed(self._diag):
            if entry["user_id"] == user_id and now - entry.get("last_ms", entry["received_ms"]) < DIAG_MERGE_MS \
                    and _diag_signature(entry["record"]) == sig:
                entry["count"] = entry.get("count", 1) + 1
                entry["last_ms"] = now
                self._diag_ids.add(record["id"])
                tally["merged"] += 1
                return "merged"
        size = len(json.dumps(record, separators=(",", ":")))
        self._diag.append({"seq": self._diag_next_seq, "received_ms": now, "last_ms": now, "count": 1, "user_id": user_id,
                           "phone": phone[:60], "id": record["id"], "size": size, "record": record})
        self._diag_next_seq += 1
        self._diag_ids.add(record["id"])
        self._diag_bytes += size
        tally["records"] += 1
        return "stored"

    def rate_ok(self, user_id: str, n: int, lines: int = 0) -> bool:
        """Count `n` reports (and `lines` Journal lines) for `user_id`; False, counting nothing, when either would exceed the window's limit."""
        now = self._now()
        recent = [t for t in self._recent.get(user_id, []) if now - t < RATE_WINDOW_MS]
        recent_lines = [(t, k) for t, k in self._recent_lines.get(user_id, []) if now - t < RATE_WINDOW_MS]
        self._recent[user_id] = recent
        self._recent_lines[user_id] = recent_lines
        if len(recent) + n > RATE_MAX_REPORTS or sum(k for _, k in recent_lines) + lines > RATE_MAX_LINES:
            return False
        recent.extend([now] * n)
        if lines:
            recent_lines.append((now, lines))
        return True

    def add(self, reports: list[Any], *, user_id: str, phone: str) -> dict[str, Any]:
        stored = duplicates = rejected = 0
        journal_seen = False
        journal_lines = journal_dropped = 0
        diag_seen = False
        tally = {"records": 0, "merged": 0, "dropped": 0, "rate_limited": 0}
        now = self._now()
        for raw in reports:
            if isinstance(raw, dict) and raw.get("kind") in DIAG_KINDS:
                diag_seen = True
                outcome = self._add_diag(raw, user_id, phone, now, tally)
                if outcome in ("stored", "merged"):
                    stored += 1
                elif outcome == "duplicate":
                    duplicates += 1
                else:
                    rejected += 1
                continue
            if isinstance(raw, dict) and raw.get("kind") == "journal":
                journal_seen = True
                item_id = raw.get("id")
                if isinstance(item_id, str) and item_id in self._journal_ids:
                    duplicates += 1
                    continue
                item, dropped = sanitize_journal(raw)
                journal_dropped += dropped
                if item is None:
                    rejected += 1
                    continue
                size = len(json.dumps(item["lines"], separators=(",", ":")))
                self._journal.append({
                    "seq": self._journal_next_seq, "received_ms": now, "user_id": user_id, "phone": phone[:60],
                    "app": item["app"], "id": item["id"], "size": size, "lines": item["lines"],
                })
                self._journal_next_seq += 1
                self._journal_ids.add(item["id"])
                self._journal_bytes += size
                self._journal_lines += len(item["lines"])
                journal_lines += len(item["lines"])
                stored += 1
                continue
            report = sanitize_report(raw)
            if report is None:
                rejected += 1
                continue
            if report["id"] in self._ids:
                duplicates += 1
                continue
            size = len(json.dumps(report, separators=(",", ":")))
            self._kept.append({
                "seq": self._next_seq, "received_ms": now, "user_id": user_id, "phone": phone[:60],
                "size": size, "report": report,
            })
            self._next_seq += 1
            self._ids.add(report["id"])
            self._bytes += size
            stored += 1
        if stored > 0 or journal_dropped or tally["dropped"] or tally["rate_limited"]:
            self._prune()
            self._prune_journal()
            self._prune_diag()
            self._journal_dropped += journal_dropped
            self._diag_dropped += tally["dropped"] + tally["rate_limited"]
            self._save()
        result: dict[str, Any] = {"stored": stored, "duplicates": duplicates, "rejected": rejected}
        if journal_seen:      # only a message that carried Journal items hears about them: an old-style reply is unchanged
            result["journal"] = {"lines": journal_lines, "dropped": journal_dropped}
        if diag_seen:         # likewise: only a message that carried crash/exit items hears about them
            result["diag"] = dict(tally)
        return result

    def get(self, since_seq: int, limit: int, *, kind: str | None = None, phone: str | None = None,
            since_ms: int | None = None, journal_since_seq: int = 0,
            diag_since_seq: int = 0) -> dict[str, Any]:
        """The connection reports after `since_seq` (as ever), and the Journal entries after `journal_since_seq` under
        `journal` (present when asked for with `kind: journal`, or when the ring is not empty). `kind: connection` is the old
        reply exactly; `kind: journal` leaves `reports` empty. `diag` (DEV-762) is the crash/exit ring, apart like `journal`: present
        when the ring holds anything (and `kind` is not `connection` or `journal`), or when asked for with `kind` `diag`, `crash`
        or `exit` (those leave `reports` and `journal` out). `phone` and `since_ms` (received at or after) narrow both."""
        def wanted(e: dict[str, Any]) -> bool:
            return (phone is None or e.get("phone") == phone) and (since_ms is None or e.get("received_ms", 0) >= since_ms)

        diag_only = kind in ("diag", "crash", "exit")
        items = [e for e in self._kept if e["seq"] > since_seq and wanted(e)][:limit] if kind not in ("journal", "diag", "crash", "exit") else []
        next_seq = items[-1]["seq"] if items else since_seq
        reply: dict[str, Any] = {
            "reports": [{k: e[k] for k in ("seq", "received_ms", "phone", "report")} for e in items],
            "next_seq": next_seq,
            "total": len(self._kept),
            "bytes": self._bytes,
        }
        if kind != "connection" and not diag_only and (kind == "journal" or self._journal or self._journal_dropped):
            entries = [e for e in self._journal if e["seq"] > journal_since_seq and wanted(e)][:limit]
            reply["journal"] = {
                "entries": [{"seq": e["seq"], "received_ms": e["received_ms"], "phone": e["phone"], "app": e.get("app", ""),
                             "id": e["id"], "lines": e["lines"]} for e in entries],
                "next_seq": entries[-1]["seq"] if entries else journal_since_seq,
                "total": len(self._journal),
                "lines": self._journal_lines,
                "bytes": self._journal_bytes,
                "dropped": self._journal_dropped,
            }
        if kind not in ("connection", "journal") and (diag_only or self._diag or self._diag_dropped):
            entries = [e for e in self._diag if e["seq"] > diag_since_seq and wanted(e)
                       and (kind not in ("crash", "exit") or e["record"]["kind"] == kind)][:limit]
            reply["diag"] = {
                "entries": [{"seq": e["seq"], "received_ms": e["received_ms"], "last_ms": e.get("last_ms", e["received_ms"]),
                             "count": e.get("count", 1), "phone": e["phone"], "id": e["id"], "record": e["record"]} for e in entries],
                "next_seq": entries[-1]["seq"] if entries else diag_since_seq,
                "total": len(self._diag),
                "bytes": self._diag_bytes,
                "dropped": self._diag_dropped,
            }
        return reply

    def clear(self) -> int:
        """Empties the connection reports (the Journal ring is `clear_journal`); returns how many."""
        removed = len(self._kept)
        self._kept = []
        self._ids = set()
        self._bytes = 0
        self._save()
        return removed

    def clear_journal(self) -> int:
        """Empties the Journal ring and its dropped count; returns how many entries (batches) were removed."""
        removed = len(self._journal)
        self._journal = []
        self._journal_ids = set()
        self._journal_bytes = 0
        self._journal_lines = 0
        self._journal_dropped = 0
        self._save()
        return removed


    def clear_diag(self) -> int:
        """Empties the crash/exit ring and its dropped count; returns how many entries were removed."""
        removed = len(self._diag)
        self._diag = []
        self._diag_ids = set()
        self._diag_bytes = 0
        self._diag_dropped = 0
        self._save()
        return removed


async def get_connlog_store(hass: HomeAssistant) -> ConnLogStore:
    store = hass.data.get(DATA_KEY)
    if store is None:
        store = ConnLogStore(hass)
        await store.async_load()
        hass.data[DATA_KEY] = store
    return store


@websocket_api.websocket_command({
    vol.Required("type"): "portico/connlog/report",
    vol.Required("reports"): vol.All([dict], vol.Length(max=MAX_MESSAGE_REPORTS)),
})
@websocket_api.async_response
async def handle_connlog_report(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    if not auth.require(connection, msg, "portico/connlog/report"):
        return
    reports = msg["reports"]
    if len(json.dumps(reports, separators=(",", ":"))) > MAX_MESSAGE_BYTES:
        connection.send_error(msg["id"], "too_large", "send fewer reports at a time")
        return
    user = connection.user
    user_id = user.id if user else ""
    store = await get_connlog_store(hass)
    lines = sum(min(len(r["lines"]), MAX_ITEM_LINES) for r in reports if r.get("kind") == "journal" and isinstance(r.get("lines"), list))
    if not store.rate_ok(user_id, len(reports), lines):
        connection.send_error(msg["id"], "rate_limited", "too many reports; try again later")
        return
    phone = enrolled_phone_name(hass, user_id) or "unknown"
    connection.send_result(msg["id"], store.add(reports, user_id=user_id, phone=phone))


@websocket_api.websocket_command({
    vol.Required("type"): "portico/connlog/get",
    vol.Optional("since_seq", default=0): vol.All(int, vol.Range(min=0)),
    vol.Optional("limit", default=200): vol.All(int, vol.Range(min=1, max=MAX_GET)),
    vol.Optional("kind"): vol.In(["journal", "connection", "crash", "exit", "diag"]),
    vol.Optional("phone"): vol.All(str, vol.Length(min=1, max=60)),
    vol.Optional("since_ms"): vol.All(int, vol.Range(min=0)),
    vol.Optional("journal_since_seq", default=0): vol.All(int, vol.Range(min=0)),
    vol.Optional("diag_since_seq", default=0): vol.All(int, vol.Range(min=0)),
})
@websocket_api.async_response
async def handle_connlog_get(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    if not auth.require(connection, msg, "portico/connlog/get"):
        return
    store = await get_connlog_store(hass)
    connection.send_result(msg["id"], store.get(
        msg["since_seq"], msg["limit"], kind=msg.get("kind"), phone=msg.get("phone"), since_ms=msg.get("since_ms"),
        journal_since_seq=msg["journal_since_seq"], diag_since_seq=msg["diag_since_seq"]))


@websocket_api.websocket_command({vol.Required("type"): "portico/connlog/clear"})
@websocket_api.async_response
async def handle_connlog_clear(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    if not auth.require(connection, msg, "portico/connlog/clear"):
        return
    store = await get_connlog_store(hass)
    result = {"removed": store.clear()}
    removed_journal = store.clear_journal()
    if removed_journal:
        result["removed_journal"] = removed_journal
    removed_diag = store.clear_diag()
    if removed_diag:
        result["removed_diag"] = removed_diag
    connection.send_result(msg["id"], result)


def async_setup_connlog_commands(hass: HomeAssistant) -> None:
    websocket_api.async_register_command(hass, handle_connlog_report)
    websocket_api.async_register_command(hass, handle_connlog_get)
    websocket_api.async_register_command(hass, handle_connlog_clear)
