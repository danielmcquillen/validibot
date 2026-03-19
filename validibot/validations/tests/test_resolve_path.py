"""
Comprehensive tests for data path resolution.

Exercises dotted-path and bracket-notation resolution across all
implementations used in the codebase:

1. resolve_path (shared function in validations.services.path_resolution)
2. BaseValidator._resolve_path (thin wrapper, used for CEL context building)
3. BasicAssertionEvaluator._resolve_path (thin wrapper, BASIC eval)
All three delegate to the shared resolve_path() function.
The parametrized fixture runs each test against all three to ensure
consistent behaviour.

Covers: top-level keys, nested objects, array indexing, mixed paths,
edge cases, and deeply nested structures.
"""

import pytest

from validibot.validations.assertions.evaluators.basic import BasicAssertionEvaluator
from validibot.validations.services.path_resolution import resolve_path
from validibot.validations.validators.base.base import BaseValidator


class _StubValidator(BaseValidator):
    """Minimal concrete subclass for testing _resolve_path."""

    def validate(self, *args, **kwargs):
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Fixtures: instantiate resolvers without requiring Django models
# ---------------------------------------------------------------------------


@pytest.fixture
def shared_resolve():
    """Return the shared resolve_path function directly."""
    return resolve_path


@pytest.fixture
def base_resolve():
    """Return BaseValidator._resolve_path bound to a throwaway instance."""
    instance = object.__new__(_StubValidator)
    return instance._resolve_path


@pytest.fixture
def basic_resolve():
    """Return BasicAssertionEvaluator._resolve_path bound to a throwaway instance."""
    instance = object.__new__(BasicAssertionEvaluator)
    return instance._resolve_path


# ---------------------------------------------------------------------------
# Shared test data
# ---------------------------------------------------------------------------

FLAT_PAYLOAD = {
    "sku": "ABCD1234",
    "name": "Widget Mini",
    "price": 20.00,
    "rating": 95,
    "in_stock": True,
}

NESTED_PAYLOAD = {
    "building": {
        "envelope": {
            "wall": {
                "u_value": 0.35,
                "area": 120.5,
            },
        },
        "name": "Test Building",
    },
    "dimensions": {
        "width": 3.5,
        "height": 1.2,
    },
}

ARRAY_PAYLOAD = {
    "tags": ["gadgets", "mini", "electronics"],
    "results": [
        {"zone": "North", "temp": 21.3},
        {"zone": "South", "temp": 23.1},
        {"zone": "East", "temp": 22.0},
    ],
}

MIXED_PAYLOAD = {
    "building": {
        "floors": [
            {
                "name": "Ground",
                "zones": [
                    {"id": "zone-1", "temp": 20.5, "sensors": [100, 200, 300]},
                    {"id": "zone-2", "temp": 22.1, "sensors": [400, 500]},
                ],
            },
            {
                "name": "First",
                "zones": [
                    {"id": "zone-3", "temp": 19.8, "sensors": [600]},
                ],
            },
        ],
    },
}


# ===================================================================
# Tests for resolve_path, BaseValidator._resolve_path, and
# BasicAssertionEvaluator._resolve_path
# ===================================================================
# We parametrise over all three implementations to ensure consistent behaviour.


@pytest.fixture(params=["shared", "base", "basic"])
def resolve(request, shared_resolve, base_resolve, basic_resolve):
    """Parametrised fixture: runs each test against all three implementations.

    This ensures the shared resolve_path() function and both thin wrappers
    produce identical results for every test case.
    """
    if request.param == "shared":
        return shared_resolve
    if request.param == "base":
        return base_resolve
    return basic_resolve


# ---- Top-level fields ----


class TestTopLevelFields:
    """Resolve top-level keys in a flat dict."""

    def test_string_value(self, resolve):
        val, found = resolve(FLAT_PAYLOAD, "sku")
        assert found is True
        assert val == "ABCD1234"

    def test_numeric_value(self, resolve):
        val, found = resolve(FLAT_PAYLOAD, "price")
        assert found is True
        assert val == 20.00  # noqa: PLR2004

    def test_integer_value(self, resolve):
        val, found = resolve(FLAT_PAYLOAD, "rating")
        assert found is True
        assert val == 95  # noqa: PLR2004

    def test_boolean_value(self, resolve):
        val, found = resolve(FLAT_PAYLOAD, "in_stock")
        assert found is True
        assert val is True

    def test_missing_key(self, resolve):
        val, found = resolve(FLAT_PAYLOAD, "nonexistent")
        assert found is False
        assert val is None


# ---- Nested objects ----


class TestNestedObjects:
    """Resolve dotted paths into nested dicts."""

    def test_one_level_deep(self, resolve):
        val, found = resolve(NESTED_PAYLOAD, "dimensions.width")
        assert found is True
        assert val == 3.5  # noqa: PLR2004

    def test_two_levels_deep(self, resolve):
        val, found = resolve(NESTED_PAYLOAD, "building.envelope.wall.u_value")
        assert found is True
        assert val == 0.35  # noqa: PLR2004

    def test_three_levels_deep(self, resolve):
        val, found = resolve(NESTED_PAYLOAD, "building.envelope.wall.area")
        assert found is True
        assert val == 120.5  # noqa: PLR2004

    def test_intermediate_dict_value(self, resolve):
        """Resolving to an intermediate node returns the sub-dict."""
        val, found = resolve(NESTED_PAYLOAD, "building.envelope.wall")
        assert found is True
        assert val == {"u_value": 0.35, "area": 120.5}

    def test_missing_intermediate_key(self, resolve):
        val, found = resolve(NESTED_PAYLOAD, "building.nonexistent.wall")
        assert found is False

    def test_missing_leaf_key(self, resolve):
        val, found = resolve(NESTED_PAYLOAD, "building.envelope.wall.color")
        assert found is False


# ---- Array access ----


class TestArrayAccess:
    """Resolve bracket notation for array elements."""

    def test_first_element(self, resolve):
        val, found = resolve(ARRAY_PAYLOAD, "tags[0]")
        assert found is True
        assert val == "gadgets"

    def test_second_element(self, resolve):
        val, found = resolve(ARRAY_PAYLOAD, "tags[1]")
        assert found is True
        assert val == "mini"

    def test_last_element(self, resolve):
        val, found = resolve(ARRAY_PAYLOAD, "tags[2]")
        assert found is True
        assert val == "electronics"

    def test_out_of_bounds(self, resolve):
        val, found = resolve(ARRAY_PAYLOAD, "tags[99]")
        assert found is False

    def test_negative_index(self, resolve):
        """Negative indices should not resolve (not supported)."""
        val, found = resolve(ARRAY_PAYLOAD, "tags[-1]")
        assert found is False

    def test_array_of_objects_field(self, resolve):
        val, found = resolve(ARRAY_PAYLOAD, "results[0].temp")
        assert found is True
        assert val == 21.3  # noqa: PLR2004

    def test_array_of_objects_second(self, resolve):
        val, found = resolve(ARRAY_PAYLOAD, "results[1].zone")
        assert found is True
        assert val == "South"

    def test_array_returns_object(self, resolve):
        """Resolving to an array element that is a dict returns the dict."""
        val, found = resolve(ARRAY_PAYLOAD, "results[2]")
        assert found is True
        assert val == {"zone": "East", "temp": 22.0}


# ---- Deeply nested mixed paths ----


class TestMixedPaths:
    """Exercise combinations of dotted paths and array indexing."""

    def test_deep_nested_with_arrays(self, resolve):
        val, found = resolve(MIXED_PAYLOAD, "building.floors[0].name")
        assert found is True
        assert val == "Ground"

    def test_nested_array_in_array(self, resolve):
        val, found = resolve(MIXED_PAYLOAD, "building.floors[0].zones[0].id")
        assert found is True
        assert val == "zone-1"

    def test_nested_array_in_array_second(self, resolve):
        val, found = resolve(MIXED_PAYLOAD, "building.floors[0].zones[1].temp")
        assert found is True
        assert val == 22.1  # noqa: PLR2004

    def test_three_levels_of_arrays(self, resolve):
        val, found = resolve(MIXED_PAYLOAD, "building.floors[0].zones[0].sensors[2]")
        assert found is True
        assert val == 300  # noqa: PLR2004

    def test_second_floor_zone(self, resolve):
        val, found = resolve(MIXED_PAYLOAD, "building.floors[1].zones[0].temp")
        assert found is True
        assert val == 19.8  # noqa: PLR2004

    def test_missing_deep_path(self, resolve):
        val, found = resolve(MIXED_PAYLOAD, "building.floors[0].zones[0].nonexistent")
        assert found is False

    def test_array_index_on_dict(self, resolve):
        """Bracket notation on a dict (not a list) should fail."""
        val, found = resolve(MIXED_PAYLOAD, "building[0]")
        assert found is False


# ---- Edge cases ----


class TestEdgeCases:
    """Boundary conditions and special values."""

    def test_empty_path_returns_data(self, resolve):
        val, found = resolve(FLAT_PAYLOAD, "")
        assert found is True
        assert val == FLAT_PAYLOAD

    def test_none_path_returns_data(self, resolve):
        val, found = resolve(FLAT_PAYLOAD, None)
        assert found is True
        assert val == FLAT_PAYLOAD

    def test_null_value_in_data(self, resolve):
        """A key whose value is None should be found."""
        data = {"field": None}
        val, found = resolve(data, "field")
        assert found is True
        assert val is None

    def test_zero_value(self, resolve):
        """Falsy values like 0 should be found."""
        data = {"count": 0}
        val, found = resolve(data, "count")
        assert found is True
        assert val == 0

    def test_empty_string_value(self, resolve):
        data = {"label": ""}
        val, found = resolve(data, "label")
        assert found is True
        assert val == ""

    def test_empty_list_value(self, resolve):
        data = {"items": []}
        val, found = resolve(data, "items")
        assert found is True
        assert val == []

    def test_empty_list_index(self, resolve):
        data = {"items": []}
        val, found = resolve(data, "items[0]")
        assert found is False

    def test_traverse_through_null(self, resolve):
        """Trying to traverse deeper into a None value should fail."""
        data = {"parent": None}
        val, found = resolve(data, "parent.child")
        assert found is False

    def test_list_at_root(self, resolve):
        """When root data is a list, direct indexing should work."""
        data = [{"id": 1}, {"id": 2}]
        val, found = resolve(data, "[0].id")
        assert found is True
        assert val == 1

    def test_nested_empty_dicts(self, resolve):
        data = {"a": {"b": {}}}
        val, found = resolve(data, "a.b.c")
        assert found is False

    def test_numeric_string_key(self, resolve):
        """Dict keys that look like numbers should resolve as dict keys."""
        data = {"2024": {"jan": 100}}
        val, found = resolve(data, "2024.jan")
        assert found is True
        assert val == 100  # noqa: PLR2004

    def test_key_with_hyphen(self, resolve):
        """Keys with hyphens should resolve correctly."""
        data = {"my-field": 42}
        val, found = resolve(data, "my-field")
        assert found is True
        assert val == 42  # noqa: PLR2004

    def test_key_with_underscore(self, resolve):
        data = {"my_field": 42}
        val, found = resolve(data, "my_field")
        assert found is True
        assert val == 42  # noqa: PLR2004


# ---- Type coercion safety ----


class TestTypeSafety:
    """Ensure path resolution handles type mismatches gracefully."""

    def test_dot_access_on_string(self, resolve):
        """Dotted access on a string value should fail, not crash."""
        data = {"name": "hello"}
        val, found = resolve(data, "name.length")
        assert found is False

    def test_dot_access_on_number(self, resolve):
        data = {"count": 42}
        val, found = resolve(data, "count.value")
        assert found is False

    def test_bracket_on_string(self, resolve):
        """Bracket indexing on a string should fail."""
        data = {"name": "hello"}
        val, found = resolve(data, "name[0]")
        assert found is False

    def test_bracket_on_number(self, resolve):
        data = {"count": 42}
        val, found = resolve(data, "count[0]")
        assert found is False

    def test_dot_access_on_boolean(self, resolve):
        data = {"flag": True}
        val, found = resolve(data, "flag.value")
        assert found is False

    def test_non_dict_root(self, resolve):
        """String root should return not-found for any path."""
        val, found = resolve("not a dict", "field")
        assert found is False

    def test_int_root(self, resolve):
        val, found = resolve(42, "field")
        assert found is False


# ===================================================================
# Chained bracket tests
# ===================================================================


class TestChainedBrackets:
    """Tests for chained bracket notation like matrix[0][1].

    The shared resolve_path() must support repeated bracket segments
    in a single dotted token, because assertion paths can contain
    expressions like matrix[0][1] or data[0][2][3].
    """

    def test_two_dimensional_array(self, resolve):
        """matrix[0][1] resolves through two array levels."""
        data = {"matrix": [[1, 2, 3], [4, 5, 6]]}
        val, found = resolve(data, "matrix[0][1]")
        assert found is True
        assert val == 2  # noqa: PLR2004

    def test_three_dimensional_array(self, resolve):
        """data[0][1][2] resolves through three array levels."""
        data = {"data": [[[10, 20], [30, 40]], [[50, 60], [70, 80]]]}
        val, found = resolve(data, "data[1][0][1]")
        assert found is True
        assert val == 60  # noqa: PLR2004

    def test_chained_brackets_out_of_bounds(self, resolve):
        """Out-of-bounds on second bracket returns not-found."""
        data = {"matrix": [[1, 2], [3, 4]]}
        val, found = resolve(data, "matrix[0][5]")
        assert found is False

    def test_chained_brackets_on_non_list(self, resolve):
        """Second bracket on a non-list returns not-found."""
        data = {"matrix": [[1, 2], "not a list"]}
        val, found = resolve(data, "matrix[1][0]")
        assert found is False

    def test_bare_chained_brackets(self, resolve):
        """[0][1] at root resolves through two levels."""
        data = [[10, 20], [30, 40]]
        val, found = resolve(data, "[0][1]")
        assert found is True
        assert val == 20  # noqa: PLR2004

    def test_chained_then_dotted(self, resolve):
        """matrix[0][1].value mixes chained brackets with dotted key."""
        data = {"matrix": [[{"value": "a"}, {"value": "b"}]]}
        val, found = resolve(data, "matrix[0][1].value")
        assert found is True
        assert val == "b"


# ===================================================================
# Realistic scenario tests
# ===================================================================


class TestRealisticScenarios:
    """End-to-end scenarios matching real-world data structures."""

    def test_building_energy_model_json(self, resolve):
        """Typical BEM payload with nested geometry and results."""
        payload = {
            "building": {
                "metadata": {
                    "name": "Office Tower A",
                    "location": {"city": "Denver", "climate_zone": "5B"},
                },
                "geometry": {
                    "total_floor_area_m2": 5000.0,
                    "num_floors": 12,
                },
            },
            "results": {
                "annual_energy": {
                    "heating_kwh": 150000,
                    "cooling_kwh": 200000,
                    "lighting_kwh": 80000,
                },
                "peak_loads": [
                    {"month": "January", "heating_kw": 450},
                    {"month": "July", "cooling_kw": 600},
                ],
            },
        }

        val, found = resolve(payload, "building.metadata.name")
        assert found is True
        assert val == "Office Tower A"

        val, found = resolve(payload, "building.metadata.location.climate_zone")
        assert found is True
        assert val == "5B"

        val, found = resolve(payload, "building.geometry.total_floor_area_m2")
        assert found is True
        assert val == 5000.0  # noqa: PLR2004

        val, found = resolve(payload, "results.annual_energy.heating_kwh")
        assert found is True
        assert val == 150000  # noqa: PLR2004

        val, found = resolve(payload, "results.peak_loads[0].heating_kw")
        assert found is True
        assert val == 450  # noqa: PLR2004

        val, found = resolve(payload, "results.peak_loads[1].cooling_kw")
        assert found is True
        assert val == 600  # noqa: PLR2004

    def test_fmu_simulation_output(self, resolve):
        """FMU output envelope with time-series data."""
        payload = {
            "output_values": {
                "indoor_temp": [20.1, 20.3, 20.5, 21.0],
                "energy_consumption": [100, 150, 120, 130],
            },
            "metadata": {
                "step_count": 4,
                "model_name": "HVAC_v2",
            },
        }

        val, found = resolve(payload, "output_values.indoor_temp[0]")
        assert found is True
        assert val == 20.1  # noqa: PLR2004

        val, found = resolve(payload, "output_values.energy_consumption[3]")
        assert found is True
        assert val == 130  # noqa: PLR2004

        val, found = resolve(payload, "metadata.model_name")
        assert found is True
        assert val == "HVAC_v2"

    def test_product_validation_payload(self, resolve):
        """E-commerce-style payload matching the help page examples."""
        payload = {
            "sku": "ABCD1234",
            "name": "Widget Mini",
            "price": 20.00,
            "rating": 95,
            "in_stock": True,
            "dimensions": {"width": 3.5, "height": 1.2},
            "tags": ["gadgets", "mini"],
            "reviews": [
                {"user": "alice", "score": 5, "text": "Great!"},
                {"user": "bob", "score": 4, "text": "Good value"},
            ],
        }

        val, found = resolve(payload, "price")
        assert found is True
        assert val == 20.00  # noqa: PLR2004

        val, found = resolve(payload, "dimensions.width")
        assert found is True
        assert val == 3.5  # noqa: PLR2004

        val, found = resolve(payload, "tags[0]")
        assert found is True
        assert val == "gadgets"

        val, found = resolve(payload, "reviews[1].score")
        assert found is True
        assert val == 4  # noqa: PLR2004
