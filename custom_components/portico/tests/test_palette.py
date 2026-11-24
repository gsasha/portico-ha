"""The button palette and the icon list of Portico blocks (DEV-658 B1, design 3.4).

`contract/palette_v1.json` carries the eight colours; `content.PALETTE_NAMES` is the list the validator accepts, and a
test keeps the two equal. Every contrast ratio is recomputed from the JSON (WCAG 2.x relative luminance), so a later
edit to a value cannot slip under 4.5:1 for text on its fill or 3:1 for the fill against the page, in light and in dark.
`contract/icons_v1.json` and `ICON_NAMES` are kept equal too (the older test is in test_content.py; this one pins the
19 names blocks added and the total).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from ..content import ICON_NAMES, PALETTE_NAMES

CONTRACT = Path(__file__).resolve().parents[3] / "contract"
HEX = re.compile(r"^#[0-9A-F]{6}$")

PALETTE = json.loads((CONTRACT / "palette_v1.json").read_text())
PAGES = PALETTE["pages"]


def _luminance(color: str) -> float:
    channels = [int(color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def contrast(a: str, b: str) -> float:
    la, lb = _luminance(a), _luminance(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


def test_the_palette_has_eight_named_colours_in_the_order_the_validator_lists_them():
    names = [c["name"] for c in PALETTE["colors"]]
    assert len(names) == 8 and len(set(names)) == 8
    assert tuple(names) == PALETTE_NAMES
    assert PALETTE["version"] == 1
    assert all(re.fullmatch(r"[a-z]+", n) for n in names)


def test_every_colour_is_an_uppercase_six_digit_hex_and_the_pages_are_the_ones_the_design_names():
    assert PAGES == {"light": "#FFFFFF", "dark": "#121212", "dark_card": "#1C1C1C"}
    for entry in PALETTE["colors"]:
        assert set(entry) == {"name", "light", "dark"}
        for theme in ("light", "dark"):
            assert set(entry[theme]) == {"fill", "text"}
            assert HEX.match(entry[theme]["fill"]) and HEX.match(entry[theme]["text"]), entry


def test_the_contrast_helper_matches_known_wcag_values():
    assert contrast("#000000", "#FFFFFF") == pytest.approx(21.0)
    assert contrast("#777777", "#FFFFFF") == pytest.approx(4.48, abs=0.01)
    assert contrast("#FFFFFF", "#FFFFFF") == pytest.approx(1.0)


@pytest.mark.parametrize("entry", PALETTE["colors"], ids=lambda e: e["name"])
def test_text_on_its_fill_is_at_least_4_5_in_light_and_in_dark(entry):
    assert contrast(entry["light"]["text"], entry["light"]["fill"]) >= 4.5
    assert contrast(entry["dark"]["text"], entry["dark"]["fill"]) >= 4.5


@pytest.mark.parametrize("entry", PALETTE["colors"], ids=lambda e: e["name"])
def test_the_fill_stands_out_from_the_page_by_3_to_1_in_light_dark_and_the_dark_card(entry):
    assert contrast(entry["light"]["fill"], PAGES["light"]) >= 3.0
    assert contrast(entry["dark"]["fill"], PAGES["dark"]) >= 3.0
    assert contrast(entry["dark"]["fill"], PAGES["dark_card"]) >= 3.0


def test_the_design_table_is_what_the_json_computes():
    """Section 3.4's ratios, one decimal, recomputed: a value edited without the design following fails here."""
    wanted = {  # name: (text:fill light, text:fill dark, fill:page light, fill:page dark)
        "blue": (5.7, 8.1, 5.7, 10.7), "green": (5.1, 7.8, 5.1, 11.4), "amber": (5.9, 9.7, 5.9, 13.3),
        "red": (5.6, 6.5, 5.6, 8.7), "purple": (9.4, 6.4, 9.4, 7.8), "teal": (5.3, 7.4, 5.3, 10.0),
        "pink": (7.0, 6.7, 7.0, 8.4), "grey": (5.4, 7.7, 5.4, 9.8),
    }
    for entry in PALETTE["colors"]:
        got = (contrast(entry["light"]["text"], entry["light"]["fill"]), contrast(entry["dark"]["text"], entry["dark"]["fill"]),
               contrast(entry["light"]["fill"], PAGES["light"]), contrast(entry["dark"]["fill"], PAGES["dark"]))
        assert tuple(round(g, 1) for g in got) == wanted[entry["name"]], entry["name"]


def test_a_mutated_palette_is_caught_by_the_contrast_rules():
    """The check has teeth: pale text on the amber fill, and a fill nearly the page's colour, both fail."""
    amber = next(c for c in PALETTE["colors"] if c["name"] == "amber")
    assert contrast("#FFD54F", amber["light"]["fill"]) < 4.5
    assert contrast("#F0F0F0", PAGES["light"]) < 3.0


def test_the_icon_list_is_the_json_list_with_the_19_names_blocks_added():
    contract = json.loads((CONTRACT / "icons_v1.json").read_text())
    assert list(ICON_NAMES) == contract["icons"]
    assert len(ICON_NAMES) == 118 and len(set(ICON_NAMES)) == 118
    added = ["door-sliding", "door-sliding-lock", "door-sliding-open", "gate-arrow-left", "gate-arrow-right",
             "boom-gate", "boom-gate-up", "garage-variant", "garage-open-variant", "garage-lock", "lock-smart",
             "lock-check", "lock-clock", "lock-alert", "lock-open-check", "account-voice", "microphone-off",
             "phone-cancel", "doorbell-video"]
    assert list(ICON_NAMES[99:]) == added                  # appended: the order of the first 99 is unchanged
    assert "intercom" not in ICON_NAMES and "boom-gate-down" not in ICON_NAMES      # not in @mdi/svg 7.4.47
