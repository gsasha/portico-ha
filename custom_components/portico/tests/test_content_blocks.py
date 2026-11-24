"""Blocks in the content store (DEV-658 B1, `portico_blocks_design.md` section 3): the schema-4 model, the
validators and limits of the new kinds, `migrate_v3_to_v4`, the readers `page_cameras` / `page_actions`, and the file
on disk (blocks only since B8).

The heart of it is data safety. A v3 file is read in memory as one block per page, nothing is written until the first
save, and that save first leaves `portico.content.v3`, which the schema-3 validators (`v3_loader.py`, a verbatim copy of
the build that is live before blocks) still read with every page intact: that copy is the way back (B8 dropped the flat
mirror the v4 file used to carry).
"""
from __future__ import annotations

import copy
import json
import random

import pytest
from pytest_homeassistant_custom_component.common import async_mock_service

from .. import content
from ..content import (
    KINDS, MAX_ACTIONS, MAX_BLOCK_ACTIONS, MAX_BLOCK_CAMERAS, MAX_BLOCKS,
    MAX_CAMERAS, ContentStore, ProfileError, RunRefused, SaveConflict, get_content_store, migrate_v3_to_v4,
    normalize_block, normalize_document, normalize_page, normalize_profile, title_from_label,
)
from ..content_resolve import (
    default_buttons, doc_rev, merged_pages, page_actions, page_cameras, resolve_action,
    resolve_page,
)
from ..endpoints import Endpoint
from . import v3_loader
from .test_content import _provide
from .test_signaling import _add_device

pytestmark = pytest.mark.asyncio

FIXTURE_DOOR = "http://10.8.0.5:8080"


# ---- fixtures: real stored shapes ---------------------------------------------------------------------------

def _cam(cid="gate", label="Gate", entity="camera.gate"):
    return {"id": cid, "label": label, "entity_id": entity}


def _act(aid, kind="lock_open", entity="lock.gate", label=None, **extra):
    out = {"id": aid, "label": label or aid.title(), "kind": kind, **extra}
    if kind == "portico_open":
        out["device_id"] = extra.pop("device_id", FIXTURE_DOOR)
        out.update(extra)
        out["confirm"] = out.get("confirm", True)
    elif kind == "call":
        out["confirm"] = False
    else:
        out["entity_id"] = entity
        out["confirm"] = KINDS[kind][2]
    return out


def live_v3():
    """A v3 file shaped like the live one: pages 'Home' and 'test' in the template (all six v3 kinds, URL-shaped
    door ids, a call button), a phone's override that replaces the template's page and adds one, a "Not attached"
    override (an old enrolment's id) with its own rule, template rules, dashboards, endpoint declarations,
    a roster, a home name."""
    gate_open = _act("open-gate", "portico_open", device_id="fake:gate-1")
    return {
        "schema_version": 3, "home_name": "My Home",
        "template": {
            "pages": [
                {"id": "home", "label": "Home",
                 "cameras": [_cam(), _cam("lobby", "Lobby", "camera.lobby")],
                 "actions": [gate_open, _act("front", "lock_open", "lock.front"),
                             _act("porch", "scene_activate", "scene.porch"), _act("bell", "button_press", "button.bell"),
                             _act("evening", "script_run", "script.evening", icon="mdi:lightbulb"),
                             {"id": "call-dina", "label": "Dina", "kind": "call", "confirm": False,
                              "target": {"type": "phone", "id": "ep-dina"}}]},
                {"id": "test", "label": "test", "cameras": [], "actions": []},
            ],
            "rules": [{"id": "door-rule", "caller": "kind:door", "cameras": None,
                       "ring_buttons": ["open-gate"], "call_buttons": ["open-gate", "front"]}],
            "roster": ["*"],
        },
        "overrides": {
            "ep-alice": {"pages": [{"id": "home", "label": "Alice's home", "cameras": [_cam()],
                                    "actions": [_act("open-gate", "portico_open", device_id=FIXTURE_DOOR)]},
                                   {"id": "garden", "label": "Garden", "cameras": [_cam("lawn", "Lawn", "camera.lawn")],
                                    "actions": []}],
                         "rules": [], "roster_hide": ["door-x"]},
            "old-enrolment": {"pages": [{"id": "home", "label": "Home", "cameras": [_cam("old", "Old", "camera.old")],
                                         "actions": [_act("old-open", "lock_open", "lock.old")]}],
                              "rules": [{"id": "r-old", "caller": "ep-dina", "cameras": ["camera.old"],
                                         "ring_buttons": ["old-open"], "call_buttons": []}], "roster_hide": []},
        },
        "dashboards": {"kitchen": {"name": "Kitchen", "ha_user_id": "u-kitchen", "room": "Kitchen",
                                   "cameras": ["camera.gate"]}},
        "endpoints": {"ep-alice": {"cameras": ["camera.alice"]}},
    }


def _all_v3_pages(doc):
    yield from doc["template"]["pages"]
    for override in doc["overrides"].values():
        yield from override["pages"]


def flatten(doc):
    """The v3 pages `{id, label, cameras, actions}` of a v4 document, through the block readers."""
    def one(page):
        return {"id": page["id"], "label": page["label"], "cameras": page_cameras(page), "actions": page_actions(page)}

    out = {"template": [one(p) for p in doc["template"]["pages"]],
           "overrides": {k: [one(p) for p in o["pages"]] for k, o in doc["overrides"].items()}}
    return out


def v3_pages(doc):
    return {"template": [dict(p) for p in doc["template"]["pages"]],
            "overrides": {k: [dict(p) for p in o["pages"]] for k, o in doc["overrides"].items()}}


def _page(blocks, pid="p", label="P"):
    return {"id": pid, "label": label, "blocks": blocks}


def _blk(bid="b", title="B", cameras=(), actions=(), **extra):
    return {"id": bid, "title": title, "cameras": list(cameras), "actions": list(actions), **extra}


def _strict(page):
    return normalize_page(page)


# ---- migrate_v3_to_v4 -----------------------------------------------------------------------------------------

async def test_migration_makes_one_block_per_page_named_after_it_and_changes_nothing_else():
    v3 = live_v3()
    before = copy.deepcopy(v3)
    v4 = migrate_v3_to_v4(v3)
    assert v3 == before                                               # pure: the input is not touched
    assert v4["schema_version"] == 4
    for section in ("rules", "roster"):
        assert v4["template"][section] == v3["template"][section]
    for key in ("home_name", "dashboards", "endpoints"):
        assert v4[key] == v3[key]
    assert {k: o["rules"] for k, o in v4["overrides"].items()} == {k: o["rules"] for k, o in v3["overrides"].items()}
    assert v4["overrides"]["ep-alice"]["roster_hide"] == ["door-x"]
    for old_pages, new_pages in [(v3["template"]["pages"], v4["template"]["pages"])] + [
            (v3["overrides"][k]["pages"], v4["overrides"][k]["pages"]) for k in v3["overrides"]]:
        assert [p["id"] for p in new_pages] == [p["id"] for p in old_pages]
        for old, new in zip(old_pages, new_pages):
            assert set(new) == {"id", "label", "blocks"}
            (block,) = new["blocks"]
            assert block == {"id": old["id"], "title": old["label"], "cameras": old["cameras"],
                             "actions": old["actions"]}
    # the "Not attached" override is migrated too, and an override page that replaces a template page still does
    assert [p["id"] for p in v4["overrides"]["old-enrolment"]["pages"]] == ["home"]
    merged = merged_pages(v4, "ep-alice")
    assert [p["id"] for p in merged] == ["home", "test", "garden"]
    assert merged[0]["label"] == "Alice's home"


async def test_flatten_of_the_migration_is_the_v3_pages_on_the_live_like_document():
    v3 = live_v3()
    assert flatten(migrate_v3_to_v4(v3)) == v3_pages(v3)


def _random_v3(rng):
    kinds = ["portico_open", "lock_open", "button_press", "script_run", "scene_activate", "call"]
    entities = {"lock_open": "lock", "button_press": "button", "script_run": "script", "scene_activate": "scene"}

    def page(pid):
        used = set()

        def item_id():
            while True:
                n = f"i{rng.randrange(1000)}"
                if n not in used:
                    used.add(n)
                    return n

        cams = [{"id": item_id(), "label": f"C{n}", "entity_id": f"camera.c{rng.randrange(50)}"}
                for n in range(rng.randrange(0, 9))]
        acts = []
        for _ in range(rng.randrange(0, 9)):
            kind = rng.choice(kinds)
            a = {"id": item_id(), "label": f"A{len(acts)}", "kind": kind, "confirm": rng.random() < 0.5}
            if kind == "portico_open":
                a["device_id"] = rng.choice(["gate-1", FIXTURE_DOOR, "fake:gate-2"])
            elif kind == "call":
                a["target"] = {"type": rng.choice(["phone", "door", "dashboard"]), "id": f"t{rng.randrange(9)}"}
                a["confirm"] = False
            else:
                a["entity_id"] = f"{entities[kind]}.e{rng.randrange(50)}"
            if rng.random() < 0.3:
                a["icon"] = rng.choice(["door", "mdi:gate", "lock"])
            acts.append(a)
        return {"id": pid, "label": rng.choice([pid.title(), "Home", "x" * 40, "Dom בית"]),
                "cameras": cams, "actions": acts}

    doc = {"schema_version": 3, "template": {"pages": [page(f"t{n}") for n in range(rng.randrange(0, 5))],
                                             "rules": [], "roster": ["*"]},
           "overrides": {f"k{n}": {"pages": [page(f"o{m}") for m in range(rng.randrange(0, 4))], "rules": [],
                                   "roster_hide": []} for n in range(rng.randrange(0, 4))},
           "dashboards": {}, "endpoints": {}}
    return doc


async def test_property_flatten_of_migrate_is_the_v3_pages_and_migrate_is_idempotent_on_generated_documents():
    rng = random.Random(658)
    for _ in range(300):
        v3 = _random_v3(rng)
        v4 = migrate_v3_to_v4(v3)
        assert flatten(v4) == v3_pages(v3)
        assert migrate_v3_to_v4(v4) == v4                                    # running it again changes nothing
        # and the loader reads every generated page without dropping or changing an item
        for pages in [v4["template"]["pages"], *(o["pages"] for o in v4["overrides"].values())]:
            for page in pages:
                loaded = normalize_page(page, strict=False)
                assert loaded["blocks"] == page["blocks"]


async def test_migrating_a_v4_document_again_and_a_v3_page_that_already_has_blocks_changes_nothing():
    v4 = migrate_v3_to_v4(live_v3())
    assert migrate_v3_to_v4(v4) == v4
    assert migrate_v3_to_v4({"template": {"pages": [_page([_blk()])]}})["template"]["pages"] == [_page([_blk()])]


async def test_a_label_with_control_characters_becomes_a_clean_title_and_the_label_itself_is_kept():
    v3 = {"schema_version": 3, "template": {"pages": [
        {"id": "a", "label": "Line\none\tTwo", "cameras": [], "actions": []},
        {"id": "b", "label": "x y", "cameras": [], "actions": []},
        {"id": "c", "label": "\n", "cameras": [], "actions": []},
        {"id": "d-e", "cameras": [], "actions": []}], "rules": [], "roster": ["*"]}, "overrides": {}}
    pages = migrate_v3_to_v4(v3)["template"]["pages"]
    assert [p["blocks"][0]["title"] for p in pages] == ["Line one Two", "x y", "C", "D E"]
    assert pages[0]["label"] == "Line\none\tTwo" and "label" not in pages[3]
    # every migrated title passes the strict title rule, so the first full-document save is not refused for it
    for page in pages:
        normalize_block(page["blocks"][0], set(), set())
    assert title_from_label("Garden", "garden") == "Garden"
    assert title_from_label("  \n ", "my-garden") == "My Garden"
    assert title_from_label(None, "x") == "X" and len(title_from_label("y" * 40, "x")) == 40


async def test_malformed_pages_survive_the_migration_untouched_for_the_loader_to_judge():
    v3 = {"schema_version": 3, "template": {"pages": [{"id": "ok", "label": "Ok", "cameras": [], "actions": []},
                                                      "junk", {"label": "no id"}, {"id": "bad", "cameras": "x"}],
                                            "rules": [], "roster": ["*"]},
          "overrides": {"o": "not a dict", "p": {"pages": "nope"}, "q": {"roster_hide": []}}, "dashboards": []}
    v4 = migrate_v3_to_v4(v3)
    assert v4["template"]["pages"][1:3] == ["junk", {"label": "no id"}]
    assert v4["template"]["pages"][3]["blocks"][0]["cameras"] == "x"       # kept as it was; the loader drops the page
    assert v4["overrides"] == {"o": "not a dict", "p": {"pages": "nope"}, "q": {"roster_hide": []}}
    assert migrate_v3_to_v4({}) == {"schema_version": 4}
    assert migrate_v3_to_v4({"template": None})["template"] is None


# ---- the loader and the strict form ----------------------------------------------------------------------------

async def test_the_migrated_live_like_document_is_already_a_valid_document():
    """A document that was not edited normalises to itself, so the first full save from the editor is a no-op."""
    v4 = migrate_v3_to_v4(live_v3())
    out = normalize_document(v4)
    assert out == v4 | {"schema_version": 4}
    # v3 PAGES (no `blocks`) inside a schema-4 document are wrapped the same way (how the panel on disk adds a page)
    assert normalize_document({**live_v3(), "schema_version": 4}) == out


async def test_only_a_schema_4_or_5_document_is_taken_a_schema_3_tab_is_told_to_reload():
    for version in (1, 2, 3, 7):
        with pytest.raises(ProfileError, match="schema_version must be 4 or 5; reload the Portico page"):
            normalize_document({"schema_version": version})
    assert normalize_document({"schema_version": 4})["schema_version"] == 4
    assert normalize_document({"schema_version": 5})["schema_version"] == 4       # 5 is stamped only by a reference
    assert normalize_document({})["schema_version"] == 4
    assert normalize_document({"schema_version": 6})["schema_version"] == 6        # the unified model (Pages unified, P-A)


async def test_a_page_without_blocks_is_a_v3_page_and_never_read_as_empty():
    page = normalize_page({"id": "home", "cameras": [_cam()], "actions": [_act("a")]})
    assert page == {"id": "home", "label": "Home", "blocks": [
        {"id": "home", "title": "Home", "cameras": [_cam()], "actions": [_act("a")]}]}
    assert normalize_page({"id": "x"})["blocks"][0]["cameras"] == []
    # a page WITH blocks ignores the flat mirror beside it, whatever it holds
    mirrored = {**_page([_blk(cameras=[_cam("real")])]), "cameras": [_cam("fake")], "actions": [_act("fake-a")]}
    assert normalize_page(mirrored)["blocks"][0]["cameras"] == [_cam("real")]
    assert set(normalize_page(mirrored)) == {"id", "label", "blocks"}


async def test_every_kind_validates_in_a_block_and_keeps_its_fields():
    actions = [
        {"id": "lk", "label": "Lock", "kind": "lock_lock", "entity_id": "lock.front"},
        {"id": "ulk", "label": "Unlock", "kind": "lock_unlock", "entity_id": "lock.front"},
        {"id": "co", "label": "Open gate", "kind": "cover_open", "entity_id": "cover.gate", "color": "green",
         "icon": "gate-open"},
        {"id": "cc", "label": "Close gate", "kind": "cover_close", "entity_id": "cover.gate", "show_label": False,
         "icon": "mdi:gate"},
        {"id": "tk", "label": "Talk", "kind": "talk", "confirm": True},
        {"id": "po", "label": "Open door", "kind": "portico_open"},
    ]
    block = normalize_block(_blk(door="fake:gate-1", actions=actions, cameras=[{**_cam(), "size": "big"}]), set(), set())
    by_id = {a["id"]: a for a in block["actions"]}
    assert by_id["lk"]["confirm"] is False and by_id["ulk"]["confirm"] is True      # the kind's default
    assert by_id["co"]["confirm"] is True and by_id["cc"]["confirm"] is False
    assert by_id["tk"]["confirm"] is False                       # never honoured (CB-9), whatever was sent
    assert "entity_id" not in by_id["tk"] and "device_id" not in by_id["tk"]
    assert "device_id" not in by_id["po"]                         # stored as it was; the readers write it in
    assert by_id["co"]["color"] == "green" and by_id["cc"]["show_label"] is False
    assert block["door"] == "fake:gate-1" and block["cameras"][0]["size"] == "big"
    assert {"lock_lock", "lock_unlock", "cover_open", "cover_close", "talk"} <= set(KINDS)


@pytest.mark.parametrize("kind,entity", [("lock_lock", "cover.x"), ("lock_unlock", "light.x"), ("cover_open", "lock.x"),
                                         ("cover_close", "camera.x")])
async def test_the_new_service_kinds_name_an_entity_of_their_own_domain(kind, entity):
    with pytest.raises(ProfileError, match="entity is required"):
        normalize_block(_blk(actions=[{"id": "a", "label": "A", "kind": kind, "entity_id": entity}]), set(), set())
    with pytest.raises(ProfileError, match="entity is required"):
        normalize_block(_blk(actions=[{"id": "a", "label": "A", "kind": kind}]), set(), set())


async def test_talk_needs_a_door_and_so_does_a_portico_open_without_a_device_id():
    talk = {"id": "t", "label": "Talk", "kind": "talk"}
    bare_open = {"id": "o", "label": "Open", "kind": "portico_open"}
    with pytest.raises(ProfileError, match="talk button needs the block to have a door"):
        normalize_block(_blk(actions=[talk]), set(), set())
    with pytest.raises(ProfileError, match="device_id is required"):
        normalize_block(_blk(actions=[bare_open]), set(), set())
    with pytest.raises(ProfileError, match="device_id is required"):
        normalize_profile({"actions": [bare_open]})                    # a flat profile has no door
    with pytest.raises(ProfileError, match="talk button"):
        normalize_profile({"actions": [talk]})
    assert normalize_block(_blk(door="d", actions=[talk, bare_open]), set(), set())["actions"][0]["kind"] == "talk"
    # an explicit device_id wins over the door and a bad one is still refused
    assert normalize_block(_blk(door="d", actions=[{**bare_open, "device_id": "other"}]), set(), set()
                           )["actions"][0]["device_id"] == "other"
    with pytest.raises(ProfileError, match="device_id is required"):
        normalize_block(_blk(door="d", actions=[{**bare_open, "device_id": 5}]), set(), set())
    # loading is lenient about a talk button without a door (it can't crash; it is refused when run)
    assert normalize_page(_page([_blk(actions=[talk])]), strict=False)["blocks"][0]["actions"][0]["kind"] == "talk"
    # but never about a portico_open that names nothing: that page was invalid under v3 as well
    with pytest.raises(ProfileError):
        normalize_page(_page([_blk(actions=[bare_open])]), strict=False)


async def test_door_must_be_a_short_nonempty_string():
    for door in ("", 5, ["x"], "d" * 129):
        with pytest.raises(ProfileError, match="door"):
            normalize_block(_blk(door=door), set(), set())
    assert normalize_block(_blk(door="d" * 128), set(), set())["door"] == "d" * 128
    assert "door" not in normalize_block(_blk(door=None), set(), set())


async def test_show_label_false_needs_an_icon_from_the_list_and_loads_leniently():
    bad = [{"id": "a", "label": "A", "kind": "lock_lock", "entity_id": "lock.x", "show_label": False}]
    with pytest.raises(ProfileError, match="hidden only on a button with an icon"):
        normalize_block(_blk(actions=bad), set(), set())
    bad[0]["icon"] = "not-in-the-list"
    with pytest.raises(ProfileError, match="hidden only on a button with an icon"):
        normalize_block(_blk(actions=bad), set(), set())
    for icon in ("gate", "mdi:gate", "boom-gate"):
        bad[0]["icon"] = icon
        assert normalize_block(_blk(actions=bad), set(), set())["actions"][0]["show_label"] is False
    bad[0]["icon"] = "not-in-the-list"
    loaded = normalize_block(_blk(actions=bad), set(), set(), strict=False)["actions"][0]
    assert "show_label" not in loaded                                   # read as shown; the page survives
    with pytest.raises(ProfileError, match="show_label must be true or false"):
        normalize_block(_blk(actions=[{**bad[0], "show_label": "no"}]), set(), set())
    plain = {"id": "z", "label": "Z", "kind": "lock_lock", "entity_id": "lock.x"}
    assert "show_label" not in normalize_block(_blk(actions=[plain]), set(), set())["actions"][0]
    assert normalize_block(_blk(actions=[{**plain, "show_label": True}]), set(), set())["actions"][0]["show_label"] is True


async def test_unknown_size_and_color_are_refused_on_a_save_and_read_as_absent_on_a_load(caplog):
    cam = {**_cam(), "size": "huge"}
    act = {"id": "a", "label": "A", "kind": "lock_lock", "entity_id": "lock.x", "color": "magenta"}
    with pytest.raises(ProfileError, match="size must be one of auto, big, small"):
        normalize_block(_blk(cameras=[cam]), set(), set())
    with pytest.raises(ProfileError, match="colour must be one of blue"):
        normalize_block(_blk(actions=[act]), set(), set())
    page = _page([_blk(cameras=[cam, {**_cam("c2"), "size": "small"}], actions=[act])])
    with caplog.at_level("WARNING"):
        loaded = normalize_page(page, strict=False, where="template")
    block = loaded["blocks"][0]
    assert "size" not in block["cameras"][0] and block["cameras"][1]["size"] == "small"
    assert "color" not in block["actions"][0]
    assert caplog.text.count("was read with 2 value(s) it does not know") == 1       # one warning for the page
    for size in ("auto", "big", "small"):
        assert normalize_block(_blk(cameras=[{**_cam(), "size": size}]), set(), set())["cameras"][0]["size"] == size


# ---- limits -----------------------------------------------------------------------------------------------------

def _cams(n, start=0):
    return [_cam(f"c{start + i}", f"C{start + i}", f"camera.c{start + i}") for i in range(n)]


def _acts(n, start=0):
    return [_act(f"a{start + i}", "lock_lock", f"lock.l{start + i}") for i in range(n)]


async def test_limits_per_block_per_page_and_blocks_per_page():
    assert (MAX_BLOCKS, MAX_BLOCK_CAMERAS, MAX_BLOCK_ACTIONS, MAX_CAMERAS, MAX_ACTIONS) == (8, 12, 12, 24, 24)
    assert len(_strict(_page([_blk(cameras=_cams(12), actions=_acts(12))]))["blocks"]) == 1
    with pytest.raises(ProfileError, match="at most 12 cameras and 12 buttons in a block"):
        _strict(_page([_blk(cameras=_cams(13))]))
    with pytest.raises(ProfileError, match="at most 12 cameras and 12 buttons in a block"):
        _strict(_page([_blk(actions=_acts(13))]))
    two = _page([_blk("b1", "One", _cams(12), _acts(12)), _blk("b2", "Two", _cams(12, 12), _acts(12, 12))])
    assert len(_strict(two)["blocks"]) == 2                                # exactly 24 and 24 across two blocks
    with pytest.raises(ProfileError, match="at most 24 cameras and 24 actions on a page"):
        _strict(_page([_blk("b1", "One", _cams(12)), _blk("b2", "Two", _cams(12, 12)), _blk("b3", "Three", _cams(1, 24))]))
    with pytest.raises(ProfileError, match="at most 24 cameras and 24 actions on a page"):
        _strict(_page([_blk("b1", "One", actions=_acts(12)), _blk("b2", "Two", actions=_acts(12, 12)),
                       _blk("b3", "Three", actions=_acts(1, 24))]))
    eight = _page([_blk(f"b{n}", f"B{n}") for n in range(8)])
    assert len(_strict(eight)["blocks"]) == 8
    with pytest.raises(ProfileError, match="at most 8 blocks"):
        _strict(_page([_blk(f"b{n}", f"B{n}") for n in range(9)]))


async def test_a_load_never_drops_a_page_for_the_block_limits_a_v3_page_may_exceed():
    """A v3 page could hold 24 cameras; migrated it is ONE block of 24, over this build's 12 per block. Nothing is
    dropped on load; the save names the block and says what to do."""
    v3 = {"schema_version": 3, "template": {"pages": [{"id": "big", "label": "Big", "cameras": _cams(24),
                                                      "actions": _acts(24)}], "rules": [], "roster": ["*"]},
          "overrides": {}}
    v4 = migrate_v3_to_v4(v3)
    loaded = normalize_page(v4["template"]["pages"][0], strict=False)
    assert len(loaded["blocks"][0]["cameras"]) == 24 and len(loaded["blocks"][0]["actions"]) == 24
    with pytest.raises(ProfileError, match="block 'big': at most 12 cameras"):
        normalize_document(v4)
    over = _page([_blk(cameras=_cams(25))])                                  # beyond a v3 page: dropped as before
    with pytest.raises(ProfileError):
        normalize_page(over, strict=False)
    nine = _page([_blk(f"b{n}", "Same") for n in range(9)])                  # block count and titles: save-only rules
    assert len(normalize_page(nine, strict=False)["blocks"]) == 9


async def test_ids_are_unique_per_page_across_blocks_and_block_ids_are_a_separate_namespace():
    one = _blk("b1", "One", [_cam("x")], [_act("a")])
    two = _blk("b2", "Two", [_cam("y")], [_act("a")])
    with pytest.raises(ProfileError, match="the id 'a' is used twice"):
        _strict(_page([one, two]))
    with pytest.raises(ProfileError, match="the id 'x' is used twice"):
        _strict(_page([one, _blk("b2", "Two", [_cam("x")])]))
    with pytest.raises(ProfileError, match="unique id"):
        _strict(_page([_blk("b1", "One"), _blk("b1", "Two")]))
    # a block may carry the id of an item (and of its page): different namespaces
    ok = _strict(_page([_blk("a", "One", [_cam("a")]), _blk("p", "Two", actions=[_act("b")])]))
    assert [b["id"] for b in ok["blocks"]] == ["a", "p"]
    # the same item id on two different pages is fine (ids are unique per page)
    normalize_document({"template": {"pages": [_page([one], "p1", "P1"), _page([_blk("b1", "One", [_cam("x")])], "p2", "P2")]}})


async def test_block_titles_are_required_trimmed_bounded_unique_and_free_of_control_characters():
    assert _strict(_page([_blk(title="  Garden  ")]))["blocks"][0]["title"] == "Garden"
    for title in ("", "   ", None, 5, "x" * 41, "a\nb", "a\tb", "a\x00b", "a b", "a b", "a\x7fb"):
        with pytest.raises(ProfileError, match="title"):
            _strict(_page([_blk(title=title)]))
    assert len(_strict(_page([_blk(title="x" * 40)]))["blocks"][0]["title"]) == 40
    with pytest.raises(ProfileError, match="two blocks are called 'garden'"):
        _strict(_page([_blk("b1", "Garden"), _blk("b2", "garden")]))
    # a title with a control character already stored loads (the load does not apply the new rules)
    assert normalize_page(_page([_blk(title="a\nb")]), strict=False)["blocks"][0]["title"] == "a\nb"
    with pytest.raises(ProfileError, match="title is required"):
        normalize_page(_page([_blk(title="")]), strict=False)


async def test_a_color_must_be_a_palette_name():
    for color in ("Blue", "", "#fff", 3, "orange"):
        with pytest.raises(ProfileError, match="colour must be one of"):
            normalize_block(_blk(actions=[_act("a", "lock_lock", color=color)]), set(), set())
    for color in content.PALETTE_NAMES:
        assert normalize_block(_blk(actions=[_act("a", "lock_lock", color=color)]), set(), set()
                               )["actions"][0]["color"] == color


@pytest.mark.parametrize("bad", [None, "str", 5, [], [None], {}, {"label": "no id"}, {"id": "Bad Id"}])
async def test_a_malformed_page_is_refused(bad):
    with pytest.raises(ProfileError):
        normalize_page(bad)


@pytest.mark.parametrize("blocks", [None, 1, "x", {"id": "b"}])
async def test_blocks_must_be_a_list(blocks):
    with pytest.raises(ProfileError, match="blocks must be a list"):
        normalize_page({"id": "p", "blocks": blocks})


@pytest.mark.parametrize("block", [
    None, "b", 5, [], {"id": "Bad Id", "title": "T"}, {"id": "b"}, {"title": "T"},
    {"id": "b", "title": "T", "cameras": "x"}, {"id": "b", "title": "T", "actions": {}},
    {"id": "b", "title": "T", "cameras": [None]}, {"id": "b", "title": "T", "actions": ["x"]},
    {"id": "b", "title": "T", "actions": [{"id": "a", "label": "A", "kind": "nope"}]},
    {"id": "b", "title": "T", "cameras": [{"id": "c", "label": 5, "entity_id": "camera.c"}]},
    {"id": "b", "title": "T", "cameras": [{"id": "c", "label": "C", "entity_id": "light.c"}]},
])
async def test_a_malformed_block_drops_its_page_on_load_and_only_that_page(block, caplog):
    """The loader keeps v3's contract: an invalid page is dropped with a warning, the others are read."""
    good = _page([_blk()], "good", "Good")
    with pytest.raises(ProfileError):
        normalize_page(_page([block]))
    with caplog.at_level("WARNING"):
        pages = content._normalize_pages([good, _page([block], "bad", "Bad")], "template")
    assert [p["id"] for p in pages] == ["good"] and "dropped an invalid stored profile" in caplog.text


# ---- rules ------------------------------------------------------------------------------------------------------

def _doc_with_rule(buttons, blocks=None, field="ring_buttons"):
    blocks = blocks or [_blk("b1", "One", actions=[_act("a1", "lock_lock", "lock.x")]),
                        _blk("b2", "Two", door="d", actions=[{"id": "tk", "label": "Talk", "kind": "talk"},
                                                            _act("a2", "lock_unlock", "lock.y")])]
    return {"template": {"pages": [_page(blocks)], "roster": ["*"],
                         "rules": [{"id": "r", "caller": "kind:door", "cameras": None, field: buttons}]}}


async def test_a_rule_may_name_a_button_in_any_block_but_never_a_talk_button():
    out = normalize_document(_doc_with_rule(["a1", "a2"]))
    assert out["template"]["rules"][0]["ring_buttons"] == ["a1", "a2"]
    assert normalize_document(_doc_with_rule(["a2"], field="call_buttons"))
    for field in ("ring_buttons", "call_buttons"):
        with pytest.raises(ProfileError, match="a talk button; a ring is not the moment to start a talk"):
            normalize_document(_doc_with_rule(["tk"], field=field))
    with pytest.raises(ProfileError, match="which is not a button on any of its pages"):
        normalize_document(_doc_with_rule(["ghost"]))


async def test_deleting_a_block_whose_button_a_rule_names_is_refused():
    doc = _doc_with_rule(["a2"])
    doc["template"]["pages"][0]["blocks"].pop()                         # the editor deletes block "Two"
    with pytest.raises(ProfileError, match="rule 'r' ring_buttons names 'a2', which is not a button"):
        normalize_document(doc)


async def test_a_talk_id_in_a_stored_rule_is_dropped_by_the_resolver_not_a_crash(caplog):
    pages = [_page([_blk(door="d", actions=[{"id": "tk", "label": "Talk", "kind": "talk", "confirm": False},
                                            _act("a1", "lock_lock", "lock.x")])])]
    from ..content_resolve import _find_actions
    with caplog.at_level("WARNING"):
        found = _find_actions(pages, ["tk", "a1"], "rule 'r'")
    assert [a["id"] for a in found] == ["a1"] and "talk button 'tk'" in caplog.text


# ---- the readers and the mirror ---------------------------------------------------------------------------------

async def test_readers_walk_the_blocks_in_order_and_write_the_door_into_a_bare_portico_open():
    bare = {"id": "po", "label": "Open", "kind": "portico_open", "confirm": True}
    named = {"id": "pn", "label": "Open 2", "kind": "portico_open", "device_id": "other", "confirm": True}
    page = _page([_blk("b1", "One", [_cam("c1")], [bare]),
                  _blk("b2", "Two", [_cam("c2"), _cam("c3")], [named], door="gate"),
                  _blk("b3", "Three", [], [bare | {"id": "po2"}], door="gate")])
    assert [c["id"] for c in page_cameras(page)] == ["c1", "c2", "c3"]
    assert [a["id"] for a in page_actions(page)] == ["po", "pn", "po2"]
    by_id = {a["id"]: a for a in page_actions(page)}
    assert "device_id" not in by_id["po"]                       # block One has no door: nothing to write
    assert by_id["pn"]["device_id"] == "other" and by_id["po2"]["device_id"] == "gate"
    assert "device_id" not in page["blocks"][2]["actions"][0]    # reading never changes the stored page
    # a page with no blocks reads as empty: the flat lists are not a form of a page any more (B8)
    assert page_cameras({"cameras": [_cam()]}) == [] and page_actions({"actions": [_act("a")]}) == []
    assert page_cameras({}) == [] and page_actions({}) == [] and page_actions({"blocks": []}) == []


async def test_page_readers_return_every_kind_with_the_device_id_written_in():
    actions = [_act("old"), _act("po", "portico_open", device_id="x"),
               {"id": "po2", "label": "Door", "kind": "portico_open", "confirm": True, "color": "blue"},
               {"id": "ul", "label": "Unlock", "kind": "lock_unlock", "entity_id": "lock.a", "confirm": True},
               {"id": "cv", "label": "Gate", "kind": "cover_open", "entity_id": "cover.a", "confirm": True},
               {"id": "tk", "label": "Talk", "kind": "talk", "confirm": False},
               {"id": "co", "label": "Call", "kind": "call", "target": {"type": "phone", "id": "p"}, "confirm": False,
                "show_label": False, "icon": "phone"}]
    page = _page([_blk(door="gate", cameras=[{**_cam(), "size": "big"}], actions=actions)])
    assert [a["id"] for a in page_actions(page)] == ["old", "po", "po2", "ul", "cv", "tk", "co"]   # EVERY kind
    by_id = {a["id"]: a for a in page_actions(page)}
    assert by_id["po2"] == {"id": "po2", "label": "Door", "kind": "portico_open", "confirm": True, "color": "blue",
                            "device_id": "gate"}
    assert by_id["co"]["show_label"] is False and by_id["tk"]["kind"] == "talk"
    assert page_cameras(page) == [{**_cam(), "size": "big"}]                   # v4 fields stay: there is no mirror to strip them
    assert "device_id" not in page["blocks"][0]["actions"][2]                  # reading never changes the stored page


async def test_default_buttons_find_a_door_buttons_through_blocks_and_the_materialised_device_id():
    door = Endpoint(id="fake:gate-1", kind="door", provider="fake", device_id="gate-1", name="Gate", ha_user_id=None,
                    cameras=(), room=None, override=None, available=True, reason=None)
    pages = [_page([_blk("b1", "One", actions=[{"id": "po", "label": "Open", "kind": "portico_open", "confirm": True}],
                         door="gate-1"),
                    _blk("b2", "Two", actions=[{"id": "other", "label": "Other", "kind": "portico_open",
                                                "device_id": "gate-2", "confirm": True},
                                               _act("lk", "lock_open", "lock.front"), _act("ul", "lock_unlock", "lock.x")])])]
    assert [a["id"] for a in default_buttons(pages, door)] == ["po", "lk"]


def _door_ep(provider, device_id):
    return Endpoint(id=f"{provider}:{device_id}", kind="door", provider=provider, device_id=device_id, name="Door",
                    ha_user_id=None, cameras=(), room=None, override=None, available=True, reason=None)


def _open_ids(door, block_door):
    pages = [_page([_blk("b1", "One", actions=[{"id": "po", "label": "Open", "kind": "portico_open", "confirm": True}],
                         door=block_door)])]
    return [a["id"] for a in default_buttons(pages, door)]


@pytest.mark.parametrize("provider,device_id,block_door,matches", [
    ("fake", "gate-1", "gate-1", True),                                   # bare
    ("fake", "gate-1", "fake:gate-1", True),                              # namespaced (Door.endpoint_id)
    ("fake", "gate-1", "other:gate-1", False),                            # another provider's door of the same id
    ("fake", "gate-1", "gate-2", False),                                  # unknown door
    ("fake", "gate-1", "fake:gate-2", False),
    ("fake", "b", "a:b", False),                                          # `a:b` is never the bare `b`
    ("a", "b", "b", True),
    ("a", "b", "a:b", True),
    ("a", "b", "a", False),
    ("portico_intercom", "http://10.6.0.7:8091", "http://10.6.0.7:8091", True),                      # URL, bare
    ("portico_intercom", "http://10.6.0.7:8091", "portico_intercom:http://10.6.0.7:8091", True),     # URL, namespaced
    ("portico_intercom", "http://10.6.0.7:8091", "http://10.6.0.8:8091", False),
    ("portico_intercom", "http://10.6.0.7:8091", "other:http://10.6.0.7:8091", False),
])
async def test_default_buttons_match_a_door_by_the_provider_registrys_rule(provider, device_id, block_door, matches):
    assert _open_ids(_door_ep(provider, device_id), block_door) == (["po"] if matches else [])


async def test_default_buttons_match_a_namespaced_device_id_written_on_the_action_too():
    door = _door_ep("fake", "gate-1")
    pages = [_page([_blk("b1", "One", actions=[{"id": "ns", "label": "A", "kind": "portico_open",
                                                "device_id": "fake:gate-1", "confirm": True},
                                               {"id": "no", "label": "B", "kind": "portico_open",
                                                "device_id": "x:gate-1", "confirm": True}])])]
    assert [a["id"] for a in default_buttons(pages, door)] == ["ns"]


async def test_resolve_page_serves_blocks_only_and_talk_resolves_off():
    class States:
        def get(self, entity_id):
            return None

    class Registry:
        def door(self, device_id):
            return None

    page = _page([_blk(door="d", cameras=[{**_cam(), "size": "big"}],
                       actions=[_act("old"), {"id": "ul", "label": "U", "kind": "lock_unlock", "entity_id": "lock.a",
                                              "confirm": True},
                                {"id": "tk", "label": "Talk", "kind": "talk", "confirm": False}])])
    resolved = resolve_page(page, states=States(), registry=Registry())
    assert set(resolved) == {"blocks"}
    assert [a["id"] for a in resolved["blocks"][0]["actions"]] == ["old", "ul", "tk"]         # every kind
    assert resolved["blocks"][0]["cameras"][0]["size"] == "big"
    talk = resolve_action({"id": "tk", "label": "Talk", "kind": "talk", "confirm": False}, states=States(),
                          registry=Registry())
    assert talk["available"] is False and talk["reason"] == "talk_off" and talk["name"] == "Talk"


# ---- a camera's name may be empty: the picture identifies itself, and no name is drawn -----------------------------

@pytest.mark.parametrize("strict", [True, False])
@pytest.mark.parametrize("raw", ["", "   ", "\t\n"])
async def test_a_camera_may_have_an_empty_or_blank_name_and_it_is_stored_as_the_empty_string(raw, strict):
    page = normalize_page(_page([_blk(cameras=[_cam("c", raw)])]), strict=strict)
    camera = page["blocks"][0]["cameras"][0]
    assert camera["label"] == "" and "label" in camera                # the key stays; nothing is substituted
    assert camera == {"id": "c", "label": "", "entity_id": "camera.gate"}


@pytest.mark.parametrize("label", [None, 5, ["x"]])
async def test_a_camera_name_must_still_be_a_string(label):
    camera = {"id": "c", "entity_id": "camera.gate", "label": label}
    with pytest.raises(ProfileError, match="label is required"):
        normalize_page(_page([_blk(cameras=[camera])]))
    with pytest.raises(ProfileError, match="label is required"):
        normalize_page(_page([_blk(cameras=[{"id": "c", "entity_id": "camera.gate"}])]))


async def test_a_camera_name_is_still_bounded_and_trimmed_when_it_is_not_empty():
    ok = normalize_page(_page([_blk(cameras=[_cam("c", "  Gate  ")])]))["blocks"][0]["cameras"][0]
    assert ok["label"] == "Gate"
    with pytest.raises(ProfileError, match="longer than"):
        normalize_page(_page([_blk(cameras=[_cam("c", "x" * (content.MAX_LABEL + 1))])]))


async def test_buttons_and_pages_still_need_a_name_when_a_camera_does_not():
    for blank in ("", "  "):
        action = {**_act("a"), "label": blank}
        with pytest.raises(ProfileError, match="label is required"):
            normalize_page(_page([_blk(actions=[action])]))
        with pytest.raises(ProfileError, match="label is required"):
            normalize_page(_page([_blk()], label=blank))
        with pytest.raises(ProfileError, match="label is required"):
            normalize_page(_page([_blk(actions=[action])]), strict=False)


async def test_an_empty_camera_name_survives_a_save_a_load_and_the_resolved_wire_with_the_label_key_present(hass, hass_storage):
    store = get_content_store(hass)
    await store.async_ensure_loaded()
    doc = {"template": {"pages": [_page([_blk("b", "B", [_cam("named", "Gate"), _cam("bare", "", "camera.lobby"),
                                                          _cam("blank", "   ", "camera.yard")])], "home", "Home")],
                        "rules": [], "roster": ["*"]}}
    await store.async_save_document(doc, store.doc_rev)
    disk = hass_storage["portico.content"]["data"]["template"]["pages"][0]["blocks"][0]["cameras"]
    assert [c["label"] for c in disk] == ["Gate", "", ""]
    reloaded = content.ContentStore(hass)
    await reloaded.async_ensure_loaded()
    stored = reloaded.document()["template"]["pages"][0]["blocks"][0]["cameras"]
    assert [c["label"] for c in stored] == ["Gate", "", ""]

    class States:
        def get(self, entity_id):
            return None

    class Registry:
        def door(self, device_id):
            return None

    wire = resolve_page(reloaded.document()["template"]["pages"][0], states=States(), registry=Registry())
    cams = wire["blocks"][0]["cameras"]
    assert [c["label"] for c in cams] == ["Gate", "", ""]             # "" on the wire, never the entity id
    assert all("label" in c and c["name"] == c["entity_id"] for c in cams[1:])


async def test_a_stored_camera_name_is_unchanged_by_the_change():
    page = normalize_page(_page([_blk(cameras=[_cam("c", "Front gate")])]))
    assert page["blocks"][0]["cameras"][0]["label"] == "Front gate"


# ---- the way back to a schema-3 build is the one-time copy, not a mirror -------------------------------------------

def _v3_loads(page_dict):
    return v3_loader._normalize_pages([page_dict], "copy")


async def test_the_schema_3_loader_reads_every_page_of_the_copy_the_first_save_leaves(hass, hass_storage):
    """R3: the real v3 validators, run over `portico.content.v3`, keep every page and every item. This copy is what a
    revert to a schema-3 build restores (the v4 file no longer carries a flat mirror, B8)."""
    v3 = live_v3()
    hass_storage["portico.content"] = {"version": 1, "minor_version": 1, "key": "portico.content", "data": copy.deepcopy(v3)}
    store = get_content_store(hass)
    await store.async_ensure_loaded()
    await store.async_set_home_name("Home 23")
    assert hass_storage["portico.content"]["data"]["schema_version"] == 4
    kept = hass_storage["portico.content.v3"]["data"]
    wanted = v3_pages(v3)
    got = {"template": v3_loader._normalize_pages(kept["template"]["pages"], "template"),
           "overrides": {k: v3_loader._normalize_pages(o["pages"], k) for k, o in kept["overrides"].items()}}
    assert got["template"] == wanted["template"]                     # same pages, same items, same order
    assert got["overrides"] == wanted["overrides"]
    assert sorted(got["overrides"]) == sorted(v3["overrides"])        # no page, no orphan dropped


async def test_the_v4_file_holds_blocks_only_every_new_kind_in_them_and_nothing_flat(hass, hass_storage):
    store = get_content_store(hass)
    await store.async_ensure_loaded()
    actions = [_act("old"), {"id": "ul", "label": "Unlock", "kind": "lock_unlock", "entity_id": "lock.a", "confirm": True},
               {"id": "tk", "label": "Talk", "kind": "talk", "confirm": False},
               {"id": "po", "label": "Open door", "kind": "portico_open", "confirm": True},
               {"id": "cv", "label": "Gate", "kind": "cover_open", "entity_id": "cover.g", "confirm": True}]
    doc = {"template": {"pages": [_page([_blk("b1", "One", [{**_cam(), "size": "big"}], actions, door="fake:gate-1",
                                              ), _blk("b2", "Two", [_cam("c2", "C2", "camera.c2")])], "home", "Home")],
                        "rules": [], "roster": ["*"]}}
    await store.async_save_document(doc, store.doc_rev)
    disk_page = hass_storage["portico.content"]["data"]["template"]["pages"][0]
    assert set(disk_page) == {"id", "label", "blocks"}
    assert [b["id"] for b in disk_page["blocks"]] == ["b1", "b2"] and len(disk_page["blocks"][0]["actions"]) == 5
    assert [a["kind"] for a in disk_page["blocks"][0]["actions"]] == ["lock_open", "lock_unlock", "talk", "portico_open", "cover_open"]


# ---- the store: loading, the first save, the copy ------------------------------------------------------------------

async def test_a_v3_file_is_read_in_memory_nothing_is_written_and_the_wire_serves_each_page_as_one_block(hass, hass_storage):
    v3 = live_v3()
    hass_storage["portico.content"] = {"version": 1, "minor_version": 1, "key": "portico.content", "data": copy.deepcopy(v3)}
    before = json.loads(json.dumps(hass_storage))
    store = get_content_store(hass)
    await store.async_ensure_loaded()
    assert json.loads(json.dumps(hass_storage)) == before               # nothing written, no copy yet
    assert store.document()["schema_version"] == 4
    assert flatten(store.document()) == v3_pages(v3)
    # the 'Not attached' override is kept
    assert store.orphan_keys() == ["ep-alice", "old-enrolment"]     # no phone is enrolled here: both are unattached
    # the wire serves the same items, names and availability as before, as one block per page and nothing flat
    hass.states.async_set("camera.gate", "idle", {"friendly_name": "Gate camera"})
    doc = store.resolve_document("ep-alice")
    page = doc["pages"][0]
    assert set(page) == {"id", "label", "blocks"} and page["label"] == "Alice's home"
    assert [b["id"] for b in page["blocks"]] == ["home"] and page["blocks"][0]["title"] == "Alice's home"
    (block,) = page["blocks"]
    assert [c["id"] for c in block["cameras"]] == ["gate"] and block["cameras"][0]["name"] == "Gate camera"
    assert [a["id"] for a in block["actions"]] == ["open-gate"]
    template_block = store.resolve_document("nobody")["pages"][0]["blocks"][0]
    assert [a["id"] for a in template_block["actions"]] == ["open-gate", "front", "porch", "bell", "evening", "call-dina"]
    assert all(len(p["blocks"]) == 1 for p in store.resolve_document("nobody")["pages"])


async def test_the_first_save_leaves_the_v3_copy_once_and_writes_v4_with_blocks_only(hass, hass_storage):
    v3 = live_v3()
    hass_storage["portico.content"] = {"version": 1, "minor_version": 1, "key": "portico.content", "data": copy.deepcopy(v3)}
    store = get_content_store(hass)
    await store.async_ensure_loaded()
    assert "portico.content.v3" not in hass_storage
    await store.async_set_home_name("Home 23")
    assert hass_storage["portico.content.v3"]["data"] == v3                # the file as it was, verbatim
    on_disk = hass_storage["portico.content"]["data"]
    assert on_disk["schema_version"] == 4 and on_disk["home_name"] == "Home 23"
    page = on_disk["template"]["pages"][0]
    assert set(page) == {"id", "label", "blocks"}                         # no flat mirror (B8)
    assert page["blocks"][0]["cameras"] == v3["template"]["pages"][0]["cameras"]
    assert page["blocks"][0]["actions"] == v3["template"]["pages"][0]["actions"]
    assert on_disk["dashboards"] == v3["dashboards"] and on_disk["endpoints"] == v3["endpoints"]
    assert on_disk["overrides"]["old-enrolment"]["rules"] == v3["overrides"]["old-enrolment"]["rules"]
    hass_storage["portico.content.v3"]["data"]["home_name"] = "marker"
    await store.async_set_home_name("Home 24")                             # a later save does not touch the copy
    assert hass_storage["portico.content.v3"]["data"]["home_name"] == "marker"
    # a restart: the v4 file reads back to the same document, and nothing more is copied
    again = ContentStore(hass)
    await again.async_ensure_loaded()
    assert again._data() == store._data() and again.doc_rev == store.doc_rev
    assert all(set(p) == {"id", "label", "blocks"} for p in again.template["pages"])
    assert "portico.content.v4" not in hass_storage


async def test_a_failed_v3_copy_warns_and_does_not_block_the_save(hass, hass_storage, caplog):
    from unittest.mock import patch
    from homeassistant.helpers.storage import Store
    hass_storage["portico.content"] = {"version": 1, "minor_version": 1, "key": "portico.content", "data": live_v3()}
    store = get_content_store(hass)
    await store.async_ensure_loaded()
    real = Store.async_save

    async def save(self, data):
        if self.key.endswith(".v3"):
            raise OSError("disk full")
        return await real(self, data)

    with patch.object(Store, "async_save", save), caplog.at_level("WARNING"):
        await store.async_set_home_name("X")
    assert "could not keep a copy of the v3 file" in caplog.text
    assert hass_storage["portico.content"]["data"]["schema_version"] == 4


async def test_a_flat_mirror_an_older_build_wrote_is_ignored_and_gone_at_the_next_save(hass, hass_storage):
    """The files written between B1 and B8 carry `cameras`/`actions` beside `blocks`. They are never read, never part of
    `doc_rev`, and the next save writes the page without them."""
    hass_storage["portico.content"] = {"version": 1, "minor_version": 1, "key": "portico.content", "data": live_v3()}
    store = get_content_store(hass)
    await store.async_ensure_loaded()
    await store.async_set_home_name("X")
    rev = store.doc_rev
    tampered = json.loads(json.dumps(hass_storage["portico.content"]))
    for page in tampered["data"]["template"]["pages"]:
        page["cameras"] = [_cam("injected", "Injected", "camera.injected")]
        page["actions"] = []
    hass_storage["portico.content"] = tampered
    again = ContentStore(hass)
    await again.async_ensure_loaded()
    assert again.doc_rev == rev and "injected" not in json.dumps(again._data())
    assert "cameras" not in again.template["pages"][0]
    await again.async_set_home_name("Y")
    assert all(set(p) == {"id", "label", "blocks"}
               for p in hass_storage["portico.content"]["data"]["template"]["pages"])
    assert again._disk_data() == again._data()


async def test_doc_rev_changes_at_the_migration_so_a_tab_open_across_the_restart_gets_a_conflict(hass, hass_storage):
    v3 = live_v3()
    hass_storage["portico.content"] = {"version": 1, "minor_version": 1, "key": "portico.content", "data": copy.deepcopy(v3)}
    store = get_content_store(hass)
    await store.async_ensure_loaded()
    old_rev = doc_rev(v3)                                                  # what the v3 build's content/document gave
    assert store.doc_rev != old_rev
    with pytest.raises(SaveConflict):
        await store.async_save_document(store.document(), old_rev)
    assert "portico.content.v3" not in hass_storage                         # a refused save leaves no copy


async def test_a_v1_and_a_v2_file_chain_through_to_v4_and_keep_their_old_copy(hass, hass_storage):
    v2 = {"schema_version": 2, "home_name": "H", "template": {"pages": [{"id": "g", "label": "G", "cameras": [_cam()],
                                                                         "actions": [_act("a")]}], "rules": [],
                                                              "roster": []},
          "overrides": {"enr": {"pages": [{"id": "home", "cameras": [], "actions": [_act("b")]}], "rules": [],
                                "roster_hide": []}}, "dashboards": {}}
    hass_storage["portico.content"] = {"version": 1, "minor_version": 1, "key": "portico.content", "data": copy.deepcopy(v2)}
    store = get_content_store(hass)
    await store.async_ensure_loaded()
    assert [p["blocks"][0]["id"] for p in store.template["pages"]] == ["g"]
    assert store.overrides["enr"]["pages"][0]["blocks"][0] == {"id": "home", "title": "Home", "cameras": [],
                                                                "actions": [_act("b")]}
    await store.async_set_home_name("H2")
    assert hass_storage["portico.content.v2"]["data"] == v2 and "portico.content.v3" not in hass_storage
    assert hass_storage["portico.content"]["data"]["schema_version"] == 4


# ---- the document the panel reads ----------------------------------------------------------------------------------

async def test_the_document_the_panel_reads_is_blocks_only_and_saving_it_back_is_a_no_op_and_a_flat_edit_is_refused(hass, hass_storage):
    v3 = live_v3()
    hass_storage["portico.content"] = {"version": 1, "minor_version": 1, "key": "portico.content", "data": copy.deepcopy(v3)}
    store = get_content_store(hass)
    await store.async_ensure_loaded()
    doc = store.document()
    assert doc["schema_version"] == 4
    assert flatten(doc) == v3_pages(v3)                                        # nothing of the old document is lost
    assert all(set(p) == {"id", "label", "blocks"} and p["blocks"] for p in doc["template"]["pages"])
    rev = store.doc_rev
    assert await store.async_save_document(doc, rev) == rev                  # unedited: nothing is written
    assert "portico.content.v3" not in hass_storage
    flat_edit = copy.deepcopy(doc)
    flat_edit["template"]["pages"][0]["cameras"] = [_cam("new", "New", "camera.new")]
    with pytest.raises(ProfileError, match="update the Portico panel"):
        await store.async_save_document(flat_edit, rev)
    assert store.doc_rev == rev and "portico.content.v3" not in hass_storage
    doc["template"]["pages"][0]["blocks"][0]["cameras"].append(_cam("new", "New", "camera.new"))
    new_rev = await store.async_save_document(doc, rev)
    assert new_rev != rev
    block = store.template["pages"][0]["blocks"][0]
    assert block["id"] == "home" and [c["id"] for c in block["cameras"]] == ["gate", "lobby", "new"]


# ---- running --------------------------------------------------------------------------------------------------------

async def _store_with(hass, hass_storage, *blocks_and_door):
    """A store holding one override for the user id 'u1' (an HA user with no enrolled phone is keyed by its id)."""
    data = {"schema_version": 4, "template": {"pages": [], "rules": [], "roster": ["*"]},
            "overrides": {"u1": {"pages": [_page(list(blocks_and_door), "home", "Home")], "rules": [], "roster_hide": []}},
            "dashboards": {}, "endpoints": {}}
    hass_storage["portico.content"] = {"version": 1, "minor_version": 1, "key": "portico.content", "data": data}
    store = get_content_store(hass)
    await store.async_ensure_loaded()
    return store


@pytest.mark.parametrize("kind,entity,service,domain", [
    ("lock_lock", "lock.front", "lock", "lock"), ("lock_unlock", "lock.front", "unlock", "lock"),
    ("cover_open", "cover.gate", "open_cover", "cover"), ("cover_close", "cover.gate", "close_cover", "cover")])
async def test_the_new_service_kinds_run_with_the_callers_user_and_every_block_is_in_the_grant(
        hass, hass_storage, kind, entity, service, domain):
    calls = async_mock_service(hass, domain, service)
    hass.states.async_set(entity, "locked" if domain == "lock" else "closed")
    action = {"id": "x", "label": "X", "kind": kind, "entity_id": entity, "confirm": KINDS[kind][2]}
    store = await _store_with(hass, hass_storage, _blk("b1", "One", actions=[_act("first", "lock_open", "lock.a")]),
                              _blk("b2", "Two", actions=[action]))
    assert [a["id"] for a in store._all_actions("u1")] == ["first", "x"]          # the second block's button is granted
    assert await store.async_run("u1", "Phone", "x", caller_user_id="the-caller") == {}
    assert len(calls) == 1 and calls[0].data == {"entity_id": entity} and calls[0].context.user_id == "the-caller"
    store._last_run.clear()
    with pytest.raises(RunRefused) as refused:
        await store.async_run("u1", "Phone", "not-there")
    assert refused.value.code == "unknown_action"


async def test_a_new_service_kind_on_an_unavailable_entity_is_refused_and_calls_nothing(hass, hass_storage):
    calls = async_mock_service(hass, "cover", "open_cover")
    store = await _store_with(hass, hass_storage, _blk(actions=[
        {"id": "x", "label": "X", "kind": "cover_open", "entity_id": "cover.gate", "confirm": True}]))
    with pytest.raises(RunRefused) as refused:
        await store.async_run("u1", "Phone", "x")
    assert refused.value.code == "unavailable" and calls == []


async def test_portico_open_in_a_block_with_a_door_opens_that_door(hass, hass_storage):
    fake = _provide(hass, "gate-1", name="Gate")
    store = await _store_with(hass, hass_storage, _blk(door="fake:gate-1", actions=[
        {"id": "door", "label": "Open the door", "kind": "portico_open", "confirm": True}]))
    await store.async_run("u1", "Phone", "door")
    assert fake.calls_to("open_relay") == [(("gate-1",), {})]
    resolved = store.resolve_document("u1")
    door = resolved["pages"][0]["blocks"][0]["actions"][0]
    assert door["device_id"] == "fake:gate-1" and door["available"]


async def test_talk_is_in_the_grant_but_refused_and_nothing_is_dialled(hass, hass_storage):
    fake = _provide(hass, "gate-1", name="Gate")
    store = await _store_with(hass, hass_storage, _blk(door="fake:gate-1", actions=[
        {"id": "talk", "label": "Talk", "kind": "talk", "confirm": False}]))
    assert [a["id"] for a in store._all_actions("u1")] == ["talk"]            # in the grant: the id is known
    with pytest.raises(RunRefused) as refused:           # "u1" is no enrolled phone endpoint and names no caller
        await store.async_run("u1", "Phone", "talk")
    assert refused.value.code == "not_allowed"
    with pytest.raises(RunRefused) as refused:
        await store.async_run("u1", "Phone", "talk", caller_user_id="u1")
    assert refused.value.code == "not_allowed"            # still no phone endpoint behind the id (B7a's gate, test_talk_gate.py)
    assert fake.calls_to("dial") == [] and fake.calls_to("open_relay") == []
    assert store._action_available({"kind": "talk", "id": "talk"}) is False
    # and the wire carries it in the block, off, phone_only for a user that is no phone (the flat form that left it out is gone, B8)
    (talk,) = store.resolve_document("u1")["pages"][0]["blocks"][0]["actions"]
    assert talk["id"] == "talk" and talk["available"] is False and talk["reason"] == "phone_only"


async def test_the_snapshot_grant_covers_the_cameras_of_every_block(hass, hass_storage):
    store = await _store_with(hass, hass_storage, _blk("b1", "One", cameras=[_cam("c1")]),
                              _blk("b2", "Two", cameras=[_cam("c2", "C2", "camera.c2")]))
    assert [c["id"] for c in store._all_cameras("u1")] == ["c1", "c2"]


# ---- the Configure flow (until E7 removes it) ---------------------------------------------------------------------

async def test_the_configure_save_refuses_a_page_of_several_blocks_and_keeps_a_single_blocks_identity(hass, hass_storage):
    store = await _store_with(hass, hass_storage, _blk("b1", "Front", cameras=[_cam("c1")], door="fake:gate-1"))
    profile = store.profile_for("u1")
    profile["cameras"].append(_cam("c2", "C2", "camera.c2"))
    await store.async_set("u1", profile)
    (block,) = store.overrides["u1"]["pages"][0]["blocks"]
    assert block["id"] == "b1" and block["title"] == "Front" and block["door"] == "fake:gate-1"
    assert [c["id"] for c in block["cameras"]] == ["c1", "c2"]
    # a user with no page yet gets a first block named after the page
    await store.async_set("u2", {"cameras": [_cam()], "actions": []})
    (fresh,) = store.overrides["u2"]["pages"][0]["blocks"]
    assert fresh["id"] == "home" and fresh["title"] == "Home"


async def test_the_configure_save_never_collapses_a_multi_block_home_page(hass, hass_storage):
    store = await _store_with(hass, hass_storage, _blk("b1", "One"), _blk("b2", "Two", cameras=[_cam("c9")]))
    before = copy.deepcopy(store.overrides)
    with pytest.raises(ProfileError, match="edit it in the Portico editor"):
        await store.async_set("u1", {"cameras": [], "actions": []})
    assert store.overrides == before and "portico.content.v3" not in hass_storage     # a refusal leaves no drift


async def test_the_home_profile_reads_through_the_blocks(hass, hass_storage):
    store = await _store_with(hass, hass_storage,
                              _blk("b1", "One", [_cam("c1")], [_act("a1")]),
                              _blk("b2", "Two", [_cam("c2", "C2", "camera.c2")],
                                   [{"id": "ul", "label": "U", "kind": "lock_unlock", "entity_id": "lock.u", "confirm": True}]))
    assert [c["id"] for c in store.profile_for("u1")["cameras"]] == ["c1", "c2"]
    assert [a["id"] for a in store.profile_for("u1")["actions"]] == ["a1", "ul"]               # every kind
