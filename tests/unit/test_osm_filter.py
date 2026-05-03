"""OSM scope preset validation (v0.1.5).

Covers app/osm_filter.py — the single source of truth for the build-time
filter expressions. The shell entrypoint hardcodes the same filter args
in case statements; if you change one, change both.
"""

from __future__ import annotations

import pytest

# ─────────────────────── validate_scope ───────────────────────


class TestValidateScope:
    def test_none_returns_default(self):
        from app.osm_filter import DEFAULT_SCOPE, validate_scope

        # Legacy sessions (created before v0.1.5) have no osm_scope key.
        # We default them to transit-focused — same memory footprint as
        # the v0.1.5 default, so behaviour stays consistent.
        assert validate_scope(None) == DEFAULT_SCOPE
        assert validate_scope("") == DEFAULT_SCOPE

    def test_valid_scopes_pass_through(self):
        from app.osm_filter import VALID_SCOPES, validate_scope

        for scope in VALID_SCOPES:
            assert validate_scope(scope) == scope

    def test_unknown_scope_raises(self):
        from app.osm_filter import validate_scope

        with pytest.raises(ValueError, match="not recognised"):
            validate_scope("super-aggressive")

    def test_non_string_raises(self):
        from app.osm_filter import validate_scope

        with pytest.raises(ValueError, match="must be a string"):
            validate_scope(42)
        with pytest.raises(ValueError, match="must be a string"):
            validate_scope(["transit-focused"])


# ─────────────────────── preset shape ───────────────────────


class TestPresetShape:
    """Defensive checks on the preset dict — these are the single source
    of truth for what the entrypoint shell does, so we want noisy CI
    failures if the structure ever drifts."""

    def test_three_scopes_exist(self):
        from app.osm_filter import OSM_SCOPE_PRESETS

        assert set(OSM_SCOPE_PRESETS.keys()) == {
            "transit-focused",
            "multi-modal",
            "comprehensive",
        }

    def test_each_preset_has_label_description_tags(self):
        from app.osm_filter import OSM_SCOPE_PRESETS

        for scope, preset in OSM_SCOPE_PRESETS.items():
            assert "label" in preset, f"{scope} missing label"
            assert "description" in preset, f"{scope} missing description"
            assert "tags" in preset, f"{scope} missing tags"
            assert isinstance(preset["label"], str)
            assert isinstance(preset["description"], str)

    def test_comprehensive_has_no_tags(self):
        """comprehensive is the no-filter sentinel — must explicitly be None
        so the entrypoint's `*) skip filter` branch fires."""
        from app.osm_filter import OSM_SCOPE_PRESETS

        assert OSM_SCOPE_PRESETS["comprehensive"]["tags"] is None

    def test_filter_scopes_have_railway_and_public_transport(self):
        """Both filter scopes MUST keep railway and public_transport — they're
        what makes the filter "transit-friendly". A regression that drops
        them would silently break OTP routing on station coordinates."""
        from app.osm_filter import OSM_SCOPE_PRESETS

        for scope in ("transit-focused", "multi-modal"):
            tags = OSM_SCOPE_PRESETS[scope]["tags"]
            assert any("railway" in t for t in tags), f"{scope} missing railway"
            assert any("public_transport" in t for t in tags), f"{scope} missing public_transport"

    def test_transit_focused_drops_service_roads(self):
        """transit-focused's headline saving is dropping highway=service.
        The filter expression's highway= list should NOT include 'service'."""
        from app.osm_filter import OSM_SCOPE_PRESETS

        tags = OSM_SCOPE_PRESETS["transit-focused"]["tags"]
        highway_filters = [t for t in tags if t.startswith("highway=")]
        # Find the one that's a value list, not 'highway=bus_stop'.
        value_list = next(
            (t for t in highway_filters if "," in t),
            None,
        )
        assert value_list is not None
        # Split into individual values: "highway=motorway,trunk,..." → set
        values = set(value_list.split("=", 1)[1].split(","))
        assert (
            "service" not in values
        ), "transit-focused should drop highway=service for the memory win"
        assert "track" not in values, "transit-focused should drop highway=track (agricultural)"
        # Ensure we DO keep the things that matter.
        assert "footway" in values, "footway is needed for stop access"
        assert "residential" in values, "residential is needed for transit access"
        assert "primary" in values, "primary is needed for bus routing"

    def test_multi_modal_keeps_all_highway(self):
        """multi-modal keeps the broad 'highway' tag (no value filter), so
        service/track/etc. are included."""
        from app.osm_filter import OSM_SCOPE_PRESETS

        tags = OSM_SCOPE_PRESETS["multi-modal"]["tags"]
        # Must have a bare 'highway' (no =) to match all values.
        assert "highway" in tags, "multi-modal should keep ALL highway types"


# ─────────────────────── osmium_args / scope_label ───────────────────────


def test_osmium_args_for_comprehensive_is_none():
    """Caller can use this as a no-filter sentinel."""
    from app.osm_filter import osmium_args

    assert osmium_args("comprehensive") is None


def test_osmium_args_for_transit_returns_list():
    from app.osm_filter import osmium_args

    args = osmium_args("transit-focused")
    assert args is not None
    assert isinstance(args, list)
    assert len(args) >= 4  # we have at least highway, railway, public_transport, parking


def test_scope_label_friendly():
    from app.osm_filter import scope_label

    # Just confirm it's not the raw scope identifier — operators should
    # see something readable.
    label = scope_label("transit-focused")
    assert "transit" in label.lower()
    assert label != "transit-focused"
