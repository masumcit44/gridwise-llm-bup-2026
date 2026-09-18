"""Deterministic tests for the strict directive guardrails.

Covers prompt Task-2 section A and SPEC_AUDIT.md 2.11 / section 4:

- one entry per operator note, ``note_index`` exactly 0..N-1 in order
- only the six official directive types
- ``no_op`` semantics vs ``applies=true`` directive semantics
- exact ``structured_adjustment`` shapes, extra-field and arbitrary-dict
  rejection
- hours non-empty, unique, ascending, integers 0..23, never clipped
- NaN / infinity / negative / out-of-range numeric rejection, never clamped
- reserve must not exceed battery capacity
- failures raise ``GuardrailValidationError`` (never ``AssertionError``) and
  are never converted into ``no_op`` or repaired

No LLM, network, or filesystem access is used.
"""

from __future__ import annotations

import copy
import math

import pytest

from app.guardrails import (
    ADJUSTMENT_FIELDS,
    ENTRY_FIELDS,
    SUPPORTED_DIRECTIVE_TYPES,
    GuardrailValidationError,
    validate_directive_entry,
    validate_directive_interpretation,
)
from app.schemas import (
    DirectiveInterpretation,
    DirectiveType,
    MaxGridWindowAdjustment,
    MinimumBatteryReserveAdjustment,
    NoChargeWindowAdjustment,
    NoDischargeWindowAdjustment,
    SolarReductionAdjustment,
)

CAPACITY = 200.0

WINDOW_TYPES = [
    "solar_reduction",
    "minimum_battery_reserve",
    "no_charge_window",
    "no_discharge_window",
    "max_grid_window",
]
ALL_TYPES = WINDOW_TYPES + ["no_op"]

# One valid adjustment object per directive type (the exact official shape).
VALID_ADJUSTMENTS: dict[str, object] = {
    "solar_reduction": {"hours": [13, 14], "factor": 0.25},
    "minimum_battery_reserve": {
        "hours": [18, 19, 20],
        "minimum_energy_kwh": 100.0,
    },
    "no_charge_window": {"hours": [2, 3, 4]},
    "no_discharge_window": {"hours": [18, 19]},
    "max_grid_window": {"hours": [19, 20], "max_grid_kwh": 180.0},
    "no_op": None,
}

_DEFAULT = object()


def _notes(count: int = 1) -> list[str]:
    return [f"operator note {index}" for index in range(count)]


def _entry(
    note_index: int = 0,
    directive_type: str = "solar_reduction",
    *,
    applies: object = _DEFAULT,
    adjustment: object = _DEFAULT,
    explanation: object = "Interpreted operator note.",
) -> dict:
    """Build one raw directive entry shaped like untrusted LLM output."""
    return {
        "note_index": note_index,
        "applies": (directive_type != "no_op") if applies is _DEFAULT else applies,
        "directive_type": directive_type,
        "structured_adjustment": (
            copy.deepcopy(VALID_ADJUSTMENTS.get(directive_type))
            if adjustment is _DEFAULT
            else adjustment
        ),
        "explanation": explanation,
    }


def _validate(entries, notes=_DEFAULT, capacity=CAPACITY):
    if notes is _DEFAULT:
        notes = _notes(len(entries) if entries else 1)
    return validate_directive_interpretation(entries, notes, capacity)


def _failure(entries, notes=_DEFAULT, capacity=CAPACITY) -> GuardrailValidationError:
    """Return the guardrail error raised for an invalid interpretation."""
    with pytest.raises(GuardrailValidationError) as excinfo:
        _validate(entries, notes, capacity)
    return excinfo.value


# --------------------------------------------------------------------------- #
# Valid interpretations
# --------------------------------------------------------------------------- #


class TestValidInterpretation:
    @pytest.mark.parametrize("directive_type", ALL_TYPES)
    def test_each_official_type_accepted(self, directive_type: str) -> None:
        result = _validate([_entry(0, directive_type)])
        assert len(result) == 1
        assert isinstance(result[0], DirectiveInterpretation)
        assert result[0].directive_type is DirectiveType(directive_type)

    def test_values_are_preserved_exactly(self) -> None:
        result = _validate([_entry(0, "solar_reduction")])
        adjustment = result[0].structured_adjustment
        assert isinstance(adjustment, SolarReductionAdjustment)
        assert adjustment.hours == [13, 14]
        assert adjustment.factor == 0.25

    def test_adjustment_model_matches_directive_type(self) -> None:
        expected = {
            "solar_reduction": SolarReductionAdjustment,
            "minimum_battery_reserve": MinimumBatteryReserveAdjustment,
            "no_charge_window": NoChargeWindowAdjustment,
            "no_discharge_window": NoDischargeWindowAdjustment,
            "max_grid_window": MaxGridWindowAdjustment,
            "no_op": type(None),
        }
        for directive_type, model in expected.items():
            result = _validate([_entry(0, directive_type)])
            assert isinstance(result[0].structured_adjustment, model)

    def test_multiple_compatible_directives(self) -> None:
        types = ["solar_reduction", "no_charge_window", "max_grid_window"]
        result = _validate([_entry(index, kind) for index, kind in enumerate(types)])
        assert [entry.note_index for entry in result] == [0, 1, 2]
        assert [entry.directive_type.value for entry in result] == types
        assert all(entry.applies for entry in result)

    def test_distractor_no_op_among_directives(self) -> None:
        entries = [
            _entry(0, "minimum_battery_reserve"),
            _entry(1, "no_op"),
            _entry(2, "max_grid_window"),
        ]
        result = _validate(entries, notes=_notes(3))
        assert [entry.applies for entry in result] == [True, False, True]
        assert result[1].structured_adjustment is None
        assert result[1].directive_type is DirectiveType.NO_OP

    def test_sample_style_reserve_and_grid_cap(self) -> None:
        entries = [
            _entry(
                0,
                "minimum_battery_reserve",
                adjustment={"hours": [18, 19, 20, 21], "minimum_energy_kwh": 60.0},
            ),
            _entry(
                1,
                "max_grid_window",
                adjustment={"hours": [19, 20, 21], "max_grid_kwh": 150.0},
            ),
        ]
        result = _validate(entries, notes=_notes(2))
        assert result[1].structured_adjustment.max_grid_kwh == 150.0

    def test_accepts_prebuilt_directive_models(self) -> None:
        models = [
            DirectiveInterpretation(
                note_index=0,
                applies=True,
                directive_type=DirectiveType.NO_CHARGE_WINDOW,
                structured_adjustment=NoChargeWindowAdjustment(hours=[2, 3, 4]),
                explanation="Charging blocked.",
            )
        ]
        result = _validate(models)
        assert result[0].structured_adjustment.hours == [2, 3, 4]

    def test_accepts_tuple_hours(self) -> None:
        entry = _entry(0, "no_charge_window", adjustment={"hours": (2, 3, 4)})
        result = _validate([entry])
        assert result[0].structured_adjustment.hours == [2, 3, 4]

    @pytest.mark.parametrize("factor", [0.0, 1.0, 0, 1])
    def test_factor_boundaries_accepted(self, factor: float) -> None:
        entry = _entry(0, adjustment={"hours": [10], "factor": factor})
        result = _validate([entry])
        assert result[0].structured_adjustment.factor == float(factor)

    def test_reserve_equal_to_capacity_accepted(self) -> None:
        entry = _entry(
            0,
            "minimum_battery_reserve",
            adjustment={"hours": [18], "minimum_energy_kwh": CAPACITY},
        )
        result = _validate([entry])
        assert result[0].structured_adjustment.minimum_energy_kwh == CAPACITY

    def test_zero_numeric_values_accepted(self) -> None:
        entry = _entry(
            0,
            "max_grid_window",
            adjustment={"hours": [0], "max_grid_kwh": 0},
        )
        result = _validate([entry])
        assert result[0].structured_adjustment.max_grid_kwh == 0.0

    def test_hour_boundaries_accepted(self) -> None:
        entry = _entry(0, "no_discharge_window", adjustment={"hours": [0, 23]})
        result = _validate([entry])
        assert result[0].structured_adjustment.hours == [0, 23]

    def test_all_24_hours_accepted(self) -> None:
        hours = list(range(24))
        entry = _entry(0, "no_charge_window", adjustment={"hours": hours})
        result = _validate([entry])
        assert result[0].structured_adjustment.hours == hours

    def test_directive_type_enum_value_accepted(self) -> None:
        entry = _entry(0, "no_op")
        entry["directive_type"] = DirectiveType.NO_OP
        result = _validate([entry])
        assert result[0].directive_type is DirectiveType.NO_OP

    def test_validated_hours_are_independent_of_input(self) -> None:
        adjustment = {"hours": [13, 14], "factor": 0.5}
        result = _validate([_entry(0, adjustment=adjustment)])
        adjustment["hours"].append(20)
        assert result[0].structured_adjustment.hours == [13, 14]


# --------------------------------------------------------------------------- #
# One entry per note, note_index ordering
# --------------------------------------------------------------------------- #


class TestNoteCountAndMapping:
    def test_missing_entry_for_second_note(self) -> None:
        error = _failure([_entry(0)], notes=_notes(2))
        assert error.rule == "interpretation.count"

    def test_extra_entry_for_single_note(self) -> None:
        entries = [_entry(0), _entry(1, "no_op")]
        error = _failure(entries, notes=_notes(1))
        assert error.rule == "interpretation.count"

    def test_empty_entries_for_one_note(self) -> None:
        error = _failure([], notes=_notes(1))
        assert error.rule == "interpretation.count"

    def test_three_notes_require_three_entries(self) -> None:
        entries = [_entry(0), _entry(1, "no_op")]
        error = _failure(entries, notes=_notes(3))
        assert error.rule == "interpretation.count"

    @pytest.mark.parametrize("entries", [None, {}, "not-a-list", 5, {"a": 1}])
    def test_non_sequence_entries_rejected(self, entries) -> None:
        error = _failure(entries, notes=_notes(1))
        assert error.rule == "interpretation.type"

    def test_count_error_has_no_note_index(self) -> None:
        assert _failure([]).note_index is None


class TestNoteIndex:
    def test_missing_note_index_key(self) -> None:
        entry = _entry(0)
        del entry["note_index"]
        error = _failure([entry])
        assert error.rule == "entry.missing_fields"
        assert error.note_index == 0

    def test_duplicate_note_index(self) -> None:
        entries = [_entry(0), _entry(0, "no_op")]
        error = _failure(entries, notes=_notes(2))
        assert error.rule == "entry.note_index.order"

    def test_negative_note_index(self) -> None:
        error = _failure([_entry(-1)])
        assert error.rule == "entry.note_index.negative"

    @pytest.mark.parametrize("note_index", [1, 24, 99])
    def test_out_of_range_note_index(self, note_index: int) -> None:
        error = _failure([_entry(note_index)])
        assert error.rule == "entry.note_index.out_of_range"

    def test_out_of_order_note_index(self) -> None:
        entries = [_entry(1, "no_op"), _entry(0)]
        error = _failure(entries, notes=_notes(2))
        assert error.rule == "entry.note_index.order"
        assert error.note_index == 0

    @pytest.mark.parametrize("value", ["0", 0.0, True, None, [0]])
    def test_non_integer_note_index(self, value) -> None:
        error = _failure([_entry(value)])
        assert error.rule == "entry.note_index.type"

    def test_note_index_error_reports_entry_position(self) -> None:
        entries = [_entry(0), _entry(5, "no_op")]
        error = _failure(entries, notes=_notes(2))
        assert error.note_index == 1


# --------------------------------------------------------------------------- #
# no_op semantics and applies semantics
# --------------------------------------------------------------------------- #


class TestNoOpSemantics:
    def test_valid_no_op(self) -> None:
        result = _validate([_entry(0, "no_op")])
        assert result[0].applies is False
        assert result[0].structured_adjustment is None

    def test_no_op_with_applies_true_rejected(self) -> None:
        error = _failure([_entry(0, "no_op", applies=True)])
        assert error.rule == "no_op.applies"

    @pytest.mark.parametrize(
        "adjustment",
        [{}, {"hours": [1]}, {"factor": 0.5}, {"hours": [1], "factor": 0.5}, [], ""],
    )
    def test_no_op_with_adjustment_rejected(self, adjustment) -> None:
        error = _failure([_entry(0, "no_op", adjustment=adjustment)])
        assert error.rule == "no_op.adjustment"

    def test_no_op_missing_adjustment_key(self) -> None:
        entry = _entry(0, "no_op")
        del entry["structured_adjustment"]
        error = _failure([entry])
        assert error.rule == "entry.missing_fields"

    @pytest.mark.parametrize("directive_type", WINDOW_TYPES)
    def test_non_no_op_requires_applies_true(self, directive_type: str) -> None:
        error = _failure([_entry(0, directive_type, applies=False)])
        assert error.rule == "directive.applies"

    @pytest.mark.parametrize("directive_type", WINDOW_TYPES)
    def test_non_no_op_requires_adjustment_object(self, directive_type: str) -> None:
        error = _failure([_entry(0, directive_type, adjustment=None)])
        assert error.rule == "directive.adjustment.missing"

    @pytest.mark.parametrize("applies", [0, 1, "true", "false", None, []])
    def test_applies_must_be_boolean(self, applies) -> None:
        error = _failure([_entry(0, applies=applies)])
        assert error.rule == "entry.applies.type"


# --------------------------------------------------------------------------- #
# Official directive types only
# --------------------------------------------------------------------------- #


class TestDirectiveTypes:
    @pytest.mark.parametrize(
        "directive_type",
        [
            "load_shift",
            "battery_reserve",
            "solar_increase",
            "",
            "No_Op",
            "SOLAR_REDUCTION",
            "no-op",
        ],
    )
    def test_unsupported_type_rejected(self, directive_type: str) -> None:
        error = _failure([_entry(0, directive_type)])
        assert error.rule == "entry.directive_type.unsupported"

    def test_unsupported_type_message_lists_official_types(self) -> None:
        message = str(_failure([_entry(0, "load_shift")]))
        for name in ("solar_reduction", "max_grid_window", "no_op"):
            assert name in message

    @pytest.mark.parametrize("value", [None, 3, 1.5, True, ["solar_reduction"]])
    def test_non_string_directive_type_rejected(self, value) -> None:
        entry = _entry(0)
        entry["directive_type"] = value
        error = _failure([entry])
        assert error.rule == "entry.directive_type.type"

    def test_supported_types_are_exactly_the_official_six(self) -> None:
        assert {item.value for item in SUPPORTED_DIRECTIVE_TYPES} == {
            "solar_reduction",
            "minimum_battery_reserve",
            "no_charge_window",
            "no_discharge_window",
            "max_grid_window",
            "no_op",
        }


# --------------------------------------------------------------------------- #
# Exact structured_adjustment shapes
# --------------------------------------------------------------------------- #


class TestAdjustmentShape:
    def test_solar_reduction_missing_factor(self) -> None:
        entry = _entry(0, adjustment={"hours": [13]})
        error = _failure([entry])
        assert error.rule == "directive.adjustment.missing_fields"

    def test_solar_reduction_extra_field(self) -> None:
        entry = _entry(
            0,
            adjustment={"hours": [13], "factor": 0.5, "max_grid_kwh": 10},
        )
        error = _failure([entry])
        assert error.rule == "directive.adjustment.extra_fields"

    def test_no_charge_window_rejects_factor(self) -> None:
        entry = _entry(0, "no_charge_window", adjustment={"hours": [1], "factor": 0.5})
        error = _failure([entry])
        assert error.rule == "directive.adjustment.extra_fields"

    def test_no_charge_window_rejects_reserve_value(self) -> None:
        entry = _entry(
            0,
            "no_charge_window",
            adjustment={"hours": [1], "minimum_energy_kwh": 5},
        )
        error = _failure([entry])
        assert error.rule == "directive.adjustment.extra_fields"

    def test_no_discharge_window_rejects_extra_cap(self) -> None:
        entry = _entry(0, "no_discharge_window", adjustment={"hours": [1], "max_grid_kwh": 5})
        error = _failure([entry])
        assert error.rule == "directive.adjustment.extra_fields"

    def test_max_grid_window_missing_cap(self) -> None:
        entry = _entry(0, "max_grid_window", adjustment={"hours": [19]})
        error = _failure([entry])
        assert error.rule == "directive.adjustment.missing_fields"

    def test_minimum_reserve_missing_value(self) -> None:
        entry = _entry(0, "minimum_battery_reserve", adjustment={"hours": [18]})
        error = _failure([entry])
        assert error.rule == "directive.adjustment.missing_fields"

    def test_missing_hours_key(self) -> None:
        entry = _entry(0, "max_grid_window", adjustment={"max_grid_kwh": 10})
        error = _failure([entry])
        assert error.rule == "directive.adjustment.missing_fields"

    def test_empty_adjustment_object(self) -> None:
        entry = _entry(0, "no_charge_window", adjustment={})
        error = _failure([entry])
        assert error.rule == "directive.adjustment.missing_fields"

    @pytest.mark.parametrize(
        "adjustment",
        [
            {"anything": 1},
            {"nested": {"hours": [1]}},
            {"solar": "reduction"},
            {0: 1},
            {"hours": [1], "factor": 0.5, "unknown": None},
        ],
    )
    def test_arbitrary_dict_rejected(self, adjustment) -> None:
        entry = _entry(0, "solar_reduction", adjustment=adjustment)
        error = _failure([entry])
        assert error.rule == "directive.adjustment.extra_fields"

    @pytest.mark.parametrize("adjustment", [[], "hours", 5, 1.5, True, ["hours"]])
    def test_non_mapping_adjustment_rejected(self, adjustment) -> None:
        entry = _entry(0, "solar_reduction", adjustment=adjustment)
        error = _failure([entry])
        assert error.rule == "directive.adjustment.type"

    def test_adjustment_fields_constant_is_exact(self) -> None:
        assert {key.value: set(value) for key, value in ADJUSTMENT_FIELDS.items()} == {
            "solar_reduction": {"hours", "factor"},
            "minimum_battery_reserve": {"hours", "minimum_energy_kwh"},
            "no_charge_window": {"hours"},
            "no_discharge_window": {"hours"},
            "max_grid_window": {"hours", "max_grid_kwh"},
            "no_op": set(),
        }


# --------------------------------------------------------------------------- #
# Hours: non-empty, unique, ascending, integers 0..23
# --------------------------------------------------------------------------- #


class TestHours:
    @pytest.mark.parametrize("directive_type", WINDOW_TYPES)
    def test_empty_hours_rejected(self, directive_type: str) -> None:
        adjustment = copy.deepcopy(VALID_ADJUSTMENTS[directive_type])
        adjustment["hours"] = []
        error = _failure([_entry(0, directive_type, adjustment=adjustment)])
        assert error.rule == "hours.empty"

    def test_duplicate_hours_rejected(self) -> None:
        entry = _entry(0, "no_charge_window", adjustment={"hours": [10, 10]})
        error = _failure([entry])
        assert error.rule == "hours.unique"

    @pytest.mark.parametrize("hours", [[-1], [24], [25], [0, 24], [-5, 3]])
    def test_out_of_range_hours_rejected(self, hours) -> None:
        entry = _entry(0, "no_charge_window", adjustment={"hours": hours})
        error = _failure([entry])
        assert error.rule == "hours.range"

    def test_unsorted_hours_rejected(self) -> None:
        entry = _entry(0, "no_charge_window", adjustment={"hours": [12, 11]})
        error = _failure([entry])
        assert error.rule == "hours.ascending"

    def test_wraparound_hours_rejected(self) -> None:
        entry = _entry(0, "no_charge_window", adjustment={"hours": [22, 23, 0, 1]})
        error = _failure([entry])
        assert error.rule == "hours.ascending"

    @pytest.mark.parametrize("hours", [[10, "11"], [1.5], [None], [True], [10, 11.0]])
    def test_non_integer_hours_rejected(self, hours) -> None:
        entry = _entry(0, "no_charge_window", adjustment={"hours": hours})
        error = _failure([entry])
        assert error.rule == "hours.range"

    @pytest.mark.parametrize("hours", [5, "13", None, True, {"hour": 13}, {13}])
    def test_non_sequence_hours_rejected(self, hours) -> None:
        entry = _entry(0, "no_charge_window", adjustment={"hours": hours})
        error = _failure([entry])
        assert error.rule == "hours.type"

    def test_hours_error_message_preserves_offending_values(self) -> None:
        entry = _entry(0, "no_charge_window", adjustment={"hours": [12, 11]})
        message = str(_failure([entry]))
        assert "12" in message
        assert "11" in message


# --------------------------------------------------------------------------- #
# Numeric strictness: finite ranges, never clamped
# --------------------------------------------------------------------------- #


class TestNumericStrictness:
    @pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
    def test_non_finite_factor_rejected(self, value: float) -> None:
        entry = _entry(0, adjustment={"hours": [13], "factor": value})
        error = _failure([entry])
        assert error.rule == "solar_reduction.factor.non_finite"

    @pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
    def test_non_finite_reserve_rejected(self, value: float) -> None:
        entry = _entry(
            0,
            "minimum_battery_reserve",
            adjustment={"hours": [18], "minimum_energy_kwh": value},
        )
        error = _failure([entry])
        assert (
            error.rule
            == "minimum_battery_reserve.minimum_energy_kwh.non_finite"
        )

    @pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
    def test_non_finite_max_grid_rejected(self, value: float) -> None:
        entry = _entry(
            0,
            "max_grid_window",
            adjustment={"hours": [19], "max_grid_kwh": value},
        )
        error = _failure([entry])
        assert error.rule == "max_grid_window.max_grid_kwh.non_finite"

    @pytest.mark.parametrize("factor", [-0.01, -1, 1.01, 2, 100])
    def test_factor_out_of_range_rejected_not_clamped(self, factor: float) -> None:
        entry = _entry(0, adjustment={"hours": [13], "factor": factor})
        error = _failure([entry])
        assert error.rule == "solar_reduction.factor.range"
        assert repr(float(factor)) in str(error)

    def test_negative_reserve_rejected_not_clamped(self) -> None:
        entry = _entry(
            0,
            "minimum_battery_reserve",
            adjustment={"hours": [18], "minimum_energy_kwh": -5},
        )
        error = _failure([entry])
        assert (
            error.rule
            == "minimum_battery_reserve.minimum_energy_kwh.negative"
        )

    def test_negative_max_grid_rejected_not_clamped(self) -> None:
        entry = _entry(
            0,
            "max_grid_window",
            adjustment={"hours": [19], "max_grid_kwh": -0.5},
        )
        error = _failure([entry])
        assert error.rule == "max_grid_window.max_grid_kwh.negative"

    @pytest.mark.parametrize("value", [200.01, 250, 1000])
    def test_reserve_above_capacity_rejected(self, value: float) -> None:
        entry = _entry(
            0,
            "minimum_battery_reserve",
            adjustment={"hours": [18], "minimum_energy_kwh": value},
        )
        error = _failure([entry])
        assert error.rule == "minimum_battery_reserve.minimum_energy_kwh.capacity"

    def test_reserve_uses_supplied_capacity(self) -> None:
        entry = _entry(
            0,
            "minimum_battery_reserve",
            adjustment={"hours": [18], "minimum_energy_kwh": 50},
        )
        result = _validate([entry], capacity=50.0)
        assert result[0].structured_adjustment.minimum_energy_kwh == 50.0

        error = _failure([entry], capacity=49.99)
        assert error.rule == "minimum_battery_reserve.minimum_energy_kwh.capacity"

    @pytest.mark.parametrize("value", [True, "0.5", None, [0.5]])
    def test_non_numeric_factor_rejected(self, value) -> None:
        entry = _entry(0, adjustment={"hours": [13], "factor": value})
        error = _failure([entry])
        assert error.rule == "solar_reduction.factor.type"

    @pytest.mark.parametrize("value", [True, "100", None])
    def test_non_numeric_reserve_rejected(self, value) -> None:
        entry = _entry(
            0,
            "minimum_battery_reserve",
            adjustment={"hours": [18], "minimum_energy_kwh": value},
        )
        error = _failure([entry])
        assert error.rule == "minimum_battery_reserve.minimum_energy_kwh.type"

    @pytest.mark.parametrize("value", [True, "180", [180]])
    def test_non_numeric_max_grid_rejected(self, value) -> None:
        entry = _entry(
            0,
            "max_grid_window",
            adjustment={"hours": [19], "max_grid_kwh": value},
        )
        error = _failure([entry])
        assert error.rule == "max_grid_window.max_grid_kwh.type"


# --------------------------------------------------------------------------- #
# Entry field strictness and explanation
# --------------------------------------------------------------------------- #


class TestEntryFieldStrictness:
    @pytest.mark.parametrize("extra_key", ["priority", "confidence", "reason", "hours"])
    def test_extra_entry_field_rejected(self, extra_key: str) -> None:
        entry = _entry(0)
        entry[extra_key] = "unexpected"
        error = _failure([entry])
        assert error.rule == "entry.extra_fields"

    @pytest.mark.parametrize(
        "missing_key",
        ["applies", "directive_type", "explanation", "structured_adjustment"],
    )
    def test_missing_required_entry_field_rejected(self, missing_key: str) -> None:
        entry = _entry(0)
        del entry[missing_key]
        error = _failure([entry])
        assert error.rule == "entry.missing_fields"

    def test_entry_fields_constant_is_exact(self) -> None:
        assert ENTRY_FIELDS == {
            "note_index",
            "applies",
            "directive_type",
            "structured_adjustment",
            "explanation",
        }

    @pytest.mark.parametrize("entry", [None, "text", 5, ["entry"], {"a"}])
    def test_non_mapping_entry_rejected(self, entry) -> None:
        error = _failure([entry])
        assert error.rule == "entry.type"

    @pytest.mark.parametrize("explanation", ["", None, 5, ["text"], 1.5])
    def test_invalid_explanation_rejected(self, explanation) -> None:
        entry = _entry(0, explanation=explanation)
        error = _failure([entry])
        assert error.rule == "entry.explanation.invalid"


class TestContextValidation:
    @pytest.mark.parametrize(
        "notes",
        ["note", 5, None, {"note": "a"}, [None], [""], [5], [[]]],
    )
    def test_invalid_operator_notes_rejected(self, notes) -> None:
        error = _failure([_entry(0)], notes=notes)
        assert error.rule == "context.operator_notes"

    @pytest.mark.parametrize(
        "capacity",
        [None, "200", math.nan, math.inf, -1, True, [200]],
    )
    def test_invalid_capacity_rejected(self, capacity) -> None:
        error = _failure([_entry(0)], capacity=capacity)
        assert error.rule == "context.battery_capacity"

    def test_capacity_checked_before_entry_validation(self) -> None:
        error = _failure([None], capacity=-1.0)
        assert error.rule == "context.battery_capacity"


# --------------------------------------------------------------------------- #
# Single-entry public API
# --------------------------------------------------------------------------- #


class TestValidateDirectiveEntry:
    def test_valid_entry(self) -> None:
        result = validate_directive_entry(
            _entry(0),
            position=0,
            note_count=1,
            battery_capacity_kwh=CAPACITY,
        )
        assert result.note_index == 0
        assert result.directive_type is DirectiveType.SOLAR_REDUCTION

    def test_position_mismatch_rejected(self) -> None:
        with pytest.raises(GuardrailValidationError) as excinfo:
            validate_directive_entry(
                _entry(1, "no_op"),
                position=0,
                note_count=2,
                battery_capacity_kwh=CAPACITY,
            )
        assert excinfo.value.rule == "entry.note_index.order"

    def test_capacity_context_enforced(self) -> None:
        entry = _entry(
            0,
            "minimum_battery_reserve",
            adjustment={"hours": [1], "minimum_energy_kwh": 500},
        )
        with pytest.raises(GuardrailValidationError) as excinfo:
            validate_directive_entry(
                entry,
                position=0,
                note_count=1,
                battery_capacity_kwh=CAPACITY,
            )
        assert (
            excinfo.value.rule
            == "minimum_battery_reserve.minimum_energy_kwh.capacity"
        )

    @pytest.mark.parametrize(
        "position,note_count,rule",
        [(-1, 1, "context.position"), (0, -1, "context.note_count")],
    )
    def test_invalid_arguments_rejected(
        self, position: int, note_count: int, rule: str
    ) -> None:
        with pytest.raises(GuardrailValidationError) as excinfo:
            validate_directive_entry(
                _entry(0),
                position=position,
                note_count=note_count,
                battery_capacity_kwh=CAPACITY,
            )
        assert excinfo.value.rule == rule

    def test_validated_models_are_schema_compatible(self) -> None:
        result = _validate([_entry(0, "max_grid_window")])
        dumped = result[0].model_dump()
        assert dumped["applies"] is True
        assert dumped["structured_adjustment"]["max_grid_kwh"] == 180.0
        assert DirectiveInterpretation(**dumped) == result[0]


# --------------------------------------------------------------------------- #
# Error contract: typed failures, no repair, no silent no_op
# --------------------------------------------------------------------------- #

INVALID_ENTRIES = [
    _entry(0, "no_op", applies=True),
    _entry(0, "no_op", adjustment={"hours": [1]}),
    _entry(0, adjustment={"hours": [13], "factor": 5.0}),
    _entry(0, adjustment={"hours": [13], "factor": math.nan}),
    _entry(0, adjustment={"hours": [12, 11]}),
    _entry(0, adjustment={"hours": []}),
    _entry(0, adjustment={"invented": 1}),
    _entry(0, applies="yes"),
    _entry(0, "load_shift"),
    _entry(0, "no_charge_window", adjustment={"hours": [1], "factor": 0.5}),
    _entry(-1),
]


class TestErrorContract:
    def test_error_is_guardrail_validation_error(self) -> None:
        error = _failure([_entry(0, adjustment={"hours": [13], "factor": 5.0})])
        assert isinstance(error, GuardrailValidationError)

    def test_error_is_not_an_assertion_error(self) -> None:
        assert not issubclass(GuardrailValidationError, AssertionError)

    def test_error_carries_rule_and_note_index(self) -> None:
        error = _failure([_entry(0, "no_op", applies=True)])
        assert error.rule == "no_op.applies"
        assert error.note_index == 0

    def test_error_message_is_non_empty(self) -> None:
        error = _failure([_entry(0, "no_op", applies=True)])
        assert str(error).strip()

    @pytest.mark.parametrize("entry", INVALID_ENTRIES)
    def test_invalid_output_never_becomes_no_op(self, entry) -> None:
        # A violation must raise; it must never return a no_op interpretation.
        with pytest.raises(GuardrailValidationError):
            _validate([entry])

    def test_valid_input_is_not_mutated(self) -> None:
        entries = [_entry(0), _entry(1, "no_op")]
        snapshot = copy.deepcopy(entries)
        _validate(entries, notes=_notes(2))
        assert entries == snapshot

    def test_invalid_input_is_not_mutated(self) -> None:
        entries = [_entry(0, "no_charge_window", adjustment={"hours": [12, 11]})]
        snapshot = copy.deepcopy(entries)
        with pytest.raises(GuardrailValidationError):
            _validate(entries)
        assert entries == snapshot

    def test_no_op_only_from_explicit_no_op_entry(self) -> None:
        result = _validate([_entry(0, "no_op")])
        assert result[0].directive_type is DirectiveType.NO_OP
        assert result[0].applies is False