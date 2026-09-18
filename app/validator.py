"""Independent replay validation of the final 24-hour plan.

The optimizer output is never trusted blindly: this module replays hours 0..23
from scratch using the original request data, the battery configuration and the
compiled directive inputs, and fails loudly on the first violation.

Official absolute tolerance is 0.01 kWh / 0.01 BDT (SPEC_AUDIT.md 2.16). All
comparisons here use that tolerance. Nothing is clipped, clamped, repaired or
mutated: every violation raises :class:`PlanValidationError` carrying a safe
machine-readable ``rule`` identifier and the offending ``hour`` when relevant.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Optional

from app.directive_applier import HOURS_PER_DAY, OptimizationInputs
from app.schemas import BatteryAction, BatteryRequest, HourRequest

__all__ = [
    "OFFICIAL_TOLERANCE",
    "PlanTotals",
    "PlanValidationError",
    "recalculate_totals",
    "validate_and_recalculate",
    "validate_hourly_plan",
]

#: Official judging tolerance for floating-point comparisons.
OFFICIAL_TOLERANCE = 0.01

_NUMERIC_FIELDS = (
    "grid_kwh",
    "solar_used_kwh",
    "battery_kwh",
    "battery_energy_after_kwh",
)
_MISSING = object()


class PlanValidationError(Exception):
    """Raised when the replayed hourly plan violates an official rule.

    ``rule`` is a stable machine-readable identifier of the violated rule and
    ``hour`` is the offending hour when the rule is hour-scoped. Messages are
    concise and contain no secrets, stack traces or provider details.
    """

    def __init__(
        self,
        message: str,
        *,
        rule: str,
        hour: Optional[int] = None,
    ) -> None:
        self.rule = rule
        self.hour = hour
        super().__init__(message)


@dataclass(frozen=True)
class PlanTotals:
    """Aggregates recalculated from the hourly plan (never from solver totals)."""

    total_grid_kwh: float
    total_cost_bdt: float
    peak_grid_kwh: float


def _error(
    message: str,
    rule: str,
    hour: Optional[int] = None,
) -> PlanValidationError:
    return PlanValidationError(message, rule=rule, hour=hour)


def _field(entry: Any, name: str) -> Any:
    """Read a plan field from a mapping or a model without mutating either."""
    if isinstance(entry, Mapping):
        return entry.get(name, _MISSING)
    return getattr(entry, name, _MISSING)


def _by_hour(hours: Sequence[HourRequest], attribute: str) -> list[float]:
    """Index an hourly request field by ``hour`` (request order is irrelevant)."""
    values = [0.0] * HOURS_PER_DAY
    for entry in hours:
        values[entry.hour] = float(getattr(entry, attribute))
    return values


def _finite(entry: Any, name: str, hour: int) -> float:
    """Return a required finite numeric plan field."""
    raw = _field(entry, name)
    if raw is _MISSING:
        raise _error(
            f"hour {hour}: required field {name} is missing",
            rule="plan.missing_field",
            hour=hour,
        )
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise _error(
            f"hour {hour}: {name} must be a finite number, "
            f"got {type(raw).__name__}",
            rule="plan.finite",
            hour=hour,
        )
    number = float(raw)
    if not math.isfinite(number):
        raise _error(
            f"hour {hour}: {name} must be finite, got {number!r}",
            rule="plan.finite",
            hour=hour,
        )
    return number


def _as_entries(plan: Any) -> list[Any]:
    """Return the plan as exactly 24 entries, or fail."""
    if isinstance(plan, (str, bytes)) or not isinstance(plan, Sequence):
        raise _error(
            f"hourly_plan must be a list of entries, got {type(plan).__name__}",
            rule="plan.type",
        )
    entries = list(plan)
    if len(entries) != HOURS_PER_DAY:
        raise _error(
            f"hourly_plan must contain exactly 24 entries, got {len(entries)}",
            rule="plan.length",
        )
    return entries


def validate_hourly_plan(
    plan: Any,
    *,
    hours: Sequence[HourRequest],
    battery: BatteryRequest,
    inputs: OptimizationInputs,
    tolerance: float = OFFICIAL_TOLERANCE,
) -> None:
    """Replay-validate a complete 24-hour plan against every official rule.

    ``plan`` may be a sequence of :class:`~app.schemas.HourlyPlanEntry` models
    or of plain mappings; the validator only reads it. Raises
    :class:`PlanValidationError` on the first violation.

    :raises PlanValidationError: when the plan length/order is wrong, a value is
        missing, non-finite or negative, the battery action, transition, rate
        limits or bounds are wrong, the solar ceiling or hourly energy balance
        is broken, a directive window is violated, or the plan is not
        end-of-day neutral.
    """
    entries = _as_entries(plan)

    if len(inputs.effective_solar) != HOURS_PER_DAY or (
        len(inputs.active_minimum_energy) != HOURS_PER_DAY
    ):
        raise _error(
            "optimization inputs must cover all 24 hours",
            rule="inputs.length",
        )

    demand = _by_hour(hours, "demand_kwh")
    capacity = float(battery.capacity_kwh)
    initial_energy = float(battery.initial_energy_kwh)
    max_charge = float(battery.max_charge_kwh_per_hour)
    max_discharge = float(battery.max_discharge_kwh_per_hour)

    previous_energy = initial_energy

    for position, entry in enumerate(entries):
        hour = position

        raw_hour = _field(entry, "hour")
        if raw_hour is _MISSING:
            raise _error(
                f"entry {position}: required field hour is missing",
                rule="plan.missing_field",
                hour=position,
            )
        if (
            isinstance(raw_hour, bool)
            or not isinstance(raw_hour, int)
            or int(raw_hour) != position
        ):
            raise _error(
                f"hourly_plan must list hours 0 through 23 in order; "
                f"entry {position} has hour={raw_hour!r}",
                rule="plan.hour_order",
                hour=position,
            )

        values = {name: _finite(entry, name, hour) for name in _NUMERIC_FIELDS}

        for name in ("grid_kwh", "solar_used_kwh", "battery_kwh"):
            if values[name] < -tolerance:
                raise _error(
                    f"hour {hour}: {name} must be non-negative, "
                    f"got {values[name]!r}",
                    rule="plan.non_negative",
                    hour=hour,
                )

        raw_action = _field(entry, "battery_action")
        if raw_action is _MISSING:
            raise _error(
                f"hour {hour}: required field battery_action is missing",
                rule="plan.missing_field",
                hour=hour,
            )
        try:
            action = BatteryAction(raw_action)
        except ValueError:
            raise _error(
                f"hour {hour}: battery_action must be charge, discharge or "
                f"idle, got {raw_action!r}",
                rule="battery_action.invalid",
                hour=hour,
            ) from None

        magnitude = values["battery_kwh"]
        charge = magnitude if action is BatteryAction.CHARGE else 0.0
        discharge = magnitude if action is BatteryAction.DISCHARGE else 0.0

        if action is BatteryAction.IDLE and magnitude > tolerance:
            raise _error(
                f"hour {hour}: idle requires battery_kwh=0, got {magnitude!r}",
                rule="battery.idle_magnitude",
                hour=hour,
            )
        if charge > max_charge + tolerance:
            raise _error(
                f"hour {hour}: charge {charge!r} exceeds "
                f"max_charge_kwh_per_hour {max_charge!r}",
                rule="battery.charge_rate",
                hour=hour,
            )
        if discharge > max_discharge + tolerance:
            raise _error(
                f"hour {hour}: discharge {discharge!r} exceeds "
                f"max_discharge_kwh_per_hour {max_discharge!r}",
                rule="battery.discharge_rate",
                hour=hour,
            )

        # Replay the battery state transition and compare with the report.
        expected_after = previous_energy + charge - discharge
        reported_after = values["battery_energy_after_kwh"]
        if abs(reported_after - expected_after) > tolerance:
            raise _error(
                f"hour {hour}: battery_energy_after_kwh {reported_after!r} does "
                f"not match the replayed state {expected_after!r}",
                rule="battery.transition",
                hour=hour,
            )

        active_minimum = float(inputs.active_minimum_energy[hour])
        if reported_after < active_minimum - tolerance:
            raise _error(
                f"hour {hour}: battery energy {reported_after!r} is below the "
                f"active minimum {active_minimum!r}",
                rule="battery.minimum",
                hour=hour,
            )
        if reported_after > capacity + tolerance:
            raise _error(
                f"hour {hour}: battery energy {reported_after!r} exceeds "
                f"capacity {capacity!r}",
                rule="battery.capacity",
                hour=hour,
            )

        effective_solar = float(inputs.effective_solar[hour])
        if values["solar_used_kwh"] > effective_solar + tolerance:
            raise _error(
                f"hour {hour}: solar_used_kwh {values['solar_used_kwh']!r} "
                f"exceeds effective solar {effective_solar!r}",
                rule="solar.ceiling",
                hour=hour,
            )

        supplied = values["grid_kwh"] + values["solar_used_kwh"] + discharge
        required = demand[hour] + charge
        if abs(supplied - required) > tolerance:
            raise _error(
                f"hour {hour}: energy balance fails: supplied {supplied!r} "
                f"!= required {required!r}",
                rule="energy.balance",
                hour=hour,
            )

        if hour in inputs.no_charge_hours and charge > tolerance:
            raise _error(
                f"hour {hour}: charging is forbidden by no_charge_window",
                rule="directive.no_charge",
                hour=hour,
            )
        if hour in inputs.no_discharge_hours and discharge > tolerance:
            raise _error(
                f"hour {hour}: discharging is forbidden by no_discharge_window",
                rule="directive.no_discharge",
                hour=hour,
            )
        grid_cap = inputs.max_grid_by_hour.get(hour)
        if grid_cap is not None and values["grid_kwh"] > float(grid_cap) + tolerance:
            raise _error(
                f"hour {hour}: grid_kwh {values['grid_kwh']!r} exceeds the "
                f"max_grid_window cap {grid_cap!r}",
                rule="directive.max_grid",
                hour=hour,
            )

        previous_energy = expected_after

    if abs(previous_energy - initial_energy) > tolerance:
        raise _error(
            f"end-of-day battery energy {previous_energy!r} must equal the "
            f"initial energy {initial_energy!r}",
            rule="battery.end_of_day",
            hour=HOURS_PER_DAY - 1,
        )


def recalculate_totals(
    plan: Any,
    *,
    hours: Sequence[HourRequest],
) -> PlanTotals:
    """Recalculate the official aggregates from the hourly plan alone.

    Call this only after :func:`validate_hourly_plan` has accepted the plan.
    No solver total or previously reported total is ever trusted.
    """
    entries = list(plan) if isinstance(plan, Sequence) else []
    if not entries:
        raise _error("hourly_plan must not be empty", rule="plan.length")

    tariffs = _by_hour(hours, "tariff_bdt_per_kwh")
    total_grid = 0.0
    total_cost = 0.0
    peak_grid = 0.0

    for position, entry in enumerate(entries):
        hour = _finite(entry, "hour", position)
        grid = _finite(entry, "grid_kwh", position)
        total_grid += grid
        total_cost += grid * tariffs[int(hour)]
        peak_grid = max(peak_grid, grid)

    return PlanTotals(
        total_grid_kwh=total_grid,
        total_cost_bdt=total_cost,
        peak_grid_kwh=peak_grid,
    )


def validate_and_recalculate(
    plan: Any,
    *,
    hours: Sequence[HourRequest],
    battery: BatteryRequest,
    inputs: OptimizationInputs,
    tolerance: float = OFFICIAL_TOLERANCE,
) -> PlanTotals:
    """Replay-validate the plan, then recalculate totals from that same plan."""
    validate_hourly_plan(
        plan,
        hours=hours,
        battery=battery,
        inputs=inputs,
        tolerance=tolerance,
    )
    return recalculate_totals(plan, hours=hours)