"""Focused deterministic tests for the PuLP/CBC 24-hour optimizer.

Covers the base feasible schedule, energy balance, battery bounds, rate
limits, end-of-day neutrality, the solar ceiling, all five directive effects,
single-direction battery operation, optimal cost behaviour, and controlled
``SolverError`` handling for infeasible input.
"""

from __future__ import annotations

import math

import pytest

from app.directive_applier import OptimizationInputs, apply_directives
from app.optimizer import SolverError, optimize_hourly_plan
from app.schemas import (
    BatteryAction,
    BatteryRequest,
    DirectiveInterpretation,
    DirectiveType,
    HourlyPlanEntry,
    HourRequest,
    MaxGridWindowAdjustment,
    MinimumBatteryReserveAdjustment,
    NoChargeWindowAdjustment,
    NoDischargeWindowAdjustment,
    SolarReductionAdjustment,
)

TOLERANCE = 1e-6


def _hours(*, solar=0.0, demand=100.0, tariff=10.0) -> list[HourRequest]:
    """Build 24 hour entries; each field accepts a scalar or a 24-list."""
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


def _directive(
    note_index: int,
    directive_type: DirectiveType,
    adjustment=None,
    *,
    applies: bool = True,
) -> DirectiveInterpretation:
    return DirectiveInterpretation(
        note_index=note_index,
        applies=applies,
        directive_type=directive_type,
        structured_adjustment=adjustment,
        explanation="test interpretation",
    )


def _compiled(
    hours: list[HourRequest],
    battery: BatteryRequest,
    directives=(),
) -> OptimizationInputs:
    return apply_directives(list(directives), hours, battery)


def _solve(
    hours: list[HourRequest],
    battery: BatteryRequest,
    directives=(),
) -> list[HourlyPlanEntry]:
    inputs = _compiled(hours, battery, directives)
    return optimize_hourly_plan(hours, battery, inputs)


def _arbitrage_tariffs() -> list[float]:
    """Cheap hour 3 (2 BDT) and expensive hour 20 (30 BDT), else 10 BDT."""
    tariffs = [10.0] * 24
    tariffs[3] = 2.0
    tariffs[20] = 30.0
    return tariffs


def _cost(plan: list[HourlyPlanEntry], hours: list[HourRequest]) -> float:
    tariffs = {entry.hour: entry.tariff_bdt_per_kwh for entry in hours}
    return sum(entry.grid_kwh * tariffs[entry.hour] for entry in plan)


def _actions(plan: list[HourlyPlanEntry]) -> dict[int, BatteryAction]:
    return {entry.hour: entry.battery_action for entry in plan}


def _charge_of(entry: HourlyPlanEntry) -> float:
    return entry.battery_kwh if entry.battery_action is BatteryAction.CHARGE else 0.0


def _discharge_of(entry: HourlyPlanEntry) -> float:
    return (
        entry.battery_kwh
        if entry.battery_action is BatteryAction.DISCHARGE
        else 0.0
    )


class TestBaseScenario:
    def test_returns_exactly_24_ordered_entries(self) -> None:
        plan = _solve(_hours(), _battery())
        assert len(plan) == 24
        assert [entry.hour for entry in plan] == list(range(24))

    def test_all_values_finite_and_non_negative(self) -> None:
        plan = _solve(_hours(), _battery())
        for entry in plan:
            assert entry.battery_action in set(BatteryAction)
            for value in (
                entry.grid_kwh,
                entry.solar_used_kwh,
                entry.battery_kwh,
                entry.battery_energy_after_kwh,
            ):
                assert math.isfinite(value)
                assert value >= 0.0

    def test_flat_tariff_total_grid_is_fixed(self) -> None:
        # Flat tariffs make battery cycling cost-neutral, so any cost-optimal
        # schedule is legal: total grid energy (and cost) is what is fixed.
        plan = _solve(_hours(), _battery())
        assert sum(entry.grid_kwh for entry in plan) == pytest.approx(
            2400.0, abs=TOLERANCE
        )

    def test_grid_only_supply_when_battery_is_disabled(self) -> None:
        battery = _battery(
            max_charge_kwh_per_hour=0.0, max_discharge_kwh_per_hour=0.0
        )
        plan = _solve(_hours(), battery)
        for entry in plan:
            assert entry.grid_kwh == pytest.approx(100.0, abs=TOLERANCE)
            assert entry.battery_action is BatteryAction.IDLE
            assert entry.battery_kwh == 0.0

    def test_energy_balance_every_hour(self) -> None:
        plan = _solve(_hours(solar=25.0, tariff=_arbitrage_tariffs()), _battery())
        for entry in plan:
            supplied = entry.grid_kwh + entry.solar_used_kwh + _discharge_of(entry)
            assert supplied == pytest.approx(
                100.0 + _charge_of(entry), abs=TOLERANCE
            )

    def test_battery_bounds_and_capacity_respected(self) -> None:
        battery = _battery()
        hours = _hours(tariff=_arbitrage_tariffs())
        inputs = _compiled(hours, battery)
        plan = optimize_hourly_plan(hours, battery, inputs)
        for entry in plan:
            assert entry.battery_energy_after_kwh >= (
                inputs.active_minimum_energy[entry.hour] - TOLERANCE
            )
            assert entry.battery_energy_after_kwh <= (
                battery.capacity_kwh + TOLERANCE
            )

    def test_rate_limits_respected(self) -> None:
        battery = _battery()
        plan = _solve(_hours(tariff=_arbitrage_tariffs()), battery)
        for entry in plan:
            if entry.battery_action is BatteryAction.CHARGE:
                assert entry.battery_kwh <= (
                    battery.max_charge_kwh_per_hour + TOLERANCE
                )
            elif entry.battery_action is BatteryAction.DISCHARGE:
                assert entry.battery_kwh <= (
                    battery.max_discharge_kwh_per_hour + TOLERANCE
                )

    def test_state_transition_matches_previous_energy(self) -> None:
        battery = _battery()
        plan = _solve(_hours(tariff=_arbitrage_tariffs()), battery)
        previous = battery.initial_energy_kwh
        for entry in plan:
            expected = previous + _charge_of(entry) - _discharge_of(entry)
            assert entry.battery_energy_after_kwh == pytest.approx(
                expected, abs=TOLERANCE
            )
            previous = entry.battery_energy_after_kwh

    def test_end_of_day_neutrality(self) -> None:
        battery = _battery()
        plan = _solve(_hours(tariff=_arbitrage_tariffs()), battery)
        assert plan[-1].battery_energy_after_kwh == pytest.approx(
            battery.initial_energy_kwh, abs=TOLERANCE
        )

    def test_zero_capacity_battery_is_feasible(self) -> None:
        battery = _battery(
            capacity_kwh=0.0,
            initial_energy_kwh=0.0,
            minimum_energy_kwh=0.0,
            max_charge_kwh_per_hour=0.0,
            max_discharge_kwh_per_hour=0.0,
        )
        plan = _solve(_hours(), battery)
        assert len(plan) == 24
        assert all(entry.battery_action is BatteryAction.IDLE for entry in plan)
        assert all(entry.battery_energy_after_kwh == 0.0 for entry in plan)


class TestDirectiveEffects:
    def test_solar_ceiling_respects_effective_solar(self) -> None:
        hours = _hours(solar=60.0, tariff=_arbitrage_tariffs())
        battery = _battery()
        directives = [
            _directive(
                0,
                DirectiveType.SOLAR_REDUCTION,
                SolarReductionAdjustment(hours=[10, 11, 12], factor=0.2),
            )
        ]
        inputs = _compiled(hours, battery, directives)
        plan = optimize_hourly_plan(hours, battery, inputs)

        assert inputs.effective_solar[10] == pytest.approx(12.0)
        for entry in plan:
            assert entry.solar_used_kwh <= (
                inputs.effective_solar[entry.hour] + TOLERANCE
            )
        for hour in (10, 11, 12):
            assert plan[hour].solar_used_kwh <= 12.0 + TOLERANCE

    def test_no_charge_window_blocks_charging(self) -> None:
        hours = _hours(tariff=_arbitrage_tariffs())
        battery = _battery()
        baseline = _solve(hours, battery)
        assert _cost(baseline, hours) == pytest.approx(23800.0, abs=TOLERANCE)

        directives = [
            _directive(
                0,
                DirectiveType.NO_CHARGE_WINDOW,
                NoChargeWindowAdjustment(hours=[3]),
            )
        ]
        plan = _solve(hours, battery, directives)

        assert _actions(plan)[3] is not BatteryAction.CHARGE
        assert plan[3].battery_kwh == 0.0
        # Blocking the cheapest charge hour costs the difference 2 BDT vs 10 BDT.
        assert _cost(plan, hours) == pytest.approx(24200.0, abs=TOLERANCE)
        assert _cost(plan, hours) > _cost(baseline, hours)

    def test_no_discharge_window_blocks_discharging(self) -> None:
        hours = _hours(tariff=_arbitrage_tariffs())
        battery = _battery()
        baseline = _solve(hours, battery)

        directives = [
            _directive(
                0,
                DirectiveType.NO_DISCHARGE_WINDOW,
                NoDischargeWindowAdjustment(hours=[20]),
            )
        ]
        plan = _solve(hours, battery, directives)

        assert _actions(plan)[20] is not BatteryAction.DISCHARGE
        assert plan[20].battery_kwh == 0.0
        assert _cost(plan, hours) > _cost(baseline, hours)
        assert _cost(plan, hours) == pytest.approx(24800.0, abs=TOLERANCE)

    def test_reserve_window_raises_battery_floor(self) -> None:
        hours = _hours()
        battery = _battery()
        directives = [
            _directive(
                0,
                DirectiveType.MINIMUM_BATTERY_RESERVE,
                MinimumBatteryReserveAdjustment(
                    hours=[18, 19], minimum_energy_kwh=150.0
                ),
            )
        ]
        plan = _solve(hours, battery, directives)

        for hour in (18, 19):
            assert plan[hour].battery_energy_after_kwh >= 150.0 - TOLERANCE
        assert plan[-1].battery_energy_after_kwh == pytest.approx(
            battery.initial_energy_kwh, abs=TOLERANCE
        )

    def test_max_grid_window_caps_grid_only_in_listed_hours(self) -> None:
        hours = _hours()
        battery = _battery()
        directives = [
            _directive(
                0,
                DirectiveType.MAX_GRID_WINDOW,
                MaxGridWindowAdjustment(hours=[19, 20], max_grid_kwh=80.0),
            )
        ]
        inputs = _compiled(hours, battery, directives)
        plan = optimize_hourly_plan(hours, battery, inputs)

        for hour in (19, 20):
            assert plan[hour].grid_kwh <= 80.0 + TOLERANCE
            # The cap forces battery discharge to cover the rest of demand.
            assert _discharge_of(plan[hour]) >= 20.0 - TOLERANCE

        # A higher peak outside the window is legal.
        assert max(entry.grid_kwh for entry in plan) > 80.0

        for entry in plan:
            supplied = entry.grid_kwh + entry.solar_used_kwh + _discharge_of(entry)
            assert supplied == pytest.approx(
                100.0 + _charge_of(entry), abs=TOLERANCE
            )

    def test_combined_reserve_and_grid_cap_stays_feasible(self) -> None:
        hours = _hours(tariff=_arbitrage_tariffs())
        battery = _battery()
        directives = [
            _directive(
                0,
                DirectiveType.MINIMUM_BATTERY_RESERVE,
                MinimumBatteryReserveAdjustment(
                    hours=[18, 19, 20, 21], minimum_energy_kwh=120.0
                ),
            ),
            _directive(
                1,
                DirectiveType.MAX_GRID_WINDOW,
                MaxGridWindowAdjustment(hours=[19, 20], max_grid_kwh=70.0),
            ),
        ]
        inputs = _compiled(hours, battery, directives)
        plan = optimize_hourly_plan(hours, battery, inputs)

        for hour in (19, 20):
            assert plan[hour].grid_kwh <= 70.0 + TOLERANCE
            # The cap forces the shortfall onto the battery (demand 100).
            assert _discharge_of(plan[hour]) >= 30.0 - TOLERANCE
        for hour in (18, 19, 20, 21):
            assert plan[hour].battery_energy_after_kwh >= 120.0 - TOLERANCE

        for entry in plan:
            assert entry.battery_energy_after_kwh <= (
                battery.capacity_kwh + TOLERANCE
            )
            supplied = entry.grid_kwh + entry.solar_used_kwh + _discharge_of(entry)
            assert supplied == pytest.approx(
                100.0 + _charge_of(entry), abs=TOLERANCE
            )
        assert plan[-1].battery_energy_after_kwh == pytest.approx(
            battery.initial_energy_kwh, abs=TOLERANCE
        )


class TestBatteryDirection:
    def test_charge_and_discharge_never_share_an_hour(self) -> None:
        plan = _solve(_hours(tariff=_arbitrage_tariffs()), _battery())
        for entry in plan:
            assert _charge_of(entry) == 0.0 or _discharge_of(entry) == 0.0
        # Non-vacuous: the arbitrage scenario exercises both directions.
        assert any(_charge_of(entry) > 0.0 for entry in plan)
        assert any(_discharge_of(entry) > 0.0 for entry in plan)

    def test_idle_hours_report_zero_battery_energy(self) -> None:
        plan = _solve(_hours(tariff=_arbitrage_tariffs()), _battery())
        for entry in plan:
            if entry.battery_action is BatteryAction.IDLE:
                assert entry.battery_kwh == 0.0


class TestOptimality:
    def test_battery_shifts_energy_to_cheaper_hours(self) -> None:
        hours = _hours(tariff=_arbitrage_tariffs())
        plan = _solve(hours, _battery())
        actions = _actions(plan)

        # Cheapest hour charges the full rate; the dearest hour discharges it.
        assert actions[3] is BatteryAction.CHARGE
        assert plan[3].battery_kwh == pytest.approx(50.0, abs=TOLERANCE)
        assert actions[20] is BatteryAction.DISCHARGE
        assert plan[20].battery_kwh == pytest.approx(50.0, abs=TOLERANCE)

        # 25200 grid-only cost minus (30 - 2) BDT/kWh * 50 kWh shifted.
        assert _cost(plan, hours) == pytest.approx(23800.0, abs=TOLERANCE)

    def test_no_battery_scenario_costs_grid_only(self) -> None:
        hours = _hours(tariff=_arbitrage_tariffs())
        disabled = _battery(
            max_charge_kwh_per_hour=0.0, max_discharge_kwh_per_hour=0.0
        )
        plan = _solve(hours, disabled)
        assert all(
            entry.battery_action is BatteryAction.IDLE for entry in plan
        )
        assert _cost(plan, hours) == pytest.approx(25200.0, abs=TOLERANCE)

    def test_solar_reduces_grid_usage(self) -> None:
        hours = _hours(solar=40.0)
        plan = _solve(hours, _battery())
        # All available solar is free, so it is always fully used.
        for entry in plan:
            assert entry.solar_used_kwh == pytest.approx(40.0, abs=TOLERANCE)
        # The battery is net-neutral, so total grid = total demand - total solar.
        assert sum(entry.grid_kwh for entry in plan) == pytest.approx(
            1440.0, abs=TOLERANCE
        )
        assert _cost(plan, hours) == pytest.approx(14400.0, abs=TOLERANCE)


class TestSolverFailure:
    def test_infeasible_reserve_at_final_hour_raises_solver_error(self) -> None:
        # Reserve of 150 kWh must hold at hour 23, but end-of-day neutrality
        # forces energy[24] back to the 100 kWh initial level.
        directives = [
            _directive(
                0,
                DirectiveType.MINIMUM_BATTERY_RESERVE,
                MinimumBatteryReserveAdjustment(
                    hours=[23], minimum_energy_kwh=150.0
                ),
            )
        ]
        with pytest.raises(SolverError) as excinfo:
            _solve(_hours(), _battery(), directives)

        assert excinfo.value.status == "Infeasible"
        assert "optimal" in str(excinfo.value)

    def test_same_reserve_one_hour_earlier_is_feasible(self) -> None:
        directives = [
            _directive(
                0,
                DirectiveType.MINIMUM_BATTERY_RESERVE,
                MinimumBatteryReserveAdjustment(
                    hours=[22], minimum_energy_kwh=150.0
                ),
            )
        ]
        plan = _solve(_hours(), _battery(), directives)
        assert plan[22].battery_energy_after_kwh >= 150.0 - TOLERANCE
        assert plan[-1].battery_energy_after_kwh == pytest.approx(
            100.0, abs=TOLERANCE
        )

    def test_solver_error_is_not_an_assertion_error(self) -> None:
        assert not issubclass(SolverError, AssertionError)

    def test_solver_error_is_raised_not_returned_as_empty_plan(self) -> None:
        directives = [
            _directive(
                0,
                DirectiveType.MINIMUM_BATTERY_RESERVE,
                MinimumBatteryReserveAdjustment(
                    hours=[23], minimum_energy_kwh=150.0
                ),
            )
        ]
        with pytest.raises(SolverError):
            _solve(_hours(), _battery(), directives)