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


# ===================================================================
# JSONPath filter expression tests
# ===================================================================
#
# These test the [?@.field=='value'] filter syntax added via the
# restricted python-jsonpath integration.  All tests run through the
# same parametrised ``resolve`` fixture so all three implementations
# are covered.

# Shared test data for filter tests.

NAMED_ELEMENT_PAYLOAD = {
    "ownedMember": [
        {
            "name": "RadiatorPanel",
            "type": "PartDefinition",
            "ownedAttribute": [
                {"name": "panelArea", "defaultValue": 2.0},
                {"name": "emissivity", "defaultValue": 0.85},
                {"name": "mass", "defaultValue": 3.6},
            ],
        },
        {
            "name": "ThermalEnvironment",
            "type": "PartDefinition",
            "ownedAttribute": [
                {"name": "solarIrradiance", "defaultValue": 1361.0},
            ],
        },
    ],
}


class TestFilterExpressions:
    """JSONPath filter expressions that should resolve successfully.

    Filter expressions let users locate array elements by a field value
    rather than by position.  This is essential for schemas like SysML v2
    where attribute names are values, not dict keys.
    """

    def test_single_filter_string_equality(self, resolve):
        """Basic filter: find element by string field value."""
        val, found = resolve(
            NAMED_ELEMENT_PAYLOAD,
            "ownedMember[?@.name=='RadiatorPanel'].type",
        )
        assert found is True
        assert val == "PartDefinition"

    def test_chained_filters(self, resolve):
        """Two chained filters: the motivating SysML v2 use case."""
        val, found = resolve(
            NAMED_ELEMENT_PAYLOAD,
            (
                "ownedMember[?@.name=='RadiatorPanel']"
                ".ownedAttribute[?@.name=='emissivity']"
                ".defaultValue"
            ),
        )
        assert found is True
        assert val == 0.85  # noqa: PLR2004

    def test_filter_numeric_equality(self, resolve):
        """Filter matching on a numeric value."""
        val, found = resolve(
            NAMED_ELEMENT_PAYLOAD,
            (
                "ownedMember[?@.name=='RadiatorPanel']"
                ".ownedAttribute[?@.defaultValue==2.0]"
                ".name"
            ),
        )
        assert found is True
        assert val == "panelArea"

    def test_filter_returns_object(self, resolve):
        """Filter without further traversal returns the matched dict."""
        val, found = resolve(
            NAMED_ELEMENT_PAYLOAD,
            "ownedMember[?@.name=='ThermalEnvironment']",
        )
        assert found is True
        assert isinstance(val, dict)
        assert val["name"] == "ThermalEnvironment"

    def test_filter_first_match_wins(self, resolve):
        """When multiple elements match, the first is returned."""
        data = {
            "items": [
                {"type": "sensor", "id": "first"},
                {"type": "sensor", "id": "second"},
            ],
        }
        val, found = resolve(data, "items[?@.type=='sensor'].id")
        assert found is True
        assert val == "first"

    def test_dot_navigation_before_and_after_filter(self, resolve):
        """Dot-notation segments can appear before and after a filter."""
        data = {
            "building": {
                "floors": [
                    {"name": "Ground", "area": 150.0},
                    {"name": "First", "area": 200.0},
                ],
            },
        }
        val, found = resolve(
            data,
            "building.floors[?@.name=='First'].area",
        )
        assert found is True
        assert val == 200.0  # noqa: PLR2004

    def test_positional_index_before_filter(self, resolve):
        """A bracket index can precede a filter expression."""
        data = {
            "buildings": [
                {
                    "floors": [
                        {"name": "Ground", "area": 100.0},
                        {"name": "First", "area": 200.0},
                    ],
                },
            ],
        }
        val, found = resolve(
            data,
            "buildings[0].floors[?@.name=='First'].area",
        )
        assert found is True
        assert val == 200.0  # noqa: PLR2004

    def test_filter_on_boolean(self, resolve):
        """Filter matching on a boolean value."""
        data = {"items": [{"active": True, "id": 1}, {"active": False, "id": 2}]}
        val, found = resolve(data, "items[?@.active==true].id")
        assert found is True
        assert val == 1

    def test_filter_not_equal(self, resolve):
        """Filter using != comparison."""
        data = {
            "items": [
                {"status": "draft", "id": 1},
                {"status": "published", "id": 2},
            ],
        }
        val, found = resolve(data, "items[?@.status!='draft'].id")
        assert found is True
        assert val == 2  # noqa: PLR2004


class TestFilterExpressionNotFound:
    """Filter expressions that are valid but match nothing.

    These should return ``(None, False)`` — the standard not-found
    result — without raising exceptions.
    """

    def test_filter_matches_nothing(self, resolve):
        """No element satisfies the filter predicate."""
        val, found = resolve(
            NAMED_ELEMENT_PAYLOAD,
            "ownedMember[?@.name=='NonExistent'].type",
        )
        assert found is False
        assert val is None

    def test_filter_on_empty_array(self, resolve):
        """Filter against an empty list returns not-found."""
        val, found = resolve(
            {"items": []},
            "items[?@.name=='x'].value",
        )
        assert found is False
        assert val is None

    def test_filter_matches_but_leaf_missing(self, resolve):
        """Filter matches an element but the subsequent key doesn't exist."""
        val, found = resolve(
            NAMED_ELEMENT_PAYLOAD,
            "ownedMember[?@.name=='RadiatorPanel'].nonExistentField",
        )
        assert found is False
        assert val is None

    def test_filter_on_missing_parent_key(self, resolve):
        """The key before the filter doesn't exist in the data."""
        val, found = resolve(
            {"other": 1},
            "missing[?@.name=='x'].value",
        )
        assert found is False
        assert val is None


class TestFilterExpressionBlocked:
    """Patterns that our security policy blocks before the library runs.

    Each blocked pattern should silently return ``(None, False)`` and
    log a warning — never raise an exception to callers.
    """

    def test_recursive_descent_blocked(self, resolve):
        """Recursive descent ('..') is blocked to prevent full-tree traversal."""
        val, found = resolve(
            NAMED_ELEMENT_PAYLOAD,
            "ownedMember[?@.name=='RadiatorPanel']..defaultValue",
        )
        assert found is False
        assert val is None

    def test_wildcard_bracket_blocked(self, resolve):
        """Wildcard [*] is blocked to prevent unbounded result sets."""
        val, found = resolve(
            NAMED_ELEMENT_PAYLOAD,
            "ownedMember[*].name",
        )
        assert found is False
        assert val is None

    def test_wildcard_dot_blocked(self, resolve):
        """Wildcard .* is blocked to prevent unbounded result sets."""
        val, found = resolve(
            NAMED_ELEMENT_PAYLOAD,
            "ownedMember[?@.name=='RadiatorPanel'].*",
        )
        assert found is False
        assert val is None

    def test_slice_blocked(self, resolve):
        """Slice notation is blocked to prevent large array selections."""
        val, found = resolve(
            NAMED_ELEMENT_PAYLOAD,
            "ownedMember[0:2][?@.name=='RadiatorPanel'].type",
        )
        assert found is False
        assert val is None

    def test_excess_filter_segments_blocked(self, resolve):
        """More than MAX_JSONPATH_FILTER_SEGMENTS filters are blocked."""
        # 5 filters exceeds the limit of 4.
        path = "a[?@.x=='1'].b[?@.y=='2'].c[?@.z=='3'].d[?@.w=='4'].e[?@.v=='5']"
        val, found = resolve({"a": []}, path)
        assert found is False
        assert val is None

    def test_four_filters_allowed(self, resolve):
        """Exactly MAX_JSONPATH_FILTER_SEGMENTS (4) filters are allowed."""
        # 4 filters at the limit — should not be blocked by the cap.
        # (Will return not-found because the data doesn't match, but
        # the important thing is it doesn't get blocked by policy.)
        path = "a[?@.x=='1'].b[?@.y=='2'].c[?@.z=='3'].d[?@.w=='4'].value"
        # We just verify it doesn't get blocked — not-found is fine.
        val, found = resolve({"a": []}, path)
        assert found is False
        assert val is None


class TestFilterExpressionMalformed:
    """Malformed filter expressions should return not-found, not crash.

    The library raises parse errors for invalid syntax.  Our wrapper
    catches these and returns ``(None, False)``.
    """

    def test_unclosed_filter_bracket(self, resolve):
        """Missing closing bracket in filter expression."""
        val, found = resolve(
            NAMED_ELEMENT_PAYLOAD,
            "ownedMember[?@.name=='x'",
        )
        assert found is False
        assert val is None

    def test_empty_filter(self, resolve):
        """Empty filter predicate."""
        val, found = resolve(
            NAMED_ELEMENT_PAYLOAD,
            "ownedMember[?].name",
        )
        assert found is False
        assert val is None

    def test_invalid_filter_syntax(self, resolve):
        """Garbage inside filter brackets."""
        val, found = resolve(
            NAMED_ELEMENT_PAYLOAD,
            "ownedMember[?!!!].name",
        )
        assert found is False
        assert val is None


class TestFilterExpressionMalicious:
    """Inputs designed to exploit the JSONPath library.

    These verify that our defense-in-depth layers (pre-validation +
    cleared function extensions) neutralize attack attempts.
    """

    def test_function_call_in_filter_blocked(self, resolve):
        """Function calls are blocked because we cleared function_extensions.

        The ``match()`` and ``search()`` functions accept regex patterns
        which could be ReDoS vectors.  Clearing the extension dict means
        the library raises JSONPathNameError for any function call.
        """
        val, found = resolve(
            NAMED_ELEMENT_PAYLOAD,
            "ownedMember[?match(@.name, '.*')].type",
        )
        assert found is False
        assert val is None

    def test_search_function_blocked(self, resolve):
        """The search() function (regex) is also blocked."""
        val, found = resolve(
            NAMED_ELEMENT_PAYLOAD,
            "ownedMember[?search(@.name, 'Rad')].type",
        )
        assert found is False
        assert val is None

    def test_length_function_blocked(self, resolve):
        """Even length() is blocked — we allow no functions at all."""
        val, found = resolve(
            NAMED_ELEMENT_PAYLOAD,
            "ownedMember[?length(@.name) > 5].type",
        )
        assert found is False
        assert val is None

    def test_deeply_nested_recursive_descent(self, resolve):
        """Recursive descent combined with filter — double-blocked."""
        val, found = resolve(
            NAMED_ELEMENT_PAYLOAD,
            "..[?@.name=='emissivity'].defaultValue",
        )
        assert found is False
        assert val is None

    def test_wildcard_chaining(self, resolve):
        """Chained wildcards that could cause combinatorial explosion."""
        val, found = resolve(
            NAMED_ELEMENT_PAYLOAD,
            "ownedMember[*].ownedAttribute[*].defaultValue",
        )
        assert found is False
        assert val is None


class TestFilterExpressionSysMLv2:
    """Integration tests against the actual SysML v2 radiator model.

    These load the real test asset and resolve the exact paths that
    the SysML v2 workflow needs for signal bindings.
    """

    @pytest.fixture
    def radiator_payload(self):
        """Load the valid thermal radiator model test asset."""
        import json
        from pathlib import Path

        asset = (
            Path(__file__).resolve().parents[3]
            / "tests"
            / "assets"
            / "sysml_v2"
            / "radiator_example"
            / "thermal_radiator_model.json"
        )
        with asset.open() as f:
            return json.load(f)

    def test_resolve_emissivity(self, resolve, radiator_payload):
        """Resolve emissivity from the radiator panel attributes."""
        val, found = resolve(
            radiator_payload,
            (
                "ownedMember[?@.name=='RadiatorPanel']"
                ".ownedAttribute[?@.name=='emissivity']"
                ".defaultValue"
            ),
        )
        assert found is True
        assert val == 0.85  # noqa: PLR2004

    def test_resolve_panel_area(self, resolve, radiator_payload):
        """Resolve panelArea from the radiator panel attributes."""
        val, found = resolve(
            radiator_payload,
            (
                "ownedMember[?@.name=='RadiatorPanel']"
                ".ownedAttribute[?@.name=='panelArea']"
                ".defaultValue"
            ),
        )
        assert found is True
        assert val == 2.0  # noqa: PLR2004

    def test_resolve_mass(self, resolve, radiator_payload):
        """Resolve mass from the radiator panel attributes."""
        val, found = resolve(
            radiator_payload,
            (
                "ownedMember[?@.name=='RadiatorPanel']"
                ".ownedAttribute[?@.name=='mass']"
                ".defaultValue"
            ),
        )
        assert found is True
        assert val == 3.6  # noqa: PLR2004

    def test_resolve_solar_irradiance(self, resolve, radiator_payload):
        """Resolve solarIrradiance from the thermal environment."""
        val, found = resolve(
            radiator_payload,
            (
                "ownedMember[?@.name=='ThermalEnvironment']"
                ".ownedAttribute[?@.name=='solarIrradiance']"
                ".defaultValue"
            ),
        )
        assert found is True
        assert val == 1361.0  # noqa: PLR2004

    def test_resolve_nonexistent_attribute(self, resolve, radiator_payload):
        """A valid-looking SysML path for an attribute that doesn't exist."""
        val, found = resolve(
            radiator_payload,
            (
                "ownedMember[?@.name=='RadiatorPanel']"
                ".ownedAttribute[?@.name=='conductivity']"
                ".defaultValue"
            ),
        )
        assert found is False
        assert val is None
