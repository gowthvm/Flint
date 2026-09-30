"""Tests for ui/style.py - palettes, design tokens and stylesheet output.

Guards the C08/C12/C14 cleanups:
- every token referenced by _QSS_TEMPLATE exists in every palette (no
  dead tokens like the removed `menuHover`, no unresolved placeholders)
- DESIGN_TOKENS matches the radii/sizes documented in SOURCE_OF_TRUTH
  and the literal radius/font-size/padding substitutions stay out of
  the template
- disabled toggles render with explicit colors (Qt ignores `opacity`
  on widget backgrounds, so C14 replaced it with palette swaps)
"""

import re

import pytest

from ui import style

THEMES = ("dark", "light", "high-contrast")


@pytest.mark.parametrize("theme", THEMES)
def test_build_style_contains_key_color_literals(theme):
    """build_style() output must carry the theme's key palette colors."""
    qss = style.build_style(theme)
    palette = style._PALETTES[theme]
    for key in ("bg", "card", "border", "text", "muted", "track", "primary"):
        value = palette[key]
        assert value in qss, f"{theme}: {key}={value} missing from stylesheet"


@pytest.mark.parametrize("theme", THEMES)
def test_no_unresolved_placeholders(theme):
    """No @palette or $token placeholder may survive build_style()."""
    qss = style.build_style(theme)
    leftovers = re.findall(r"[@$][A-Za-z_][A-Za-z0-9_]*", qss)
    assert leftovers == [], f"{theme}: unresolved placeholders {leftovers}"


@pytest.mark.parametrize("theme", THEMES)
def test_every_template_token_defined_in_every_palette(theme):
    """Every @name referenced by the template exists in every palette."""
    refs = set(re.findall(r"@([A-Za-z0-9_]+)", style._QSS_TEMPLATE))
    missing = refs - set(style._PALETTES[theme])
    assert missing == set(), f"{theme}: template references undefined {sorted(missing)}"


@pytest.mark.parametrize("theme", THEMES)
def test_no_dead_palette_tokens(theme):
    """Every palette key is referenced by the template (catches dead
    tokens like the removed `menuHover`)."""
    refs = set(re.findall(r"@([A-Za-z0-9_]+)", style._QSS_TEMPLATE))
    dead = set(style._PALETTES[theme]) - refs
    assert dead == set(), f"{theme}: unreferenced palette keys {sorted(dead)}"


def test_palette_keys_match_across_themes():
    """All three palettes define exactly the same key set."""
    key_sets = {t: set(style._PALETTES[t]) for t in THEMES}
    assert key_sets["dark"] == key_sets["light"] == key_sets["high-contrast"]


def test_template_token_refs_are_unambiguous():
    """No DESIGN_TOKENS key may be a prefix of another: build_style()
    substitutes with a plain str.replace, so a prefix collision (e.g.
    `radius_s` vs `radius_sm`) would corrupt values."""
    keys = list(style.DESIGN_TOKENS)
    for i, a in enumerate(keys):
        for b in keys:
            if a != b and b.startswith(a):
                pytest.fail(f"token prefix collision: {a!r} prefixes {b!r}")


def test_design_tokens_match_documented_values():
    """DESIGN_TOKENS values must match SOURCE_OF_TRUTH/ui-style.md 4.1/4.3
    plus the C12 tokenizations (no drift between doc and code)."""
    expected = {
        "space_xs": 4, "space_sm": 8, "space_md": 12, "space_lg": 16, "space_xl": 20,
        "radius_sm": 4, "radius_md": 6, "radius_lg": 8, "radius_btn": 7,
        "radius_xs": 3, "radius_seg": 5, "radius_help": 9, "radius_frame": 10,
        "radius_pill": 10, "radius_dialog": 12,
        "icon_small": 16, "icon_medium": 22, "icon_large": 38,
        "toggle_w": 28, "toggle_h": 16, "toggle_knob": 12,
        "btn_pad_v": 9, "btn_pad_h": 16, "btn_primary_pad": 10,
        "combo_pad_v": 4, "combo_pad_h": 10,
        "badge_pad_v": 2, "badge_pad_h": 6,
        "chip_pad_v": 3, "chip_pad_h": 8,
        "seg_pad_v": 7, "caplabel_pad_b": 2,
        "progress_h": 4,
        "font_xs": 10, "font_sm": 11, "font_base": 13, "font_md": 14,
        "font_lg": 16, "font_xl": 22,
        "font_12": 12, "font_15": 15, "font_24": 24, "font_hidden": 1,
        "button_height": 36, "chamfer": 8,
    }
    assert style.DESIGN_TOKENS == expected


def test_template_has_no_literal_metrics():
    """C12: radii, font-sizes and paddings in the template come from
    DESIGN_TOKENS, never from raw pixel literals."""
    tpl = style._QSS_TEMPLATE
    assert not re.search(r"border-radius:\s*\d+px", tpl), "literal border-radius"
    assert not re.search(r"font-size:\s*\d+px", tpl), "literal font-size"
    assert not re.search(r"padding(?:-\w+)?:\s*[^;]*\d+px", tpl), "literal padding"


def test_all_template_metric_placeholders_are_defined_tokens():
    """Every $token used in the template exists in DESIGN_TOKENS."""
    refs = set(re.findall(r"\$([A-Za-z0-9_]+)", style._QSS_TEMPLATE))
    refs.discard("font_mono")  # substituted from FONT_MONO, not DESIGN_TOKENS
    missing = refs - set(style.DESIGN_TOKENS)
    assert missing == set(), f"undefined template tokens {sorted(missing)}"


def test_disabled_toggle_uses_explicit_colors_not_opacity():
    """C14 regression guard: Qt ignores `opacity` on widget backgrounds,
    so disabled toggles must be colored explicitly."""
    for theme in THEMES:
        qss = style.build_style(theme)
        assert "opacity: 0.4" not in qss, theme
        assert "opacity: 0.3" not in qss, theme
        assert "QWidget#toggleTrack:disabled" in qss, theme
        assert "#toggleKnob:disabled" in qss, theme
        palette = style._PALETTES[theme]
        # disabled ON track = @muted, disabled OFF = @card, knob = @faded
        assert palette["muted"] in qss, theme
        assert palette["faded"] in qss, theme


def test_disabled_pseudo_sits_on_final_compound():
    """Qt ignores pseudo-states on non-final selector compounds (they
    match unconditionally), so :disabled/:hover/:focus must sit on the
    last compound of a rule. Comments are stripped first; the one known
    shipped exception is allowlisted below."""
    tpl = re.sub(r"/\*.*?\*/", "", style._QSS_TEMPLATE, flags=re.DOTALL)
    allowlist = {
        # Documented shipped behavior (comment in ui/style.py): Qt draws
        # this muted border unconditionally today; "fixing" it would be a
        # visual change, out of scope for this cleanup.
        "QWidget#toggleSwitch:focus QLabel#toggleTrack",
    }
    for rule in tpl.split("}"):
        if "{" not in rule:
            continue
        for sel in rule.split("{")[0].split(","):
            sel = sel.strip()
            if not sel or sel in allowlist:
                continue
            compounds = sel.split()
            for comp in compounds[:-1]:
                if ":disabled" in comp or ":hover" in comp or ":focus" in comp:
                    pytest.fail(
                        f"pseudo-state on non-final compound in {sel!r} "
                        f"(Qt matches it unconditionally)"
                    )


def test_build_style_themes_differ():
    """Each theme produces a distinct stylesheet."""
    outputs = {t: style.build_style(t) for t in THEMES}
    assert outputs["dark"] != outputs["light"]
    assert outputs["dark"] != outputs["high-contrast"]


def test_build_style_is_deterministic():
    """build_style() output depends only on the requested theme, so
    repeated/cross-order calls cannot leak state into each other."""
    for _ in range(2):
        dark1 = style.build_style("dark")
        style.build_style("light")
        dark2 = style.build_style("dark")
        assert dark1 == dark2
    assert style.palette_for(None) == style._PALETTES["dark"]
