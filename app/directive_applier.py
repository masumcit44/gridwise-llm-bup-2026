"""Deterministic directive application.

Compiles guardrail-validated :class:`~app.schemas.DirectiveInterpretation`
objects into the exact optimization inputs used by :mod:`app.optimizer`.

Official effects (Problem Statement 5.3, SPEC_AUDIT.md 2.4):

- ``solar_reduction``:          ``effective_solar[h] *= factor``
- ``minimum_battery_reserve``:  ``active_minimum_energy[h] = max(current, value)``
- ``no_charge_window``:         hours added to ``no_charge_hours``
- ``no_discharge_window``:      hours added to ``no_discharge_hours``
- ``max_grid_window``:          tightest cap wins per hour
- ``no_op``:                    no change to the optimization model

The strict guardrail layer (:mod:`app.guardrails`) is the trust boundary. This
module therefore performs no revalidation and no repair: it only compiles the
already-validated directives. Input request objects and directive objects are
never mutated.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import cast

from app.schemas import (
    BatteryRequest,
    DirectiveInterpretation,
    DirectiveType,
    HourRequest,
    MaxGridWindowAdjustment,
    MinimumBatteryReserveAdjustment,
    NoChargeWindowAdjustment,
    NoDischargeWindowAdjustment,
    SolarReductionAdjustment,
)

__all__ = ["HOURS_PER_DAY", "OptimizationInputs", "apply_directives"]

#: The scenario horizon is fixed at exactly 24 hourly entries (hours 0..23).
HOURS_PER_DAY = 24


@dataclass(frozen=True)
class OptimizationInputs:
    """Deterministic optimizer inputs compiled from validated directives.

    ``effective_solar`` and ``active_minimum_energy`` are indexed by hour
    (0..23). ``max_grid_by_hour`` holds only the hours that carry a cap. Every
    compiled input starts from the unmodified request values.
    """

    effective_solar: list[float]
    active_minimum_energy: list[float]
    no_charge_hours: set[int]
    no_discharge_hours: set[int]
    max_grid_by_hour: dict[int, float]


def _by_hour(hours: Sequence[HourRequest], attribute: str) -> list[float]:
    """Index an hourly request field by ``hour`` (request order is irrelevant)."""
    values = [0.0] * HOURS_PER_DAY
    for entry in hours:
        values[entry.hour] = float(getattr(entry, attribute))
    return values


def apply_directives(
    directives: Sequence[DirectiveInterpretation],
    hours: Sequence[HourRequest],
    battery: BatteryRequest,
) -> OptimizationInputs:
    """Compile validated directives into deterministic optimization inputs.

    :param directives: guardrail-validated directive interpretations, one per
        operator note (already in ``note_index`` order).
    :param hours: the 24 request hour entries (any order; indexed by ``hour``).
    :param battery: battery parameters. ``initial_energy_kwh``, ``capacity_kwh``
        and the rate limits are read by the optimizer; ``minimum_energy_kwh``
        seeds every ``active_minimum_energy`` slot.
    """
    base_minimum_energy = float(battery.minimum_energy_kwh)
    effective_solar = _by_hour(hours, "solar_kwh")
    active_minimum_energy = [base_minimum_energy] * HOURS_PER_DAY
    no_charge_hours: set[int] = set()
    no_discharge_hours: set[int] = set()
    max_grid_by_hour: dict[int, float] = {}

    for directive in directives:
        directive_type = directive.directive_type

        if directive_type is DirectiveType.NO_OP:
            # Explicitly no change to the optimization model.
            continue

        if directive_type is DirectiveType.SOLAR_REDUCTION:
            reduction = cast(
                SolarReductionAdjustment, directive.structured_adjustment
            )
            factor = float(reduction.factor)
            for hour in reduction.hours:
                effective_solar[hour] *= factor
            continue

        if directive_type is DirectiveType.MINIMUM_BATTERY_RESERVE:
            reserve = cast(
                MinimumBatteryReserveAdjustment, directive.structured_adjustment
            )
            required = float(reserve.minimum_energy_kwh)
            for hour in reserve.hours:
                active_minimum_energy[hour] = max(
                    active_minimum_energy[hour], required
                )
            continue

        if directive_type is DirectiveType.NO_CHARGE_WINDOW:
            window = cast(
                NoChargeWindowAdjustment, directive.structured_adjustment
            )
            no_charge_hours.update(window.hours)
            continue

        if directive_type is DirectiveType.NO_DISCHARGE_WINDOW:
            window = cast(
                NoDischargeWindowAdjustment, directive.structured_adjustment
            )
            no_discharge_hours.update(window.hours)
            continue

        if directive_type is DirectiveType.MAX_GRID_WINDOW:
            window = cast(
                MaxGridWindowAdjustment, directive.structured_adjustment
            )
            cap = float(window.max_grid_kwh)
            for hour in window.hours:
                existing = max_grid_by_hour.get(hour)
                # Multiple valid caps on the same hour: keep the tightest one.
                max_grid_by_hour[hour] = (
                    cap if existing is None else min(existing, cap)
                )
            continue

        # Unreachable for guardrail-validated input (only the six official
        # types are accepted). Fail loudly rather than silently ignoring an
        # unknown effect.
        raise ValueError(f"unsupported directive type: {directive_type!r}")

    return OptimizationInputs(
        effective_solar=effective_solar,
        active_minimum_energy=active_minimum_energy,
        no_charge_hours=no_charge_hours,
        no_discharge_hours=no_discharge_hours,
        max_grid_by_hour=max_grid_by_hour,
    )