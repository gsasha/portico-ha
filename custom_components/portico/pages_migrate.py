"""The Pages migration: schema 4/5 (template + overrides + rules) to schema 6 (library + devices), Pages unified P-A
(design `portico_pages_unify_design.md` 3.1 and 3.2; the owner's decisions D-20261109-4c5c, D-20261110).

Three pure pieces and one orchestration:

- `migrate_to_v6(document, endpoints=, elements=)`: the document as schema 6. Every device's EFFECTIVE result is written into its
  own lists (its `pages` = `merged_pages`, its `ring` = the rule that wins per caller, written as a Ring page of the caller's
  Talk element), then the template and the rules disappear. Identical pages and identical rules are stored once.
- `prove_equality(...)`: `resolve_endpoint(migrated) == resolve_endpoint(original)`, `rev` included, for every device and every
  caller. The apply refuses unless every one is equal.
- `export_to_v5(document6, ...)`: the best-effort way back (the `.v4`/`.v5` safety copy is the guaranteed one).
- `plan_for_store` / `run_migration` and the admin command `portico/content/pages_migrate` (dry run, then apply).

Talk elements. A Ring page belongs to a Talk element (the party that rings: a door, a phone, a screen). Doors have theirs
(ELEM-10). Every enrolled phone and every screen has a MANAGED one (id `talk-phone-<endpoint>` / `talk-screen-<endpoint>`),
created by the Devices work's reconcile (`managed_talk.py`). `TalkPlan` finds the element
that stands for a caller, or plans the one that would be created; `ensure_talk_elements` is the ONE thin create-if-missing
interface, stubbed here (see its docstring for the integration point).
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import Any

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback

from . import auth
from .content import (
    MAX_LABEL, ProfileError, SaveConflict, _fresh_id, get_content_store,
)
from .content_elements import REF_KEY, expand_action, expand_document, is_ref
from .content_resolve import (
    ROSTER_ALL, block_actions, endpoint_table, merged_pages, page_actions, resolve_endpoint, resolve_rule, ring_call_actions,
)
from .endpoints import KIND_DASHBOARD, KIND_DOOR, KIND_PHONE, Endpoint
from .pages_model import load_pages_model, normalize_device, normalize_library_page
from .pages_resolve import (
    KIND_CONTENT, KIND_RING, PARTY_DOOR, PARTY_PHONE, PARTY_SCREEN, SCHEMA_PAGES, WHEN_CALL, WHEN_RING, device_pages,
    is_pages_model, is_talk_id, library_by_id, page_kind, parties_of, party_matches, roster_of,
)

_LOGGER = logging.getLogger(__name__)

# Rule caller categories in the report ("rules converted by caller kind").
CALLER_DOOR, CALLER_PHONE, CALLER_SCREEN, CALLER_KIND, CALLER_ORPHAN = "door", "phone", "screen", "kind", "orphan"


# ---- Talk elements: who stands for a caller ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TalkNeed:
    """The Talk element standing for one party: an existing one, or the one a create-if-missing would make."""
    kind: str                   # PARTY_DOOR | PARTY_PHONE | PARTY_SCREEN
    party: str                  # the door's device id, or the phone's / screen's endpoint id
    name: str                   # what the element is called (the party's name)
    element_id: str             # its id: real when `exists`, else the id it is planned to have
    exists: bool
    options: dict[str, str] = field(default_factory=dict)      # {door|phone|screen: party}, what a create sends

    def as_row(self) -> dict[str, Any]:
        return {"id": self.element_id, "kind": self.kind, "party": self.party, "name": self.name,
                "options": dict(self.options), "managed": self.kind != PARTY_DOOR,
                "provisional_id": self.kind == PARTY_DOOR}


def party_of_endpoint(endpoint: Endpoint) -> tuple[str, str] | None:
    if endpoint.kind == KIND_DOOR:
        return (PARTY_DOOR, endpoint.device_id) if endpoint.device_id else None
    if endpoint.kind == KIND_PHONE:
        return PARTY_PHONE, endpoint.id
    if endpoint.kind == KIND_DASHBOARD:
        return PARTY_SCREEN, endpoint.id
    return None


def planned_talk_id(kind: str, key: str) -> str:
    """The id a not-yet-existing Talk element is planned to have. A phone's and a screen's is the MANAGED id the Devices worker's
    reconcile gives it (`talk-phone-<endpoint id>`), so the reference written now is valid the moment that element exists. A door's
    is provisional (a hash): the element store mints its own `el-` id on create, and the apply re-plans with the real one."""
    if kind == PARTY_DOOR:
        return "el-" + hashlib.sha256(f"door:{key}".encode()).hexdigest()[:8]
    return f"talk-{kind}-{key}"


class TalkPlan:
    """Finds, for each caller, the Talk element that stands for it, or plans the one a create-if-missing would add."""

    def __init__(self, elements: dict[str, Any]) -> None:
        self._parties = parties_of(elements)
        self._memo: dict[tuple[str, str], TalkNeed | None] = {}
        self.problems: list[str] = []

    def for_endpoint(self, endpoint: Endpoint) -> TalkNeed | None:
        party = party_of_endpoint(endpoint)
        if party is None:
            self.problems.append(f"{endpoint.name or endpoint.id}: cannot have a Talk element")
            return None
        if party in self._memo:
            return self._memo[party]
        kind, key = party
        found = sorted(eid for eid, standing in self._parties.items() if party_matches(standing, endpoint))
        managed = [eid for eid in found if eid == planned_talk_id(kind, endpoint.id)]
        if found:
            need = TalkNeed(kind, key, endpoint.name or key, (managed or found)[0], True, {kind: key})
        else:
            planned = planned_talk_id(kind, endpoint.id if kind != PARTY_DOOR else key)
            if not is_talk_id(planned):
                self.problems.append(f"{endpoint.name or endpoint.id}: its id cannot make a Talk element id")
                need = None
            else:
                need = TalkNeed(kind, key, endpoint.name or key, planned, False, {kind: key})
        self._memo[party] = need
        return need

    def missing(self) -> list[TalkNeed]:
        return [n for n in self._memo.values() if n is not None and not n.exists]

    def planned_parties(self) -> dict[str, tuple[str, str]]:
        """`{planned id: (kind, party)}` of the elements that do not exist yet: the dry run's overlay for the equality proof."""
        return {n.element_id: (n.kind, n.party) for n in self.missing()}


def orphan_talk_id(caller_id: str) -> str:
    """A Talk element id for a rule whose caller no endpoint answers to any more (a removed phone or door). No such element
    exists, so the Ring page made from the rule is kept, shown "Talk element removed" and never rings (design 3.2 line 5)."""
    if ":" in caller_id or len(caller_id) > 100:
        return "el-" + hashlib.sha256(f"orphan:{caller_id}".encode()).hexdigest()[:8]
    safe = "".join(c if c.isalnum() or c in "_-" else "-" for c in caller_id) or "x"
    return f"talk-phone-{safe}"


async def ensure_talk_elements(hass: HomeAssistant, needs: list[TalkNeed]) -> dict[tuple[str, str], str]:
    """THE create-if-missing interface to the Talk elements store. Returns `{(kind, party): element id}` for every need, creating
    the ones that do not exist. Raises `TalkUnavailable` (the apply then refuses; the content store is untouched) for a need it
    cannot create.

    A DOOR's Talk element is created through `ElementStore.async_set` (a `talk` element with `options.door`). A PHONE's or
    SCREEN's is the Devices work's managed element (`managed_talk.py`, id `talk-phone-<endpoint id>` / `talk-screen-<endpoint id>`):
    `managed_talk.async_reconcile` creates every missing one (idempotent), and the planned id (`planned_talk_id`) is exactly the id it
    gives, so the Ring pages written by the migration name it correctly."""
    from . import managed_talk
    from .elements import ElementError, get_elements_store

    store = get_elements_store(hass)
    await store.async_ensure_loaded()
    if any(need.kind != PARTY_DOOR and not need.exists for need in needs):
        await managed_talk.async_reconcile(hass)
    made: dict[tuple[str, str], str] = {}
    for need in needs:
        if need.exists:
            made[(need.kind, need.party)] = need.element_id
            continue
        if need.kind != PARTY_DOOR:
            if need.element_id not in store.elements_by_id():
                raise TalkUnavailable(
                    f"the managed Talk element of {need.kind} {need.name!r} ({need.element_id}) could not be created")
            made[(need.kind, need.party)] = need.element_id
            continue
        name = need.name
        for attempt in range(3):
            try:
                result = await store.async_set({"kind": "talk", "name": name, "options": {"door": need.party}}, store.rev)
            except ElementError as err:
                if err.code == "name_taken" and attempt < 2:
                    name = f"{need.name} talk" if attempt == 0 else f"{need.name} talk {attempt + 1}"
                    continue
                raise TalkUnavailable(f"could not create the Talk element of {need.name!r}: {err.code}") from err
            made[(need.kind, need.party)] = result["element"]["id"]
            break
    return made


class TalkUnavailable(Exception):
    """A Talk element the migration needs cannot be created (yet)."""


# ---- the migration ----------------------------------------------------------------------------------------------------

@dataclass
class Plan:
    document: dict[str, Any]
    talks: TalkPlan
    ring_pages: list[dict[str, Any]] = field(default_factory=list)       # report rows
    dropped: list[dict[str, Any]] = field(default_factory=list)
    unused_rules: list[dict[str, Any]] = field(default_factory=list)
    rules_by_caller_kind: dict[str, int] = field(default_factory=dict)
    assignments: int = 0
    problems: list[str] = field(default_factory=list)
    callees: list[str] = field(default_factory=list)


def _canon(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _copy_form(expanded: dict[str, Any], stored_pages: list[dict[str, Any]], elements: dict[str, Any]) -> dict[str, Any]:
    """The action to put on a Ring page for the (expanded) button a rule names: the STORED form where that resolves to the same
    button without the block's door (an element reference keeps following its element), else the expanded inline action. A button
    whose expansion needs the block's door (a `portico_open` with no target of its own) is written inline, device id included,
    because a Ring page's block has no door."""
    for page in stored_pages:
        for block in page.get("blocks") or ():
            for item in block.get("actions") or ():
                if not isinstance(item, dict) or item.get("id") != expanded["id"]:
                    continue
                if is_ref(item):
                    element = elements.get(item.get(REF_KEY))
                    if expand_action(item, element, block.get("door")) == expanded:
                        if expand_action(item, element, None) == expanded:
                            return copy.deepcopy(item)
                        return copy.deepcopy(expanded)
                elif block_actions({"door": block.get("door"), "actions": [item]})[0] == expanded:
                    return copy.deepcopy(expanded)
    return copy.deepcopy(expanded)


def _find_buttons(ids: list[str], expanded_pages: list[dict[str, Any]], stored_pages: list[dict[str, Any]],
                  elements: dict[str, Any], dropped: list[dict[str, Any]], note: dict[str, Any]) -> list[dict[str, Any]]:
    """`content_resolve._find_actions`, reproduced step for step, returning the Ring page form of each button it finds. A
    button it drops (missing, shared by two different pages, a talk button) is listed in `dropped`: the migration reproduces the
    drop, it cannot copy what is not there."""
    by_id: dict[str, dict[str, Any]] = {}
    clash: set[str] = set()
    for page in expanded_pages:
        for action in page_actions(page):
            if action["id"] in by_id and by_id[action["id"]] != action:
                clash.add(action["id"])
            by_id.setdefault(action["id"], action)
    out: list[dict[str, Any]] = []
    for action_id in ids:
        if action_id in clash:
            dropped.append({**note, "button": action_id, "reason": "two different buttons share this id"})
        elif action_id in by_id and by_id[action_id].get("kind") == "talk":
            dropped.append({**note, "button": action_id, "reason": "a talk button (a ring is not the moment to talk)"})
        elif action_id in by_id:
            out.append(_copy_form(by_id[action_id], stored_pages, elements))
        else:
            dropped.append({**note, "button": action_id, "reason": "the page set no longer has it"})
    return out


def _ring_blocks(ring: list[dict[str, Any]], call: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The blocks of a Ring page that give back exactly the two lists. The same buttons in the same order for both: one block, shown
    always. Otherwise a block for the ringing and a block for the call (a button in both lists is then in both blocks, which a
    Ring page allows across the two audiences)."""
    if ring == call:
        return [{"id": "buttons", "title": "Buttons", "cameras": [], "actions": ring}] if ring else []
    blocks: list[dict[str, Any]] = []
    if ring:
        blocks.append({"id": "ring", "title": "While ringing", "when": WHEN_RING, "cameras": [], "actions": ring})
    if call:
        blocks.append({"id": "call", "title": "During the call", "when": WHEN_CALL, "cameras": [], "actions": call})
    return blocks


def _caller_category(caller: str, endpoints: dict[str, Endpoint]) -> str:
    if caller.startswith("kind:"):
        return CALLER_KIND
    found = endpoints.get(caller)
    if found is None:
        return CALLER_ORPHAN
    return {KIND_DOOR: CALLER_DOOR, KIND_PHONE: CALLER_PHONE, KIND_DASHBOARD: CALLER_SCREEN}.get(found.kind, CALLER_ORPHAN)


def migrate_to_v6(document: dict[str, Any], *, endpoints: list[Endpoint], elements: dict[str, Any]) -> Plan:
    """The schema 4/5 `document` (stored form) as schema 6. Pure: writes nothing, mutates nothing.

    `endpoints` is `endpoint_table(document, registry)` (phones, screens, doors) and `elements` the stored Talk, button and camera
    elements by id. See the module docstring and design 3.2. Never raises for a document it cannot convert: what it cannot do
    goes into `Plan.problems`, which the dry run reports and the apply refuses."""
    if is_pages_model(document):
        raise ProfileError("the document is already schema 6")
    expanded = expand_document(document, elements)
    talks = TalkPlan(elements)
    by_endpoint = {e.id: e for e in endpoints}
    overrides = document.get("overrides") or {}
    callees = [e.id for e in endpoints if e.kind in (KIND_PHONE, KIND_DASHBOARD)]
    callees += [k for k in overrides if k not in callees]            # "Not attached" keys (and a door's) keep their lists
    plan = Plan(document={}, talks=talks, callees=callees)

    # 1 and 2: the library and each device's own page list --------------------------------------------------------------
    content_pages: list[dict[str, Any]] = []
    lib_ids: dict[str, str] = {}
    taken: set[str] = set()
    devices: dict[str, dict[str, Any]] = {}
    # The template's pages come first and keep their own ids (most devices show them); they are kept even when no device lists
    # one yet (nothing is dropped). An override page of the same id and other content then gets a fresh library id.
    for page in (document.get("template") or {}).get("pages") or []:
        signature = _canon([page["id"], page.get("label"), page.get("blocks")])
        if signature not in lib_ids and page["id"] not in taken:
            taken.add(page["id"])
            lib_ids[signature] = page["id"]
            content_pages.append({"id": page["id"], "label": page.get("label") or page["id"], "kind": KIND_CONTENT,
                                  "blocks": copy.deepcopy(page.get("blocks") or [])})
    for key in callees:
        entries = []
        for page in merged_pages(document, key):
            signature = _canon([page["id"], page.get("label"), page.get("blocks")])
            lib_id = lib_ids.get(signature)
            if lib_id is None:
                lib_id = page["id"] if page["id"] not in taken else _fresh_id(page["id"], taken)
                taken.add(lib_id)
                lib_ids[signature] = lib_id
                content_pages.append({"id": lib_id, "label": page.get("label") or page["id"], "kind": KIND_CONTENT,
                                      "blocks": copy.deepcopy(page.get("blocks") or [])})
            entry = {"page": lib_id}
            if lib_id != page["id"]:
                entry["as"] = page["id"]
            entries.append(entry)
        hide = (overrides.get(key) or {}).get("roster_hide") or []
        devices[key] = {"pages": entries, "ring": {}, "roster_hide": list(hide)}

    # 3 to 5: the rules, as Ring pages -----------------------------------------------------------------------------------
    ring_pages: list[dict[str, Any]] = []
    ring_ids: dict[str, str] = {}
    won: set[tuple[str, str]] = set()                       # (scope, rule id) of every rule that wins somewhere
    rule_rows: dict[tuple[str, str], dict[str, Any]] = {}
    labels_used: dict[str, set[str]] = {}

    def add_ring_page(talk_id: str, label: str, rule: dict[str, Any], key: str, scope: str, rows: list[str]) -> str:
        note = {"scope": key, "rule": rule["id"]}
        stored_pages, expanded_pages = merged_pages(document, key), merged_pages(expanded, key)
        ring = _find_buttons(rule["ring_buttons"], expanded_pages, stored_pages, elements, plan.dropped,
                             {**note, "list": "ring_buttons"})
        call = _find_buttons(rule["call_buttons"], expanded_pages, stored_pages, elements, plan.dropped,
                             {**note, "list": "call_buttons"})
        cameras = None if rule["cameras"] is None else list(rule["cameras"])
        blocks = _ring_blocks(ring, call)
        signature = _canon([talk_id, cameras, blocks])
        page_id = ring_ids.get(signature)
        if page_id is None:
            page_id = "ring-" + hashlib.sha256(signature.encode()).hexdigest()[:8]
            page_id = page_id if page_id not in taken else _fresh_id(page_id, taken)
            taken.add(page_id)
            ring_ids[signature] = page_id
            wanted = f"Ring: {label}"[:MAX_LABEL].rstrip()
            used = labels_used.setdefault(talk_id, set())
            unique, n = wanted, 2
            while unique in used:
                unique = f"{wanted[:MAX_LABEL - 4].rstrip()} ({n})"
                n += 1
            used.add(unique)
            ring_pages.append({"id": page_id, "label": unique, "kind": KIND_RING, "talk": talk_id, "cameras": cameras,
                               "blocks": blocks})
            plan.ring_pages.append({"id": page_id, "label": unique, "talk": talk_id, "cameras": cameras,
                                    "ring_buttons": [a["id"] for a in ring], "call_buttons": [a["id"] for a in call],
                                    "from_rules": [], "assigned_to": []})
        row = next(r for r in plan.ring_pages if r["id"] == page_id)
        if rule["id"] not in row["from_rules"]:
            row["from_rules"].append(f"{scope}:{rule['id']}")
        if key not in row["assigned_to"]:
            row["assigned_to"].append(key)
        return page_id

    known_ids = set(by_endpoint)
    for key in callees:
        for caller in endpoints:
            if caller.id == key:
                continue
            rule = resolve_rule(document, key, caller.id, caller.kind)
            if rule is None:
                continue
            need = talks.for_endpoint(caller)
            if need is None:
                plan.problems.append(f"{key}: the rule {rule['id']!r} for {caller.name or caller.id} cannot be converted")
                continue
            scope = key if rule in ((overrides.get(key) or {}).get("rules") or []) else "template"
            page_id = add_ring_page(need.element_id, caller.name or caller.id, rule, key, scope, [])
            devices[key]["ring"][need.element_id] = page_id
            plan.assignments += 1
            won.add((scope, rule["id"]))
            rule_rows[(scope, rule["id"])] = rule
        # a rule whose caller no endpoint answers to: kept as a Ring page of a Talk element that is not there
        scoped = [(key, r) for r in (overrides.get(key) or {}).get("rules") or []]
        scoped += [("template", r) for r in (document.get("template") or {}).get("rules") or []]
        seen_orphans: set[str] = set()
        for scope, rule in scoped:
            caller_id = rule["caller"]
            if caller_id.startswith("kind:") or caller_id in known_ids or caller_id == key or caller_id in seen_orphans:
                continue
            seen_orphans.add(caller_id)
            talk_id = orphan_talk_id(caller_id)
            page_id = add_ring_page(talk_id, caller_id, rule, key, scope, [])
            devices[key]["ring"][talk_id] = page_id
            plan.assignments += 1
            won.add((scope, rule["id"]))
            rule_rows[(scope, rule["id"])] = rule

    all_rules = [(k, r) for k, o in overrides.items() for r in (o or {}).get("rules") or []]
    all_rules += [("template", r) for r in (document.get("template") or {}).get("rules") or []]
    for scope, rule in all_rules:
        if (scope, rule["id"]) in won:
            category = _caller_category(rule["caller"], by_endpoint)
            plan.rules_by_caller_kind[category] = plan.rules_by_caller_kind.get(category, 0) + 1
        else:
            plan.unused_rules.append({"scope": scope, "rule": rule["id"], "caller": rule["caller"],
                                      "why": "it never wins for any device (another rule is chosen first, it names the device "
                                             "itself, or no other endpoint has that kind)"})

    # the document ------------------------------------------------------------------------------------------------------
    doc6: dict[str, Any] = {"schema_version": SCHEMA_PAGES, "pages": [*content_pages, *ring_pages], "devices": devices,
                            "dashboards": copy.deepcopy(document.get("dashboards") or {}),
                            "endpoints": copy.deepcopy(document.get("endpoints") or {})}
    roster = (document.get("template") or {}).get("roster", [ROSTER_ALL])
    if roster != [ROSTER_ALL]:
        doc6["roster"] = list(roster)
    if document.get("home_name"):
        doc6["home_name"] = document["home_name"]
    if document.get("show_fps") is True:
        doc6["show_fps"] = True
    # Canonical form: what the store holds after it loads this file (and nothing it would drop).
    pages, loaded_devices, loaded_roster = load_pages_model(doc6)
    if len(pages) != len(doc6["pages"]):
        plan.problems.append("a page could not be carried over (it does not validate); see the log")
    plan.document = {**doc6, "pages": pages, "devices": loaded_devices}
    if loaded_roster != [ROSTER_ALL]:
        plan.document["roster"] = loaded_roster
    # The new material must pass the SAVE's validators too (an old page the loader tolerates is carried as it is).
    ring_seen: set[str] = set()
    by_id = library_by_id(plan.document)
    for page in plan.document["pages"]:
        if page_kind(page) != KIND_RING:
            continue
        try:
            normalize_library_page(page, ring_seen, strict=True)
        except ProfileError as err:
            plan.problems.append(f"{err}")
    for key, device in plan.document["devices"].items():
        try:
            normalize_device(key, device, by_id)
        except ProfileError as err:
            plan.problems.append(f"{err}")
    plan.problems.extend(talks.problems)
    return plan


# ---- the proof ---------------------------------------------------------------------------------------------------------

class _Overlay:
    """The registry the migrated document is resolved against in a dry run: everything the store answers, plus the Talk elements
    the migration would create (so a Ring page assigned to a not-yet-existing Talk element is found, as it will be after the
    create)."""

    def __init__(self, base: Any, extra: dict[str, tuple[str, str]]) -> None:
        self._base = base
        self._extra = extra

    def talk_parties(self) -> dict[str, tuple[str, str]]:
        return {**self._base.talk_parties(), **self._extra}

    def __getattr__(self, name: str) -> Any:
        return getattr(self._base, name)


def _differences(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for name in sorted(set(before) | set(after)):
        if before.get(name) == after.get(name):
            continue
        if name == "callers":
            a, b = before.get("callers") or {}, after.get("callers") or {}
            out.extend(f"callers.{c}" for c in sorted(set(a) | set(b)) if a.get(c) != b.get(c))
        else:
            out.append(name)
    return out


def prove_equality(before: dict[str, Any], after: dict[str, Any], keys: list[str], *, elements: dict[str, Any], states: Any,
                   registry: Any, extra_parties: dict[str, tuple[str, str]] | None = None) -> list[dict[str, Any]]:
    """For every device key: `resolve_endpoint(after) == resolve_endpoint(before)`, `rev` included, every caller. Returns one row
    per key `{endpoint, equal, rev_before, rev_after, callers, differences}`. `before`/`after` are stored documents (references
    are expanded here, against `elements`)."""
    old, new = expand_document(before, elements), expand_document(after, elements)
    overlay = _Overlay(registry, extra_parties or {})
    rows = []
    for key in keys:
        a = resolve_endpoint(old, key, states=states, registry=registry)
        b = resolve_endpoint(new, key, states=states, registry=overlay)
        rows.append({"endpoint": key, "kind": a.get("kind"), "equal": a == b, "rev_before": a["rev"], "rev_after": b["rev"],
                     "callers": len(a["callers"]), "pages": [p["id"] for p in a["pages"]],
                     "differences": [] if a == b else _differences(a, b)})
    return rows


# ---- the report -------------------------------------------------------------------------------------------------------

def build_report(document: dict[str, Any], plan: Plan, proof: list[dict[str, Any]], doc_rev: str, *,
                 disk_version: int | None) -> dict[str, Any]:
    """The dry run's answer. `ok` is true when nothing blocks the apply from the DATA's side (no problem, every device equal);
    `can_apply` additionally needs every Talk element it uses to exist or be creatable. Nothing in it is a secret: ids, names and
    counts."""
    creates = [n.as_row() for n in plan.talks.missing()]
    blocked: list[str] = []         # reserved: a Talk element that cannot be created (the managed ones are, by the reconcile)
    unequal = [r["endpoint"] for r in proof if not r["equal"]]
    problems = list(plan.problems)
    if unequal:
        problems.append("the resolved document differs before and after for: " + ", ".join(unequal))
    pages = plan.document.get("pages", [])
    ok = not problems
    return {
        "ok": ok,
        "can_apply": ok and not blocked,
        "doc_rev": doc_rev,
        "schema": {"from": disk_version, "to": SCHEMA_PAGES},
        "safety_copy": None if disk_version is None else f".storage/portico.content.v{disk_version}",
        "counts": {
            "endpoints": len(plan.callees),
            "library_pages": len(pages),
            "content_pages": sum(1 for p in pages if page_kind(p) == KIND_CONTENT),
            "ring_pages_new": len(plan.ring_pages),
            "ring_assignments": plan.assignments,
            "rules_converted_by_caller_kind": dict(sorted(plan.rules_by_caller_kind.items())),
            "rules_unused": len(plan.unused_rules),
            "talk_elements_to_create": len(creates),
            "dropped_dangling_buttons": len(plan.dropped),
            "endpoints_equal": sum(1 for r in proof if r["equal"]),
            "endpoints_unequal": len(unequal),
        },
        "ring_pages": plan.ring_pages,
        "talk_elements_to_create": creates,
        "blocked_by": blocked,
        "dropped": plan.dropped,
        "unused_rules": plan.unused_rules,
        "per_endpoint": proof,
        "problems": problems,
    }


def plan_for_store(store: Any, document: dict[str, Any], elements: dict[str, Any], states: Any,
                   disk_version: int | None) -> tuple[Plan, dict[str, Any]]:
    """The migration of the store's stored `document`, proven and reported (nothing is written)."""
    endpoints = endpoint_table(expand_document(document, elements), store)
    plan = migrate_to_v6(document, endpoints=endpoints, elements=elements)
    proof = prove_equality(document, plan.document, plan.callees, elements=elements, states=states, registry=store,
                           extra_parties=plan.talks.planned_parties())
    from .content_resolve import doc_rev

    return plan, build_report(document, plan, proof, doc_rev(document), disk_version=disk_version)


class MigrationRefused(Exception):
    """The apply is refused: `code` is the wire's error code, `report` the dry-run report that says why."""

    def __init__(self, code: str, message: str, report: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.report = report


async def run_migration(store: Any, hass: HomeAssistant, *, apply: bool, base_doc_rev: str | None) -> dict[str, Any]:
    """Dry run (`apply` False) or apply. The apply order is the placements design's rollback rule: the elements first (a Talk
    element created and left over is harmless), then the content write, only when the plan was re-proved against the elements as
    they are now. A refusal changes nothing in the content store."""
    await store.async_ensure_loaded()
    if store.uses_pages_model:
        raise MigrationRefused("already_applied", "the document is already the unified Pages model (schema 6)")
    plan, report = store.plan_pages_migration()
    if not apply:
        return report
    store.check_writable()
    if base_doc_rev != store.doc_rev:
        raise SaveConflict("the document changed since the dry run")
    if not report["ok"]:
        raise MigrationRefused("refused", "the migration does not give the same result for every device: " +
                               "; ".join(report["problems"][:3]), report)
    if not report["can_apply"]:
        raise MigrationRefused("talk_unavailable", "; ".join(report["blocked_by"][:3]), report)
    try:
        await ensure_talk_elements(hass, plan.talks.missing())
    except TalkUnavailable as err:
        raise MigrationRefused("talk_unavailable", str(err), report) from err
    plan, report = store.plan_pages_migration()            # again, with the elements as they are now
    if not report["ok"] or not report["can_apply"] or report["talk_elements_to_create"]:
        raise MigrationRefused("refused", "the plan changed after the Talk elements were created; run the dry run again", report)
    new_rev = await store.async_adopt_migrated(plan.document, base_doc_rev)
    return {**report, "applied": True, "doc_rev": new_rev}


# ---- the way back (best effort) ---------------------------------------------------------------------------------------

def export_to_v5(document: dict[str, Any], *, endpoints: list[Endpoint] | None = None,
                 parties: dict[str, tuple[str, str]] | None = None) -> tuple[dict[str, Any], list[str]]:
    """A schema 6 `document` as a schema 4 (5 while it holds a reference) document, and the notes on what it could not say. Pure.

    Every device becomes an override holding its own pages (wire ids) and one rule per Ring page it assigns (the caller is the
    endpoint the Talk element stands for, read from `parties` and `endpoints`); the template is empty and the roster is carried
    over. A Ring page's button that is not on the device's content pages cannot be named by a rule (v5 rules name buttons of the
    callee's pages) and is noted and left out; a Ring page whose Talk element stands for nobody is noted and left out. The
    `.v4`/`.v5` safety copy, not this, is the guaranteed way back."""
    from .content_elements import has_refs

    notes: list[str] = []
    endpoints = endpoints or []
    parties = parties or {}
    by_id = library_by_id(document)
    overrides: dict[str, Any] = {}
    for key, device in (document.get("devices") or {}).items():
        pages = copy.deepcopy(device_pages(document, key))
        on_pages = {a["id"] for page in pages for a in page_actions(page)}
        rules: list[dict[str, Any]] = []
        for talk, page_id in (device.get("ring") or {}).items():
            page = by_id.get(page_id)
            if page is None or page_kind(page) != KIND_RING or page.get("talk") != talk:
                notes.append(f"{key}: the assignment of {talk} to {page_id!r} names no Ring page of that Talk element; left out")
                continue
            caller = next((e for e in endpoints if party_matches(parties.get(talk), e)), None)
            if caller is None:
                notes.append(f"{key}: the Ring page {page_id!r} is the ring page of {talk}, which stands for no known caller; left out")
                continue
            ring, call = ring_call_actions(page)
            lists = {}
            for name, actions in (("ring_buttons", ring), ("call_buttons", call)):
                lists[name] = [a["id"] for a in actions if a["id"] in on_pages]
                for action in actions:
                    if action["id"] not in on_pages:
                        notes.append(f"{key}: the button {action['id']!r} of the Ring page {page_id!r} is not on its pages; "
                                     "a v5 rule cannot name it")
            rules.append({"id": page_id, "caller": caller.id, "cameras": page.get("cameras"), **lists})
        overrides[key] = {"pages": pages, "rules": rules, "roster_hide": list(device.get("roster_hide") or ())}
    out: dict[str, Any] = {
        "schema_version": 4, "template": {"pages": [], "rules": [], "roster": roster_of(document)},
        "overrides": overrides, "dashboards": copy.deepcopy(document.get("dashboards") or {}),
        "endpoints": copy.deepcopy(document.get("endpoints") or {})}
    if document.get("home_name"):
        out["home_name"] = document["home_name"]
    if document.get("show_fps") is True:
        out["show_fps"] = True
    if has_refs(out):
        out["schema_version"] = 5
    return out, notes


# ---- the admin command ------------------------------------------------------------------------------------------------

_REV = vol.All(str, vol.Length(min=1, max=64))


@websocket_api.websocket_command({
    vol.Required("type"): "portico/content/pages_migrate",
    vol.Optional("validate_only", default=True): bool,
    vol.Optional("base_doc_rev"): _REV,
})
@websocket_api.async_response
async def handle_pages_migrate(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """The Pages migration (admin). `validate_only: true` (the default) is the dry run: it writes nothing and returns the report
    (see `build_report`). `validate_only: false` applies it and needs the `base_doc_rev` the dry run returned as `doc_rev`; it
    is refused (`refused`, `talk_unavailable`) unless every device resolves identically, `conflict` when the document moved,
    `already_applied` when it is schema 6."""
    if not auth.require(connection, msg, "portico/content/pages_migrate"):
        return
    store = get_content_store(hass)
    apply = not msg["validate_only"]
    if apply and not msg.get("base_doc_rev"):
        connection.send_error(msg["id"], "invalid", "an apply needs the doc_rev the dry run returned (base_doc_rev)")
        return
    try:
        report = await run_migration(store, hass, apply=apply, base_doc_rev=msg.get("base_doc_rev"))
    except SaveConflict as err:
        connection.send_error(msg["id"], "conflict", str(err))
        return
    except MigrationRefused as err:
        connection.send_error(msg["id"], err.code, str(err))
        return
    except ProfileError as err:
        connection.send_error(msg["id"], "invalid", str(err))
        return
    connection.send_result(msg["id"], report)


@callback
def async_setup_pages_migrate(hass: HomeAssistant) -> None:
    websocket_api.async_register_command(hass, handle_pages_migrate)
