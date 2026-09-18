"""Strict deterministic guardrails for LLM directive interpretation.

This module is the trust boundary between untrusted structured output (raw
JSON-shaped data produced by a language model or any other generative
provider) and the deterministic optimization pipeline. Every field is checked
explicitly. Nothing is clipped, clamped, guessed, repaired, reordered, or
downgraded to ``no_op``.

Binding rules (SPEC_AUDIT.md 2.11 and 4; prompt Task-2 section A):

- Exactly one directive entry per operator note.
- ``note_index`` must be exactly 0 through N-1, in order.
- Only the six official directive types are supported.
- ``no_op`` requires ``applies=false`` and ``structured_adjustment=null``.
- Every non-``no_op`` directive requires ``applies=true`` plus the exact
  directive-specific ``structured_adjustment`` shape.
- ``hours`` must be a non-empty list of unique ascending integers 0..23.
- ``solar_reduction.factor`` must be finite and within 0..1 inclusive.
- ``minimum_battery_reserve.minimum_energy_kwh`` must be finite, non-negative,
  and must not exceed the battery capacity.
- ``max_grid_window.max_grid_kwh`` must be finite and non-negative.
- Unexpected extra fields and arbitrary adjustment dictionaries are rejected.

A technical failure or invalid structured output is never equivalent to
``no_op``: every violation raises :class:`GuardrailValidationError`.
"""

from __future__ import annotations

import math
import numbers
from collections.abc import Mapping, Sequence
from typing import Any, Optional

from pydantic import BaseModel, ValidationError

from app.schemas import (
    DirectiveInterpretation,
    DirectiveType,
    MaxGridWindowAdjustment,
    MinimumBatteryReserveAdjustment,
    NoChargeWindowAdjustment,
    NoDischargeWindowAdjustment,
    SolarReductionAdjustment,
)

__all__ = [
    "ADJUSTMENT_FIELDS",
    "ENTRY_FIELDS",
    "SUPPORTED_DIRECTIVE_TYPES",
    "GuardrailValidationError",
    "validate_directive_entry",
    "validate_directive_interpretation",
]


class GuardrailValidationError(Exception):
    """Raised when untrusted directive output violates a strict guardrail rule.

    ``rule`` is a stable machine-readable identifier of the violated rule and
    ``note_index`` identifies the offending operator note when known.

    The exception always means "this structured output is invalid". It is never
    converted into ``no_op``, never repaired, and never replaced by a default.
    Callers must map it to a controlled failure response; they must not treat
    it as a successful interpretation.
    """

    def __init__(
        self,
        message: str,
        *,
        rule: str,
        note_index: Optional[int] = None,
    ) -> None:
        self.rule = rule
        self.note_index = note_index
        super().__init__(message)


# The complete, exact field set of a directive_interpretation entry
# (Problem Statement 10.2). Any other key is an unexpected field.
ENTRY_FIELDS: frozenset[str] = frozenset(
    {
        "note_index",
        "applies",
        "directive_type",
        "structured_adjustment",
        "explanation",
    }
)

# The six official directive types. Nothing else is supported.
SUPPORTED_DIRECTIVE_TYPES: frozenset[DirectiveType] = frozenset(DirectiveType)

# Exact structured_adjustment shape per directive type (Problem Statement 4.1).
# ``no_op`` takes no adjustment object at all (it requires null).
ADJUSTMENT_FIELDS: dict[DirectiveType, frozenset[str]] = {
    DirectiveType.SOLAR_REDUCTION: frozenset({"hours", "factor"}),
    DirectiveType.MINIMUM_BATTERY_RESERVE: frozenset(
        {"hours", "minimum_energy_kwh"}
    ),
    DirectiveType.NO_CHARGE_WINDOW: frozenset({"hours"}),
    DirectiveType.NO_DISCHARGE_WINDOW: frozenset({"hours"}),
    DirectiveType.MAX_GRID_WINDOW: frozenset({"hours", "max_grid_kwh"}),
    DirectiveType.NO_OP: frozenset(),
}

_SUPPORTED_TYPE_NAMES: str = ", ".join(
    sorted(directive_type.value for directive_type in SUPPORTED_DIRECTIVE_TYPES)
)


# --------------------------------------------------------------------------- #
# Internal helpers
# --------------------------------------------------------------------------- #


def _error(
    message: str,
    rule: str,
    note_index: Optional[int] = None,
) -> GuardrailValidationError:
    """Build a contextual guardrail failure (never an AssertionError)."""
    return GuardrailValidationError(message, rule=rule, note_index=note_index)


def _field_names(fields: Any) -> list[str]:
    """Render field names deterministically, even for non-string keys."""
    return sorted(str(field) for field in fields)


def _is_integer(value: Any) -> bool:
    """True only for true integers; booleans are deliberately excluded."""
    return isinstance(value, numbers.Integral) and not isinstance(value, bool)


def _is_real_number(value: Any) -> bool:
    """True only for real numbers; booleans are deliberately excluded."""
    return isinstance(value, numbers.Real) and not isinstance(value, bool)


def _require_finite_number(
    value: Any,
    *,
    label: str,
    rule: str,
    note_index: int,
) -> float:
    """Return ``value`` as a finite float, or fail without coercing."""
    if not _is_real_number(value):
        raise _error(
            f"note {note_index}: {label} must be a finite number, "
            f"got {type(value).__name__}",
            rule=f"{rule}.type",
            note_index=note_index,
        )
    number = float(value)
    if not math.isfinite(number):
        raise _error(
            f"note {note_index}: {label} must be finite, got {number!r}",
            rule=f"{rule}.non_finite",
            note_index=note_index,
        )
    return number


def _validate_hours(value: Any, *, note_index: int) -> list[int]:
    """Validate an adjustment ``hours`` array without reordering or clipping."""
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise _error(
            f"note {note_index}: hours must be a non-empty list of unique "
            "integers from 0 through 23",
            rule="hours.type",
            note_index=note_index,
        )
    hours = list(value)
    if not hours:
        raise _error(
            f"note {note_index}: hours must not be empty",
            rule="hours.empty",
            note_index=note_index,
        )
    seen: set[int] = set()
    for hour in hours:
        if not _is_integer(hour) or int(hour) < 0 or int(hour) > 23:
            raise _error(
                f"note {note_index}: hours must be unique integers from 0 "
                f"through 23, got {hour!r}",
                rule="hours.range",
                note_index=note_index,
            )
        if int(hour) in seen:
            raise _error(
                f"note {note_index}: hours must be unique, {int(hour)} repeats",
                rule="hours.unique",
                note_index=note_index,
            )
        seen.add(int(hour))
    normalized = [int(hour) for hour in hours]
    if normalized != sorted(normalized):
        raise _error(
            f"note {note_index}: hours must be in ascending order, "
            f"got {normalized}",
            rule="hours.ascending",
            note_index=note_index,
        )
    return normalized


def _coerce_directive_type(value: Any, *, note_index: int) -> DirectiveType:
    """Map an untrusted ``directive_type`` to one of the six official types."""
    if isinstance(value, DirectiveType):
        return value
    if isinstance(value, str):
        try:
            return DirectiveType(value)
        except ValueError:
            raise _error(
                f"note {note_index}: unsupported directive_type {value!r}; "
                f"supported types are {_SUPPORTED_TYPE_NAMES}",
                rule="entry.directive_type.unsupported",
                note_index=note_index,
            ) from None
    raise _error(
        f"note {note_index}: directive_type must be a string, "
        f"got {type(value).__name__}",
        rule="entry.directive_type.type",
        note_index=note_index,
    )


def _validate_adjustment(
    value: Any,
    *,
    directive_type: DirectiveType,
    note_index: int,
    battery_capacity_kwh: float,
) -> Any:
    """Validate the exact adjustment shape for one directive type.

    The key set must match the official shape exactly: unexpected fields,
    missing fields, and arbitrary dictionaries are all rejected. Values are
    never clipped or clamped.
    """
    expected = ADJUSTMENT_FIELDS[directive_type]
    if isinstance(value, BaseModel):
        value = value.model_dump()
    if not isinstance(value, Mapping):
        raise _error(
            f"note {note_index}: structured_adjustment for "
            f"{directive_type.value} must be an object with fields "
            f"{_field_names(expected)}, got {type(value).__name__}",
            rule="directive.adjustment.type",
            note_index=note_index,
        )
    keys = set(value.keys())
    extra = keys - expected
    missing = expected - keys
    if extra:
        raise _error(
            f"note {note_index}: structured_adjustment for "
            f"{directive_type.value} contains unexpected field(s) "
            f"{_field_names(extra)}",
            rule="directive.adjustment.extra_fields",
            note_index=note_index,
        )
    if missing:
        raise _error(
            f"note {note_index}: structured_adjustment for "
            f"{directive_type.value} is missing required field(s) "
            f"{_field_names(missing)}",
            rule="directive.adjustment.missing_fields",
            note_index=note_index,
        )

    hours = _validate_hours(value["hours"], note_index=note_index)

    if directive_type is DirectiveType.SOLAR_REDUCTION:
        factor = _require_finite_number(
            value["factor"],
            label="factor",
            rule="solar_reduction.factor",
            note_index=note_index,
        )
        if factor < 0.0 or factor > 1.0:
            raise _error(
                f"note {note_index}: solar_reduction factor must be between "
                f"0 and 1 inclusive, got {factor!r}",
                rule="solar_reduction.factor.range",
                note_index=note_index,
            )
        return SolarReductionAdjustment(hours=hours, factor=factor)

    if directive_type is DirectiveType.MINIMUM_BATTERY_RESERVE:
        reserve = _require_finite_number(
            value["minimum_energy_kwh"],
            label="minimum_energy_kwh",
            rule="minimum_battery_reserve.minimum_energy_kwh",
            note_index=note_index,
        )
        if reserve < 0.0:
            raise _error(
                f"note {note_index}: minimum_energy_kwh must be non-negative, "
                f"got {reserve!r}",
                rule="minimum_battery_reserve.minimum_energy_kwh.negative",
                note_index=note_index,
            )
        if reserve > battery_capacity_kwh:
            raise _error(
                f"note {note_index}: minimum_energy_kwh {reserve!r} exceeds "
                f"battery capacity {battery_capacity_kwh!r}",
                rule="minimum_battery_reserve.minimum_energy_kwh.capacity",
                note_index=note_index,
            )
        return MinimumBatteryReserveAdjustment(
            hours=hours,
            minimum_energy_kwh=reserve,
        )

    if directive_type is DirectiveType.MAX_GRID_WINDOW:
        cap = _require_finite_number(
            value["max_grid_kwh"],
            label="max_grid_kwh",
            rule="max_grid_window.max_grid_kwh",
            note_index=note_index,
        )
        if cap < 0.0:
            raise _error(
                f"note {note_index}: max_grid_kwh must be non-negative, "
                f"got {cap!r}",
                rule="max_grid_window.max_grid_kwh.negative",
                note_index=note_index,
            )
        return MaxGridWindowAdjustment(hours=hours, max_grid_kwh=cap)

    if directive_type is DirectiveType.NO_CHARGE_WINDOW:
        return NoChargeWindowAdjustment(hours=hours)

    if directive_type is DirectiveType.NO_DISCHARGE_WINDOW:
        return NoDischargeWindowAdjustment(hours=hours)

    raise _error(
        f"note {note_index}: {directive_type.value} does not take a "
        "structured_adjustment object",
        rule="directive.adjustment.type",
        note_index=note_index,
    )


# --------------------------------------------------------------------------- #
# Entry / interpretation validation
# --------------------------------------------------------------------------- #


def _as_mapping(raw_entry: Any, *, position: int) -> Mapping[str, Any]:
    """Return the entry as a mapping, or fail with entry context."""
    if isinstance(raw_entry, DirectiveInterpretation):
        return raw_entry.model_dump()
    if isinstance(raw_entry, Mapping):
        return raw_entry
    raise _error(
        f"directive entry {position} must be an object, "
        f"got {type(raw_entry).__name__}",
        rule="entry.type",
        note_index=position,
    )


def _validate_entry(
    raw_entry: Any,
    *,
    position: int,
    note_count: int,
    battery_capacity_kwh: float,
) -> DirectiveInterpretation:
    """Strictly validate one raw directive entry without repairing it."""
    entry = _as_mapping(raw_entry, position=position)
    keys = set(entry.keys())
    extra = keys - ENTRY_FIELDS
    missing = ENTRY_FIELDS - keys
    if extra:
        raise _error(
            f"directive entry {position} contains unexpected field(s) "
            f"{_field_names(extra)}",
            rule="entry.extra_fields",
            note_index=position,
        )
    if missing:
        raise _error(
            f"directive entry {position} is missing required field(s) "
            f"{_field_names(missing)}",
            rule="entry.missing_fields",
            note_index=position,
        )

    raw_note_index = entry["note_index"]
    if not _is_integer(raw_note_index):
        raise _error(
            f"directive entry {position}: note_index must be an integer, "
            f"got {type(raw_note_index).__name__}",
            rule="entry.note_index.type",
            note_index=position,
        )
    note_index = int(raw_note_index)
    if note_index < 0:
        raise _error(
            f"directive entry {position}: note_index must not be negative, "
            f"got {note_index}",
            rule="entry.note_index.negative",
            note_index=position,
        )
    if note_index >= note_count:
        raise _error(
            f"directive entry {position}: note_index {note_index} is out of "
            f"range for {note_count} operator note(s)",
            rule="entry.note_index.out_of_range",
            note_index=position,
        )
    if note_index != position:
        raise _error(
            f"directive entry {position}: note_index must be exactly 0 "
            f"through N-1 in order, expected {position}, got {note_index}",
            rule="entry.note_index.order",
            note_index=position,
        )

    applies = entry["applies"]
    if not isinstance(applies, bool):
        raise _error(
            f"directive entry {position}: applies must be a boolean, "
            f"got {type(applies).__name__}",
            rule="entry.applies.type",
            note_index=position,
        )

    directive_type = _coerce_directive_type(
        entry["directive_type"],
        note_index=position,
    )

    explanation = entry["explanation"]
    if not isinstance(explanation, str) or len(explanation) < 1:
        raise _error(
            f"directive entry {position}: explanation must be a non-empty "
            f"string, got {type(explanation).__name__}",
            rule="entry.explanation.invalid",
            note_index=position,
        )

    adjustment_raw = entry["structured_adjustment"]
    if directive_type is DirectiveType.NO_OP:
        if applies is not False:
            raise _error(
                f"directive entry {position}: no_op requires applies=false",
                rule="no_op.applies",
                note_index=position,
            )
        if adjustment_raw is not None:
            raise _error(
                f"directive entry {position}: no_op requires "
                "structured_adjustment=null",
                rule="no_op.adjustment",
                note_index=position,
            )
        adjustment = None
    else:
        if applies is not True:
            raise _error(
                f"directive entry {position}: {directive_type.value} requires "
                "applies=true",
                rule="directive.applies",
                note_index=position,
            )
        if adjustment_raw is None:
            raise _error(
                f"directive entry {position}: {directive_type.value} requires "
                "a structured_adjustment object",
                rule="directive.adjustment.missing",
                note_index=position,
            )
        adjustment = _validate_adjustment(
            adjustment_raw,
            directive_type=directive_type,
            note_index=position,
            battery_capacity_kwh=battery_capacity_kwh,
        )

    try:
        return DirectiveInterpretation(
            note_index=note_index,
            applies=applies,
            directive_type=directive_type,
            structured_adjustment=adjustment,
            explanation=explanation,
        )
    except ValidationError as exc:
        raise _error(
            f"directive entry {position} failed final schema validation: {exc}",
            rule="entry.schema",
            note_index=position,
        ) from exc


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


def _validate_notes(operator_notes: Any) -> list[str]:
    """Validate the operator-note context used for the 1:1 mapping check."""
    if isinstance(operator_notes, (str, bytes)) or not isinstance(
        operator_notes, Sequence
    ):
        raise _error(
            "operator_notes must be a sequence of non-empty strings, "
            f"got {type(operator_notes).__name__}",
            rule="context.operator_notes",
        )
    notes = list(operator_notes)
    if any(not isinstance(note, str) or not note for note in notes):
        raise _error(
            "operator_notes must contain non-empty strings",
            rule="context.operator_notes",
        )
    return notes


def _validate_capacity(battery_capacity_kwh: Any) -> float:
    """Validate the battery capacity used for the reserve ceiling check."""
    if not _is_real_number(battery_capacity_kwh):
        raise _error(
            "battery_capacity_kwh must be a finite non-negative number, "
            f"got {type(battery_capacity_kwh).__name__}",
            rule="context.battery_capacity",
        )
    capacity = float(battery_capacity_kwh)
    if not math.isfinite(capacity) or capacity < 0.0:
        raise _error(
            "battery_capacity_kwh must be a finite non-negative number, "
            f"got {capacity!r}",
            rule="context.battery_capacity",
        )
    return capacity


def validate_directive_entry(
    raw_entry: Any,
    *,
    position: int,
    note_count: int,
    battery_capacity_kwh: float,
) -> DirectiveInterpretation:
    """Validate a single raw directive entry under strict guardrail rules.

    ``position`` is the zero-based position of the entry in the
    ``directive_interpretation`` array and ``note_count`` is the number of
    operator notes. The function raises :class:`GuardrailValidationError` for
    any violation and never repairs, clamps, or downgrades the entry.
    """
    if not _is_integer(position) or position < 0:
        raise _error(
            f"position must be a non-negative integer, got {position!r}",
            rule="context.position",
        )
    if not _is_integer(note_count) or note_count < 0:
        raise _error(
            f"note_count must be a non-negative integer, got {note_count!r}",
            rule="context.note_count",
        )
    capacity = _validate_capacity(battery_capacity_kwh)
    return _validate_entry(
        raw_entry,
        position=position,
        note_count=note_count,
        battery_capacity_kwh=capacity,
    )


def validate_directive_interpretation(
    entries: Any,
    operator_notes: Sequence[str],
    battery_capacity_kwh: float,
) -> list[DirectiveInterpretation]:
    """Validate untrusted ``directive_interpretation`` output.

    Enforces the binding guardrail contract (SPEC_AUDIT.md 2.11, 4):

    - exactly one entry per operator note, in ``note_index`` order 0..N-1;
    - only the six official directive types;
    - ``no_op`` with ``applies=false`` and ``structured_adjustment=null``;
    - every non-``no_op`` directive with ``applies=true`` and the exact
      directive-specific adjustment shape;
    - hours unique/ascending integers 0..23, factor in 0..1, reserve within
      battery capacity, and non-negative ``max_grid_kwh``;
    - no extra fields and no arbitrary adjustment dictionaries.

    Returns fully validated :class:`~app.schemas.DirectiveInterpretation`
    models. Any violation raises :class:`GuardrailValidationError`; invalid
    output is never converted into ``no_op``.
    """
    notes = _validate_notes(operator_notes)
    capacity = _validate_capacity(battery_capacity_kwh)
    note_count = len(notes)

    if isinstance(entries, (str, bytes)) or not isinstance(entries, Sequence):
        raise _error(
            "directive_interpretation must be a list of entries, "
            f"got {type(entries).__name__}",
            rule="interpretation.type",
        )
    entry_list = list(entries)
    if len(entry_list) != note_count:
        raise _error(
            "directive_interpretation must contain exactly one entry per "
            f"operator note: expected {note_count}, got {len(entry_list)}",
            rule="interpretation.count",
        )

    return [
        _validate_entry(
            entry,
            position=position,
            note_count=note_count,
            battery_capacity_kwh=capacity,
        )
        for position, entry in enumerate(entry_list)
    ]
