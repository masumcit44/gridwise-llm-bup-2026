"""Focused deterministic tests for the official response builder and pipeline."""

from __future__ import annotations

import pytest

from app.guardrails import validate_directive_interpretation
from app.main import run_optimization_pipeline
from app.response_builder import (
    TOTAL_DECIMAL_PLACES,
    build_success_response,
    response_payload,
)
from app.schemas import (
    BatteryAction,
    BatteryRequest,
    HourlyPlanEntry,
    HourRequest,
    OptimizeEnergyRequest,
    OptimizeEnergyResponse,
)
from app.validator import recalculate_totals

OFFICIAL_FIELDS = {
    "scenario_id",
    "directive_interpretation",
    "hourly_plan",
    "total_grid_kwh",
    "total_cost_bdt",
    "peak_grid_kwh",
    "plan_summary",
}


def _hours(*, solar=0.0, demand=100.0, tariff=10.0) -> list[HourRequest]:
    def at(value, hour: int) -> float:
        if isinstance(value, (list, tuple)):
            return float(value[hour])
        return float(value)

    return [
        HourRequest(
            hour=hour,
            demand_kwh=at(demand, hour),
            solar_kwh=at(solar, hour),
            tariff_bdt_per_kwh=at(tariff, hour),
        )
        for hour in range(24)
    ]


def _battery(**overrides) -> BatteryRequest:
    base = {
        "capacity_kwh": 200.0,
        "initial_energy_kwh": 100.0,
        "minimum_energy_kwh": 40.0,
        "max_charge_kwh_per_hour": 50.0,
        "max_discharge_kwh_per_hour": 50.0,
    }
    base.update(overrides)
    return BatteryRequest(**base)


def _request(
    scenario_id: str = "TEST-01",
    hours: list[HourRequest] | None = None,
) -> OptimizeEnergyRequest:
    return OptimizeEnergyRequest(
        scenario_id=scenario_id,
        operator_notes=["Interpreted operator note."],
        hours=hours if hours is not None else _hours(),
        battery=_battery(),
    )


def _idle_plan(grid_by_hour: dict[int, float]) -> list[HourlyPlanEntry]:
    """Hand-built idle plan with the given grid values (energy stays at 100)."""
    return [
        HourlyPlanEntry(
            hour=hour,
            grid_kwh=grid_by_hour.get(hour, 100.0),
            solar_used_kwh=0.0,
            battery_action=BatteryAction.IDLE,
            battery_kwh=0.0,
            battery_energy_after_kwh=100.0,
        )
        for hour in range(24)
    ]


def _solar_reduction_directive(request: OptimizeEnergyRequest):
    return validate_directive_interpretation(
        [
            {
                "note_index": 0,
                "applies": True,
                "directive_type": "solar_reduction",
                "structured_adjustment": {"hours": [13, 14], "factor": 0.25},
                "explanation": "Reduced usable solar during cleaning.",
            }
        ],
        request.operator_notes,
        request.battery.capacity_kwh,
    )


class TestResponseFields:
    def test_scenario_id_echoed_with_24_entries_unchanged(self) -> None:
        plan = _idle_plan({0: 100.006, 5: 42.5})
        response = build_success_response(_request("SAMPLE-XYZ"), [], plan)
        assert response.scenario_id == "SAMPLE-XYZ"
        assert len(response.hourly_plan) == 24
        assert [entry.hour for entry in response.hourly_plan] == list(range(24))
        assert response.hourly_plan[0].grid_kwh == 100.006
        assert response.hourly_plan[5].grid_kwh == 42.5
        assert response.hourly_plan == plan

    def test_totals_and_peak_recalculated_from_plan(self) -> None:
        response = build_success_response(_request(), [], _idle_plan({}))
        assert response.total_grid_kwh == 2400.0
        assert response.total_cost_bdt == 24000.0
        assert response.peak_grid_kwh == 100.0
        peaked = build_success_response(
            _request(), [], _idle_plan({7: 175.0, 21: 160.0})
        )
        assert peaked.peak_grid_kwh == 175.0

    def test_cost_uses_request_tariffs(self) -> None:
        hours = _hours(tariff=[float(hour) for hour in range(24)])
        request = _request(hours=hours)
        response = build_success_response(request, [], _idle_plan({}))
        # sum(grid=100 * tariff=hour) over hours 0..23 = 100 * 276
        assert response.total_cost_bdt == 27600.0

    def test_totals_rounded_but_plan_values_untouched(self) -> None:
        plan = _idle_plan({0: 100.006, 1: 100.004})
        request = _request(hours=_hours(tariff=1.0))
        response = build_success_response(request, [], plan)

        assert response.total_grid_kwh == 2400.01
        assert response.total_cost_bdt == 2400.01
        assert response.peak_grid_kwh == 100.01
        # Hourly plan values remain the unrounded source of truth.
        assert response.hourly_plan[0].grid_kwh == 100.006
        assert response.hourly_plan[1].grid_kwh == 100.004
        assert TOTAL_DECIMAL_PLACES == 2


class TestSchemaAndSummary:
    def test_response_payload_schema_round_trip(self) -> None:
        response = build_success_response(_request("RT-1"), [], _idle_plan({}))
        payload = response_payload(response)

        assert set(payload) == OFFICIAL_FIELDS
        assert payload["scenario_id"] == "RT-1"
        assert payload["hourly_plan"][0]["battery_action"] == "idle"
        assert len(payload["hourly_plan"]) == 24
        assert payload["directive_interpretation"] == []

        rebuilt = OptimizeEnergyResponse(**payload)
        assert rebuilt.total_grid_kwh == response.total_grid_kwh
        assert rebuilt.plan_summary == response.plan_summary
        assert len(rebuilt.hourly_plan) == 24

    def test_plan_summary_short_deterministic_with_counts(self) -> None:
        first = build_success_response(_request(), [], _idle_plan({})).plan_summary
        second = build_success_response(_request(), [], _idle_plan({})).plan_summary

        assert first == second
        assert first.strip() == first
        assert "\n" not in first
        assert len(first) <= 200
        assert "2400.00" in first

        request = _request(hours=_hours(solar=60.0))
        directives = _solar_reduction_directive(request)
        response = build_success_response(
            request, directives, _idle_plan({})
        )
        assert "1 applicable directive(s)" in response.plan_summary


class TestDeterministicPipeline:
    def test_pipeline_end_to_end_valid_with_matching_totals(self) -> None:
        request = _request("E2E-1", _hours(solar=30.0))
        directives = _solar_reduction_directive(request)
        response = run_optimization_pipeline(request, directives)

        assert response.scenario_id == "E2E-1"
        assert len(response.hourly_plan) == 24
        assert len(response.directive_interpretation) == 1
        assert response.plan_summary

        payload = response_payload(response)
        assert set(payload) == OFFICIAL_FIELDS
        assert OptimizeEnergyResponse(**payload).scenario_id == "E2E-1"

        totals = recalculate_totals(response.hourly_plan, hours=request.hours)
        assert response.total_grid_kwh == pytest.approx(
            round(totals.total_grid_kwh, 2)
        )
        assert response.total_cost_bdt == pytest.approx(
            round(totals.total_cost_bdt, 2)
        )
        assert response.peak_grid_kwh == pytest.approx(
            round(totals.peak_grid_kwh, 2)
        )

    def test_pipeline_applies_directive_effect_and_handles_empty(self) -> None:
        request = _request("E2E-3", _hours(solar=60.0))
        baseline = run_optimization_pipeline(request, [])
        reduced = run_optimization_pipeline(
            request, _solar_reduction_directive(request)
        )
        # Curtailing solar in hours 13-14 must raise the grid total.
        assert reduced.total_grid_kwh > baseline.total_grid_kwh
        assert reduced.total_grid_kwh == pytest.approx(1050.0)
        assert baseline.total_grid_kwh == pytest.approx(960.0)

        empty = run_optimization_pipeline(_request("E2E-4"), [])
        assert empty.directive_interpretation == []
        assert empty.total_grid_kwh == 2400.0
        assert empty.total_cost_bdt == 24000.0