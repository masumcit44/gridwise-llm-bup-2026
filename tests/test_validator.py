"""Focused deterministic tests for the final replay validator.

Each mutation starts from a real optimizer result and changes exactly one thing,
then asserts the specific ``PlanValidationError.rule`` that must fire.
"""

from __future__ import annotations

import copy
import dataclasses

import pytest

from app.directive_applier import OptimizationInputs, apply_directives
from app.optimizer import optimize_hourly_plan
from app.schemas import (
    BatteryAction,
    BatteryRequest,
    HourlyPlanEntry,
    HourRequest,
)
from app.validator import (
    OFFICIAL_TOLERANCE,
    PlanTotals,
    PlanValidationError,
    recalculate_totals,
    validate_and_recalculate,
    validate_hourly_plan,
)

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


def _arbitrage_tariffs() -> list[float]:
    """Cheap hour 3 (2 BDT) and expensive hour 20 (30 BDT), else 10 BDT."""
    tariffs = [10.0] * 24
    tariffs[3] = 2.0
    tariffs[20] = 30.0
    return tariffs


def _scenario() -> tuple[list[HourRequest], BatteryRequest, OptimizationInputs, list[HourlyPlanEntry]]:
    """Optimize the arbitrage scenario: charge at hour 3, else marginal flows."""
    hours = _hours(tariff=_arbitrage_tariffs())
    battery = _battery()
    inputs = apply_directives([], hours, battery)
    plan = optimize_hourly_plan(hours, battery, inputs)
    return hours, battery, inputs, plan


def _mutate(plan, hour: int, **updates) -> list[HourlyPlanEntry]:
    """Return a copy of the plan with one entry's fields replaced."""
    return [
        entry.model_copy(update=updates) if entry.hour == hour else entry
        for entry in plan
    ]


def _expect(plan, *, hours, battery, inputs, rule: str, hour=None) -> None:
    with pytest.raises(PlanValidationError) as excinfo:
        validate_hourly_plan(
            plan, hours=hours, battery=battery, inputs=inputs
        )
    assert excinfo.value.rule == rule
    if hour is not None:
        assert excinfo.value.hour == hour


def _non_neutral_plan(demand: float = 100.0, initial: float = 100.0):
    """Per-hour-valid plan that is deliberately not end-of-day neutral."""
    plan = []
    energy = initial
    for hour in range(24):
        if hour == 23:
            energy -= 10.0
            plan.append(
                HourlyPlanEntry(
                    hour=hour,
                    grid_kwh=demand - 10.0,
                    solar_used_kwh=0.0,
                    battery_action=BatteryAction.DISCHARGE,
                    battery_kwh=10.0,
                    battery_energy_after_kwh=energy,
                )
            )
        else:
            plan.append(
                HourlyPlanEntry(
                    hour=hour,
                    grid_kwh=demand,
                    solar_used_kwh=0.0,
                    battery_action=BatteryAction.IDLE,
                    battery_kwh=0.0,
                    battery_energy_after_kwh=energy,
                )
            )
    return plan


def _first_hour_with(plan, action: BatteryAction) -> int:
    """Return the first hour whose action matches (shape-independent tests)."""
    for entry in plan:
        if entry.battery_action is action:
            return entry.hour
    raise AssertionError(f"optimizer plan has no {action} hour")


class TestValidPlan:
    def test_valid_optimized_plan_passes_and_totals_recalculated(self) -> None:
        hours, battery, inputs, plan = _scenario()
        snapshot = copy.deepcopy(plan)
        totals = validate_and_recalculate(
            plan, hours=hours, battery=battery, inputs=inputs
        )
        assert plan == snapshot
        assert isinstance(totals, PlanTotals)
        assert totals.total_grid_kwh == pytest.approx(2400.0)
        assert totals.total_cost_bdt == pytest.approx(23800.0)
        assert totals.peak_grid_kwh == pytest.approx(150.0)
        assert plan[3].battery_action is BatteryAction.CHARGE
        assert plan[20].battery_action is BatteryAction.DISCHARGE


class TestStructureRules:
    def test_plan_length_and_hour_order_rejected(self) -> None:
        hours, battery, inputs, plan = _scenario()
        _expect(
            plan[:-1],
            hours=hours,
            battery=battery,
            inputs=inputs,
            rule="plan.length",
        )
        swapped = [plan[1], plan[0], *plan[2:]]
        _expect(
            swapped,
            hours=hours,
            battery=battery,
            inputs=inputs,
            rule="plan.hour_order",
            hour=0,
        )

    def test_negative_value_and_missing_field_rejected(self) -> None:
        hours, battery, inputs, plan = _scenario()
        _expect(
            _mutate(plan, 0, grid_kwh=-1.0),
            hours=hours,
            battery=battery,
            inputs=inputs,
            rule="plan.non_negative",
            hour=0,
        )
        raw = [entry.model_dump() for entry in plan]
        del raw[0]["grid_kwh"]
        _expect(
            raw,
            hours=hours,
            battery=battery,
            inputs=inputs,
            rule="plan.missing_field",
            hour=0,
        )

    def test_non_finite_value_and_idle_magnitude_rejected(self) -> None:
        hours, battery, inputs, plan = _scenario()
        _expect(
            _mutate(plan, 0, grid_kwh=float("nan")),
            hours=hours,
            battery=battery,
            inputs=inputs,
            rule="plan.finite",
            hour=0,
        )
        idle_hour = _first_hour_with(plan, BatteryAction.IDLE)
        _expect(
            _mutate(plan, idle_hour, battery_kwh=5.0),
            hours=hours,
            battery=battery,
            inputs=inputs,
            rule="battery.idle_magnitude",
            hour=idle_hour,
        )


class TestPhysicalRules:
    def test_energy_balance_and_battery_transition_rejected(self) -> None:
        hours, battery, inputs, plan = _scenario()
        _expect(
            _mutate(plan, 0, grid_kwh=105.0),
            hours=hours,
            battery=battery,
            inputs=inputs,
            rule="energy.balance",
            hour=0,
        )
        charge_hour = _first_hour_with(plan, BatteryAction.CHARGE)
        _expect(
            _mutate(plan, charge_hour, battery_kwh=10.0),
            hours=hours,
            battery=battery,
            inputs=inputs,
            rule="battery.transition",
            hour=charge_hour,
        )

    def test_rate_limits_rejected(self) -> None:
        hours, battery, inputs, plan = _scenario()
        charge_hour = _first_hour_with(plan, BatteryAction.CHARGE)
        _expect(
            _mutate(plan, charge_hour, battery_kwh=120.0),
            hours=hours,
            battery=battery,
            inputs=inputs,
            rule="battery.charge_rate",
            hour=charge_hour,
        )
        discharge_hour = _first_hour_with(plan, BatteryAction.DISCHARGE)
        _expect(
            _mutate(plan, discharge_hour, battery_kwh=120.0),
            hours=hours,
            battery=battery,
            inputs=inputs,
            rule="battery.discharge_rate",
            hour=discharge_hour,
        )

    def test_capacity_and_reserve_rejected(self) -> None:
        hours, battery, inputs, plan = _scenario()
        # A 100 kWh capacity is schema-valid, but the forced charge at hour 3
        # raises the battery to 150 kWh, which exceeds it.
        _expect(
            plan,
            hours=hours,
            battery=_battery(capacity_kwh=100.0),
            inputs=inputs,
            rule="battery.capacity",
        )
        raised = dataclasses.replace(
            inputs, active_minimum_energy=[250.0] * 24
        )
        _expect(
            plan,
            hours=hours,
            battery=battery,
            inputs=raised,
            rule="battery.minimum",
            hour=0,
        )

    def test_solar_ceiling_and_tolerance_rejected(self) -> None:
        hours, battery, inputs, plan = _scenario()
        _expect(
            _mutate(plan, 0, solar_used_kwh=5.0),
            hours=hours,
            battery=battery,
            inputs=inputs,
            rule="solar.ceiling",
            hour=0,
        )
        # Half the official tolerance is accepted; twice it is rejected.
        validate_hourly_plan(
            _mutate(plan, 0, grid_kwh=plan[0].grid_kwh + OFFICIAL_TOLERANCE / 2),
            hours=hours,
            battery=battery,
            inputs=inputs,
        )
        _expect(
            _mutate(plan, 0, grid_kwh=plan[0].grid_kwh + OFFICIAL_TOLERANCE * 2),
            hours=hours,
            battery=battery,
            inputs=inputs,
            rule="energy.balance",
            hour=0,
        )


class TestDirectiveRules:
    def test_directive_windows_rejected(self) -> None:
        hours, battery, inputs, plan = _scenario()
        charge_hour = _first_hour_with(plan, BatteryAction.CHARGE)
        blocked_charge = dataclasses.replace(
            inputs, no_charge_hours={charge_hour}
        )
        _expect(
            plan,
            hours=hours,
            battery=battery,
            inputs=blocked_charge,
            rule="directive.no_charge",
            hour=charge_hour,
        )
        discharge_hour = _first_hour_with(plan, BatteryAction.DISCHARGE)
        blocked_discharge = dataclasses.replace(
            inputs, no_discharge_hours={discharge_hour}
        )
        _expect(
            plan,
            hours=hours,
            battery=battery,
            inputs=blocked_discharge,
            rule="directive.no_discharge",
            hour=discharge_hour,
        )
        capped = dataclasses.replace(inputs, max_grid_by_hour={0: 1.0})
        _expect(
            plan,
            hours=hours,
            battery=battery,
            inputs=capped,
            rule="directive.max_grid",
            hour=0,
        )


class TestEndOfDayAndErrorContract:
    def test_end_of_day_violation_rejected(self) -> None:
        hours = _hours()
        battery = _battery()
        inputs = apply_directives([], hours, battery)
        plan = _non_neutral_plan()
        _expect(
            plan,
            hours=hours,
            battery=battery,
            inputs=inputs,
            rule="battery.end_of_day",
            hour=23,
        )

    def test_error_contract_is_explicit_and_secret_free(self) -> None:
        hours, battery, inputs, plan = _scenario()
        with pytest.raises(PlanValidationError) as excinfo:
            validate_hourly_plan(
                _mutate(plan, 0, grid_kwh=105.0),
                hours=hours,
                battery=battery,
                inputs=inputs,
            )
        error = excinfo.value
        assert not issubclass(PlanValidationError, AssertionError)
        assert error.rule == "energy.balance"
        assert error.hour == 0
        message = str(error)
        assert message.strip()
        assert "Traceback" not in message
        assert "File \"" not in message

    def test_recalculate_totals_from_plan(self) -> None:
        hours = _hours(tariff=[float(hour) for hour in range(24)])
        plan = [
            HourlyPlanEntry(
                hour=hour,
                grid_kwh=float(hour + 1),
                solar_used_kwh=0.0,
                battery_action=BatteryAction.IDLE,
                battery_kwh=0.0,
                battery_energy_after_kwh=100.0,
            )
            for hour in range(24)
        ]
        totals = recalculate_totals(plan, hours=hours)
        assert totals.total_grid_kwh == 300.0
        assert totals.total_cost_bdt == 4600.0
        assert totals.peak_grid_kwh == 24.0
        with pytest.raises(PlanValidationError) as excinfo:
            recalculate_totals([], hours=_hours())
        assert excinfo.value.rule == "plan.length"