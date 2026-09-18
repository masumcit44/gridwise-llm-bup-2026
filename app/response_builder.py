"""Assembly of the official successful POST /optimize-energy response.

The hourly plan remains the source of truth: aggregate totals are recalculated
from it with :func:`app.validator.recalculate_totals`, and only those final
aggregates are rounded (to at most two decimal places). Hourly plan values are
never rounded or rewritten, so replay validity is preserved.

``plan_summary`` is generated deterministically from the plan and totals. No
language model is used for it.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from app.schemas import (
    BatteryAction,
    DirectiveInterpretation,
    HourlyPlanEntry,
    OptimizeEnergyRequest,
    OptimizeEnergyResponse,
)
from app.validator import PlanTotals, recalculate_totals

__all__ = [
    "TOTAL_DECIMAL_PLACES",
    "build_plan_summary",
    "build_success_response",
    "response_payload",
    "round_total",
]

#: Final reported aggregates are rounded to at most this many decimal places.
TOTAL_DECIMAL_PLACES = 2


def round_total(value: float) -> float:
    """Round one final aggregate; hourly plan entries are never rounded."""
    return round(float(value), TOTAL_DECIMAL_PLACES)


def build_plan_summary(
    plan: Sequence[HourlyPlanEntry],
    totals: PlanTotals,
    directives: Sequence[DirectiveInterpretation],
) -> str:
    """Build a short, deterministic, LLM-free summary of the final strategy."""
    applicable = sum(1 for directive in directives if directive.applies)
    charged = sum(
        entry.battery_kwh
        for entry in plan
        if entry.battery_action is BatteryAction.CHARGE
    )
    discharged = sum(
        entry.battery_kwh
        for entry in plan
        if entry.battery_action is BatteryAction.DISCHARGE
    )
    return (
        f"{applicable} applicable directive(s); grid "
        f"{round_total(totals.total_grid_kwh):.2f} kWh at "
        f"{round_total(totals.total_cost_bdt):.2f} BDT, peak "
        f"{round_total(totals.peak_grid_kwh):.2f} kWh; battery charged "
        f"{round_total(charged):.2f} kWh, discharged "
        f"{round_total(discharged):.2f} kWh."
    )


def build_success_response(
    request: OptimizeEnergyRequest,
    directives: Sequence[DirectiveInterpretation],
    plan: Sequence[HourlyPlanEntry],
) -> OptimizeEnergyResponse:
    """Build the official successful response from a replay-validated plan.

    ``request`` supplies the echoed ``scenario_id`` and the tariffs used to
    recalculate cost. ``plan`` must already have been replay-validated by
    :func:`app.validator.validate_hourly_plan`. Totals are recalculated here
    from the plan itself; solver-reported totals are never trusted.
    """
    totals = recalculate_totals(plan, hours=request.hours)
    return OptimizeEnergyResponse(
        scenario_id=request.scenario_id,
        directive_interpretation=list(directives),
        hourly_plan=list(plan),
        total_grid_kwh=round_total(totals.total_grid_kwh),
        total_cost_bdt=round_total(totals.total_cost_bdt),
        peak_grid_kwh=round_total(totals.peak_grid_kwh),
        plan_summary=build_plan_summary(plan, totals, directives),
    )


def response_payload(response: OptimizeEnergyResponse) -> dict[str, Any]:
    """Serialize a validated response for JSON transport (Pydantic v2)."""
    return response.model_dump(mode="json")