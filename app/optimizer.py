"""Deterministic 24-hour optimization using PuLP and the CBC solver.

Model (Problem Statement 9, SPEC_AUDIT.md 2.7-2.10; prompt Task-2 section C):

- 25 battery-state variables: ``energy[0] == initial_energy_kwh``,
  ``energy[h+1] == energy[h] + charge[h] - discharge[h]``, and
  ``energy[24] == initial_energy_kwh`` (end-of-day neutrality).
- Per-hour variables: ``grid[h] >= 0``, ``solar_used[h] >= 0``,
  ``charge[h] >= 0``, ``discharge[h] >= 0``, binary ``is_charging[h]`` and
  ``is_discharging[h]``.
- Energy balance: ``grid + solar_used + discharge == demand + charge``.
- Solar ceiling, rate limits, one-direction-at-a-time, active minimum reserve
  and capacity bounds, no-charge / no-discharge / max-grid directive windows.
- Objective: minimize ``sum(grid[h] * tariff[h])``.

No battery efficiency or energy-loss term exists anywhere in the official
rules, so none is modelled here. Numerical precision is preserved internally;
the solver output is read only after the status is verified to be Optimal.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

import pulp

from app.directive_applier import HOURS_PER_DAY, OptimizationInputs
from app.schemas import (
    BatteryAction,
    BatteryRequest,
    HourlyPlanEntry,
    HourRequest,
)

__all__ = ["SOLVER_NAME", "DEFAULT_EPSILON", "SolverError", "optimize_hourly_plan"]

#: Solver backend requested by the specification (CBC via PuLP, quiet mode).
SOLVER_NAME = "PULP_CBC_CMD"

#: Action-classification epsilon. Deliberately far stricter than the official
#: 0.01 kWh / 0.01 BDT judging tolerance so real actions are never discarded.
DEFAULT_EPSILON = 1e-6


class SolverError(Exception):
    """Raised when the optimizer cannot produce an optimal solution.

    ``status`` carries the PuLP status name (for example ``"Infeasible"``) so
    callers can log a controlled, secret-free failure reason. A ``SolverError``
    is never converted into ``no_op`` or into an empty/default plan.
    """

    def __init__(self, message: str, *, status: str) -> None:
        self.status = status
        super().__init__(message)


def _values_by_hour(
    hours: Sequence[HourRequest], attribute: str
) -> list[float]:
    """Index an hourly request field by ``hour`` (request order is irrelevant)."""
    values = [0.0] * HOURS_PER_DAY
    for entry in hours:
        values[entry.hour] = float(getattr(entry, attribute))
    return values


def _solution_value(variable: pulp.LpVariable, label: str) -> float:
    """Read one variable value from an already-optimal solution."""
    value = variable.value()
    if value is None:
        raise SolverError(
            f"solver returned no value for {label}", status="Undefined"
        )
    number = float(value)
    if not math.isfinite(number):
        raise SolverError(
            f"solver returned a non-finite value for {label}", status="Undefined"
        )
    return number


def _snap_non_negative(value: float, label: str, epsilon: float) -> float:
    """Snap solver noise inside the epsilon band to zero.

    Variables that are bounded below by zero can come back as tiny negatives
    such as ``-1e-12`` purely from solver tolerance. Anything beyond the
    epsilon band is a genuine violation and raises a controlled error instead
    of being silently clamped.
    """
    if value < 0.0:
        if value < -epsilon:
            raise SolverError(
                f"solver returned a negative value for {label}: {value!r}",
                status="InvalidSolution",
            )
        return 0.0
    return value


def optimize_hourly_plan(
    hours: Sequence[HourRequest],
    battery: BatteryRequest,
    inputs: OptimizationInputs,
    *,
    epsilon: float = DEFAULT_EPSILON,
) -> list[HourlyPlanEntry]:
    """Solve the 24-hour dispatch problem and return 24 hourly-plan entries.

    :param hours: the 24 request hour entries (any order; indexed by ``hour``).
    :param battery: battery parameters (capacity, initial energy, rate limits).
    :param inputs: compiled directive effects from
        :func:`app.directive_applier.apply_directives`.
    :param epsilon: action-classification tolerance (defaults to ``1e-6``).
    :raises SolverError: if the solver does not report an optimal solution or
        returns an unusable value.
    """
    demand = _values_by_hour(hours, "demand_kwh")
    tariff = _values_by_hour(hours, "tariff_bdt_per_kwh")
    capacity = float(battery.capacity_kwh)
    initial_energy = float(battery.initial_energy_kwh)
    max_charge = float(battery.max_charge_kwh_per_hour)
    max_discharge = float(battery.max_discharge_kwh_per_hour)

    problem = pulp.LpProblem("gridwise_24h_dispatch", pulp.LpMinimize)

    grid = [
        pulp.LpVariable(f"grid_{h}", lowBound=0) for h in range(HOURS_PER_DAY)
    ]
    solar_used = [
        pulp.LpVariable(f"solar_used_{h}", lowBound=0)
        for h in range(HOURS_PER_DAY)
    ]
    charge = [
        pulp.LpVariable(f"charge_{h}", lowBound=0) for h in range(HOURS_PER_DAY)
    ]
    discharge = [
        pulp.LpVariable(f"discharge_{h}", lowBound=0)
        for h in range(HOURS_PER_DAY)
    ]
    # 25 battery-state variables: energy[h] is the state at the start of hour h,
    # so energy[h + 1] is the reported battery_energy_after_kwh for hour h.
    energy = [
        pulp.LpVariable(f"energy_{h}", lowBound=0)
        for h in range(HOURS_PER_DAY + 1)
    ]
    is_charging = [
        pulp.LpVariable(f"is_charging_{h}", cat=pulp.LpBinary)
        for h in range(HOURS_PER_DAY)
    ]
    is_discharging = [
        pulp.LpVariable(f"is_discharging_{h}", cat=pulp.LpBinary)
        for h in range(HOURS_PER_DAY)
    ]

    problem += (energy[0] == initial_energy, "initial_energy")

    for hour in range(HOURS_PER_DAY):
        problem += (
            grid[hour] + solar_used[hour] + discharge[hour]
            == demand[hour] + charge[hour],
            f"energy_balance_{hour}",
        )
        problem += (
            solar_used[hour] <= inputs.effective_solar[hour],
            f"solar_ceiling_{hour}",
        )
        problem += (
            charge[hour] <= max_charge * is_charging[hour],
            f"charge_rate_limit_{hour}",
        )
        problem += (
            discharge[hour] <= max_discharge * is_discharging[hour],
            f"discharge_rate_limit_{hour}",
        )
        problem += (
            is_charging[hour] + is_discharging[hour] <= 1,
            f"single_direction_{hour}",
        )
        problem += (
            energy[hour + 1] == energy[hour] + charge[hour] - discharge[hour],
            f"state_transition_{hour}",
        )
        problem += (
            energy[hour + 1] >= inputs.active_minimum_energy[hour],
            f"active_minimum_{hour}",
        )
        problem += (energy[hour + 1] <= capacity, f"capacity_{hour}")

        if hour in inputs.no_charge_hours:
            problem += (charge[hour] == 0, f"no_charge_{hour}")
        if hour in inputs.no_discharge_hours:
            problem += (discharge[hour] == 0, f"no_discharge_{hour}")
        grid_cap = inputs.max_grid_by_hour.get(hour)
        if grid_cap is not None:
            problem += (grid[hour] <= grid_cap, f"max_grid_{hour}")

    problem += (
        energy[HOURS_PER_DAY] == initial_energy,
        "end_of_day_neutrality",
    )
    problem += (
        pulp.lpSum(
            grid[hour] * tariff[hour] for hour in range(HOURS_PER_DAY)
        ),
        "total_cost_bdt",
    )

    # Solver status is verified before any variable value is read.
    try:
        status_code = problem.solve(pulp.PULP_CBC_CMD(msg=False))
    except pulp.PulpSolverError as exc:
        raise SolverError(
            f"{SOLVER_NAME} could not solve the model: {exc}",
            status="SolverUnavailable",
        ) from exc

    status = pulp.LpStatus.get(status_code, str(status_code))
    if status_code != pulp.LpStatusOptimal:
        raise SolverError(
            f"optimizer did not reach optimal status (status={status})",
            status=status,
        )

    return _build_hourly_plan(
        grid, solar_used, charge, discharge, energy, epsilon=epsilon
    )


def _build_hourly_plan(
    grid: Sequence[pulp.LpVariable],
    solar_used: Sequence[pulp.LpVariable],
    charge: Sequence[pulp.LpVariable],
    discharge: Sequence[pulp.LpVariable],
    energy: Sequence[pulp.LpVariable],
    *,
    epsilon: float,
) -> list[HourlyPlanEntry]:
    """Convert an optimal solution into exactly 24 hourly-plan entries."""
    plan: list[HourlyPlanEntry] = []

    for hour in range(HOURS_PER_DAY):
        charge_kwh = _snap_non_negative(
            _solution_value(charge[hour], f"charge[{hour}]"),
            f"charge[{hour}]",
            epsilon,
        )
        discharge_kwh = _snap_non_negative(
            _solution_value(discharge[hour], f"discharge[{hour}]"),
            f"discharge[{hour}]",
            epsilon,
        )
        grid_kwh = _snap_non_negative(
            _solution_value(grid[hour], f"grid[{hour}]"),
            f"grid[{hour}]",
            epsilon,
        )
        solar_kwh = _snap_non_negative(
            _solution_value(solar_used[hour], f"solar_used[{hour}]"),
            f"solar_used[{hour}]",
            epsilon,
        )
        energy_after = _snap_non_negative(
            _solution_value(energy[hour + 1], f"energy[{hour + 1}]"),
            f"energy[{hour + 1}]",
            epsilon,
        )

        charging = charge_kwh > epsilon
        discharging = discharge_kwh > epsilon
        if charging and discharging:
            raise SolverError(
                f"hour {hour} has simultaneous charge and discharge "
                f"({charge_kwh!r} / {discharge_kwh!r})",
                status="InvalidSolution",
            )

        if charging:
            action = BatteryAction.CHARGE
            battery_kwh = charge_kwh
        elif discharging:
            action = BatteryAction.DISCHARGE
            battery_kwh = discharge_kwh
        else:
            # Neither direction is positive: idle requires battery_kwh == 0.
            action = BatteryAction.IDLE
            battery_kwh = 0.0

        plan.append(
            HourlyPlanEntry(
                hour=hour,
                grid_kwh=grid_kwh,
                solar_used_kwh=solar_kwh,
                battery_action=action,
                battery_kwh=battery_kwh,
                battery_energy_after_kwh=energy_after,
            )
        )

    return plan