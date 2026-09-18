"""Focused deterministic tests for the directive applier.

Covers each official directive effect, overlap resolution (highest reserve,
tightest grid cap), input immutability, and hour indexing.
"""

from __future__ import annotations

import copy

from app.directive_applier import HOURS_PER_DAY, apply_directives
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
        for hour in range(HOURS_PER_DAY)
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


class TestNoOp:
    def test_no_op_changes_nothing(self) -> None:
        inputs = apply_directives(
            [_directive(0, DirectiveType.NO_OP, None, applies=False)],
            _hours(solar=30.0),
            _battery(),
        )
        assert inputs.effective_solar == [30.0] * HOURS_PER_DAY
        assert inputs.active_minimum_energy == [40.0] * HOURS_PER_DAY
        assert inputs.no_charge_hours == set()
        assert inputs.no_discharge_hours == set()
        assert inputs.max_grid_by_hour == {}


class TestOfficialEffects:
    def test_solar_reduction_multiplies_only_listed_hours(self) -> None:
        inputs = apply_directives(
            [
                _directive(
                    0,
                    DirectiveType.SOLAR_REDUCTION,
                    SolarReductionAdjustment(hours=[13, 14], factor=0.25),
                )
            ],
            _hours(solar=100.0),
            _battery(),
        )
        assert inputs.effective_solar[12] == 100.0
        assert inputs.effective_solar[13] == 25.0
        assert inputs.effective_solar[14] == 25.0
        assert inputs.effective_solar[15] == 100.0

    def test_solar_reduction_zero_factor_curtails_hour(self) -> None:
        inputs = apply_directives(
            [
                _directive(
                    0,
                    DirectiveType.SOLAR_REDUCTION,
                    SolarReductionAdjustment(hours=[9], factor=0.0),
                )
            ],
            _hours(solar=80.0),
            _battery(),
        )
        assert inputs.effective_solar[9] == 0.0

    def test_minimum_battery_reserve_raises_floor(self) -> None:
        inputs = apply_directives(
            [
                _directive(
                    0,
                    DirectiveType.MINIMUM_BATTERY_RESERVE,
                    MinimumBatteryReserveAdjustment(
                        hours=[18, 19, 20], minimum_energy_kwh=120.0
                    ),
                )
            ],
            _hours(),
            _battery(minimum_energy_kwh=40.0),
        )
        assert inputs.active_minimum_energy[17] == 40.0
        assert inputs.active_minimum_energy[18] == 120.0
        assert inputs.active_minimum_energy[19] == 120.0
        assert inputs.active_minimum_energy[20] == 120.0
        assert inputs.active_minimum_energy[21] == 40.0

    def test_no_charge_window_adds_hours(self) -> None:
        inputs = apply_directives(
            [
                _directive(
                    0,
                    DirectiveType.NO_CHARGE_WINDOW,
                    NoChargeWindowAdjustment(hours=[2, 3, 4]),
                )
            ],
            _hours(),
            _battery(),
        )
        assert inputs.no_charge_hours == {2, 3, 4}
        assert inputs.no_discharge_hours == set()

    def test_no_discharge_window_adds_hours(self) -> None:
        inputs = apply_directives(
            [
                _directive(
                    0,
                    DirectiveType.NO_DISCHARGE_WINDOW,
                    NoDischargeWindowAdjustment(hours=[18, 19]),
                )
            ],
            _hours(),
            _battery(),
        )
        assert inputs.no_discharge_hours == {18, 19}
        assert inputs.no_charge_hours == set()

    def test_max_grid_window_sets_cap(self) -> None:
        inputs = apply_directives(
            [
                _directive(
                    0,
                    DirectiveType.MAX_GRID_WINDOW,
                    MaxGridWindowAdjustment(hours=[19, 20], max_grid_kwh=150.0),
                )
            ],
            _hours(),
            _battery(),
        )
        assert inputs.max_grid_by_hour == {19: 150.0, 20: 150.0}


class TestOverlaps:
    def test_overlapping_reserve_uses_highest(self) -> None:
        inputs = apply_directives(
            [
                _directive(
                    0,
                    DirectiveType.MINIMUM_BATTERY_RESERVE,
                    MinimumBatteryReserveAdjustment(
                        hours=[18, 19, 20], minimum_energy_kwh=100.0
                    ),
                ),
                _directive(
                    1,
                    DirectiveType.MINIMUM_BATTERY_RESERVE,
                    MinimumBatteryReserveAdjustment(
                        hours=[19, 20, 21], minimum_energy_kwh=150.0
                    ),
                ),
            ],
            _hours(),
            _battery(minimum_energy_kwh=40.0),
        )
        assert inputs.active_minimum_energy[18] == 100.0
        assert inputs.active_minimum_energy[19] == 150.0
        assert inputs.active_minimum_energy[20] == 150.0
        assert inputs.active_minimum_energy[21] == 150.0
        assert inputs.active_minimum_energy[17] == 40.0

    def test_overlapping_grid_cap_uses_lowest(self) -> None:
        inputs = apply_directives(
            [
                _directive(
                    0,
                    DirectiveType.MAX_GRID_WINDOW,
                    MaxGridWindowAdjustment(hours=[19, 20], max_grid_kwh=180.0),
                ),
                _directive(
                    1,
                    DirectiveType.MAX_GRID_WINDOW,
                    MaxGridWindowAdjustment(hours=[20, 21], max_grid_kwh=120.0),
                ),
            ],
            _hours(),
            _battery(),
        )
        assert inputs.max_grid_by_hour[19] == 180.0
        assert inputs.max_grid_by_hour[20] == 120.0
        assert inputs.max_grid_by_hour[21] == 120.0


class TestBaseInputsAndCombination:
    def test_base_minimum_seeds_every_hour(self) -> None:
        inputs = apply_directives([], _hours(), _battery(minimum_energy_kwh=25.0))
        assert inputs.active_minimum_energy == [25.0] * HOURS_PER_DAY

    def test_effective_solar_starts_from_request_solar(self) -> None:
        solar = [float(hour) for hour in range(HOURS_PER_DAY)]
        inputs = apply_directives([], _hours(solar=solar), _battery())
        assert inputs.effective_solar == solar

    def test_all_five_directives_combine(self) -> None:
        directives = [
            _directive(
                0,
                DirectiveType.SOLAR_REDUCTION,
                SolarReductionAdjustment(hours=[12, 13], factor=0.5),
            ),
            _directive(
                1,
                DirectiveType.MINIMUM_BATTERY_RESERVE,
                MinimumBatteryReserveAdjustment(
                    hours=[18], minimum_energy_kwh=90.0
                ),
            ),
            _directive(
                2,
                DirectiveType.NO_CHARGE_WINDOW,
                NoChargeWindowAdjustment(hours=[2, 3]),
            ),
            _directive(
                3,
                DirectiveType.NO_DISCHARGE_WINDOW,
                NoDischargeWindowAdjustment(hours=[18]),
            ),
            _directive(
                4,
                DirectiveType.MAX_GRID_WINDOW,
                MaxGridWindowAdjustment(hours=[19], max_grid_kwh=140.0),
            ),
        ]
        inputs = apply_directives(directives, _hours(solar=100.0), _battery())
        assert inputs.effective_solar[12] == 50.0
        assert inputs.active_minimum_energy[18] == 90.0
        assert inputs.no_charge_hours == {2, 3}
        assert inputs.no_discharge_hours == {18}
        assert inputs.max_grid_by_hour == {19: 140.0}

    def test_hours_are_indexed_by_hour_field_not_list_order(self) -> None:
        hours = _hours(solar=[float(hour) for hour in range(HOURS_PER_DAY)])
        inputs = apply_directives(
            [
                _directive(
                    0,
                    DirectiveType.SOLAR_REDUCTION,
                    SolarReductionAdjustment(hours=[5], factor=0.5),
                ),
                _directive(
                    1,
                    DirectiveType.NO_CHARGE_WINDOW,
                    NoChargeWindowAdjustment(hours=[7]),
                ),
            ],
            list(reversed(hours)),
            _battery(),
        )
        assert inputs.effective_solar[5] == 2.5
        assert inputs.effective_solar[4] == 4.0
        assert inputs.no_charge_hours == {7}


class TestInputsAreNotMutated:
    def test_requests_and_directives_are_unchanged(self) -> None:
        hours = _hours(solar=100.0)
        battery = _battery(minimum_energy_kwh=40.0)
        directives = [
            _directive(
                0,
                DirectiveType.SOLAR_REDUCTION,
                SolarReductionAdjustment(hours=[13, 14], factor=0.25),
            ),
            _directive(
                1,
                DirectiveType.MINIMUM_BATTERY_RESERVE,
                MinimumBatteryReserveAdjustment(
                    hours=[18], minimum_energy_kwh=120.0
                ),
            ),
            _directive(
                2,
                DirectiveType.NO_CHARGE_WINDOW,
                NoChargeWindowAdjustment(hours=[2, 3, 4]),
            ),
            _directive(
                3,
                DirectiveType.MAX_GRID_WINDOW,
                MaxGridWindowAdjustment(hours=[19, 20], max_grid_kwh=150.0),
            ),
        ]
        hours_snapshot = copy.deepcopy(hours)
        battery_snapshot = copy.deepcopy(battery)
        directives_snapshot = copy.deepcopy(directives)

        inputs = apply_directives(directives, hours, battery)

        assert hours == hours_snapshot
        assert battery == battery_snapshot
        assert directives == directives_snapshot
        # Compiled inputs are independent copies of the request values.
        inputs.effective_solar[13] = 999.0
        assert hours[13].solar_kwh == 100.0
        assert directives[0].structured_adjustment.factor == 0.25