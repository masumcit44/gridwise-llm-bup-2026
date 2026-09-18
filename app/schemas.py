"""Exact API contract and schema models for the GridWise preliminary.

Field names, types, and shapes are canonical from the Problem Statement
(Preliminary_Problem_Statement_GridWise_LLM.pdf, sections 7 and 10). The
Implementation Invariants in SPEC_AUDIT.md are treated as binding: no invented
fields, no silent repair of invalid input, no clamping of invalid numbers.

This module only defines and validates the contract. The LLM interpretation,
deterministic guardrails, optimizer, and fallback logic are implemented in
later tasks.
"""

from __future__ import annotations

from enum import Enum
from typing import Annotated, Optional, Union

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

# --------------------------------------------------------------------------- #
# Health endpoint
# --------------------------------------------------------------------------- #


class HealthResponse(BaseModel):
    """Exact GET /health readiness payload (Problem Statement section 6.2)."""

    model_config = ConfigDict(extra="forbid")

    status: str


# --------------------------------------------------------------------------- #
# Shared enums
# --------------------------------------------------------------------------- #


class DirectiveType(str, Enum):
    """The six supported operator-note directive types (Problem Statement 4.1)."""

    SOLAR_REDUCTION = "solar_reduction"
    MINIMUM_BATTERY_RESERVE = "minimum_battery_reserve"
    NO_CHARGE_WINDOW = "no_charge_window"
    NO_DISCHARGE_WINDOW = "no_discharge_window"
    MAX_GRID_WINDOW = "max_grid_window"
    NO_OP = "no_op"


class BatteryAction(str, Enum):
    """Allowed hourly battery actions (Problem Statement section 10.3)."""

    CHARGE = "charge"
    DISCHARGE = "discharge"
    IDLE = "idle"


# --------------------------------------------------------------------------- #
# Request schema (Problem Statement section 7)
# --------------------------------------------------------------------------- #


def _validate_hours(values: list[int]) -> list[int]:
    """Shared validation for every directive adjustment `hours` array.

    Official rules: unique integers from 0 through 23, in ascending order,
    non-empty (a directive that affects no hours is meaningless).
    """
    if not values:
        raise ValueError("hours must contain at least one valid hour")
    if any(not isinstance(h, int) or h < 0 or h > 23 for h in values):
        raise ValueError("hours must be unique integers from 0 through 23")
    if len(set(values)) != len(values):
        raise ValueError("hours must be unique")
    if list(values) != sorted(values):
        raise ValueError("hours must be in ascending order")
    return values


class HourRequest(BaseModel):
    """One hourly entry in the request (Problem Statement section 7.2)."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    hour: int = Field(ge=0, le=23)
    demand_kwh: Annotated[float, Field(ge=0)]
    solar_kwh: Annotated[float, Field(ge=0)]
    tariff_bdt_per_kwh: Annotated[float, Field(ge=0)]


class BatteryRequest(BaseModel):
    """Battery parameters (Problem Statement section 7.3).

    The Problem Statement defines exactly five fields. There is no efficiency
    field anywhere in the official contract; extra fields are rejected.
    """

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    capacity_kwh: Annotated[float, Field(ge=0)]
    initial_energy_kwh: Annotated[float, Field(ge=0)]
    minimum_energy_kwh: Annotated[float, Field(ge=0)]
    max_charge_kwh_per_hour: Annotated[float, Field(ge=0)]
    max_discharge_kwh_per_hour: Annotated[float, Field(ge=0)]

    @model_validator(mode="after")
    def validate_bounds_against_capacity(self) -> "BatteryRequest":
        """minimum and initial energy must not exceed battery capacity."""
        if self.minimum_energy_kwh > self.capacity_kwh:
            raise ValueError(
                "minimum_energy_kwh cannot exceed capacity_kwh"
            )
        if self.initial_energy_kwh > self.capacity_kwh:
            raise ValueError("initial_energy_kwh cannot exceed capacity_kwh")
        return self


class OptimizeEnergyRequest(BaseModel):
    """POST /optimize-energy request body (Problem Statement section 7.1)."""

    model_config = ConfigDict(extra="forbid")

    scenario_id: str = Field(min_length=1)
    operator_notes: list[str] = Field(min_length=1, max_length=3)
    hours: list[HourRequest]
    battery: BatteryRequest

    @field_validator("operator_notes")
    @classmethod
    def validate_operator_notes(cls, values: list[str]) -> list[str]:
        """Require between 1 and 3 notes, each non-empty after stripping."""
        stripped = [v.strip() for v in values]
        if any(not note for note in stripped):
            raise ValueError("each operator note must be non-empty")
        return stripped

    @field_validator("hours")
    @classmethod
    def validate_hours(
        cls, values: list[HourRequest]
    ) -> list[HourRequest]:
        """Require exactly 24 unique hourly entries for hours 0 through 23."""
        if len(values) != 24:
            raise ValueError("hours must contain exactly 24 entries")
        seen: set[int] = set()
        for entry in values:
            if entry.hour in seen:
                raise ValueError(f"duplicate hour entry: {entry.hour}")
            seen.add(entry.hour)
        if seen != set(range(24)):
            missing = sorted(set(range(24)) - seen)
            raise ValueError(f"hours must cover all of 0..23; missing {missing}")
        return values


# --------------------------------------------------------------------------- #
# Response schema — directive interpretation (Problem Statement section 10.2)
# --------------------------------------------------------------------------- #


class SolarReductionAdjustment(BaseModel):
    """{"hours": [...], "factor": number} — usable fraction remaining."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    hours: list[int]
    factor: Annotated[float, Field(ge=0, le=1)]

    @field_validator("hours")
    @classmethod
    def validate_hours(cls, values: list[int]) -> list[int]:
        return _validate_hours(values)


class MinimumBatteryReserveAdjustment(BaseModel):
    """{"hours": [...], "minimum_energy_kwh": number}."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    hours: list[int]
    minimum_energy_kwh: Annotated[float, Field(ge=0)]

    @field_validator("hours")
    @classmethod
    def validate_hours(cls, values: list[int]) -> list[int]:
        return _validate_hours(values)


class NoChargeWindowAdjustment(BaseModel):
    """{"hours": [...]}."""

    model_config = ConfigDict(extra="forbid")

    hours: list[int]

    @field_validator("hours")
    @classmethod
    def validate_hours(cls, values: list[int]) -> list[int]:
        return _validate_hours(values)


class NoDischargeWindowAdjustment(BaseModel):
    """{"hours": [...]}."""

    model_config = ConfigDict(extra="forbid")

    hours: list[int]

    @field_validator("hours")
    @classmethod
    def validate_hours(cls, values: list[int]) -> list[int]:
        return _validate_hours(values)


class MaxGridWindowAdjustment(BaseModel):
    """{"hours": [...], "max_grid_kwh": number}."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    hours: list[int]
    max_grid_kwh: Annotated[float, Field(ge=0)]

    @field_validator("hours")
    @classmethod
    def validate_hours(cls, values: list[int]) -> list[int]:
        return _validate_hours(values)


StructuredAdjustment = Union[
    SolarReductionAdjustment,
    MinimumBatteryReserveAdjustment,
    NoChargeWindowAdjustment,
    NoDischargeWindowAdjustment,
    MaxGridWindowAdjustment,
]


class DirectiveInterpretation(BaseModel):
    """One machine-checkable interpretation entry (Problem Statement 10.2)."""

    model_config = ConfigDict(extra="forbid")

    note_index: int = Field(ge=0)
    applies: bool
    directive_type: DirectiveType
    structured_adjustment: Optional[StructuredAdjustment] = None
    explanation: str = Field(min_length=1)


# --------------------------------------------------------------------------- #
# Response schema — hourly plan and top level (Problem Statement section 10)
# --------------------------------------------------------------------------- #


class HourlyPlanEntry(BaseModel):
    """One hourly plan entry (Problem Statement section 10.3)."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    hour: int = Field(ge=0, le=23)
    grid_kwh: Annotated[float, Field(ge=0)]
    solar_used_kwh: Annotated[float, Field(ge=0)]
    battery_action: BatteryAction
    battery_kwh: Annotated[float, Field(ge=0)]
    battery_energy_after_kwh: Annotated[float, Field(ge=0)]


class OptimizeEnergyResponse(BaseModel):
    """Successful POST /optimize-energy response (Problem Statement 10.1)."""

    model_config = ConfigDict(extra="forbid")

    scenario_id: str = Field(min_length=1)
    directive_interpretation: list[DirectiveInterpretation]
    hourly_plan: list[HourlyPlanEntry]
    total_grid_kwh: Annotated[float, Field(ge=0)]
    total_cost_bdt: Annotated[float, Field(ge=0)]
    peak_grid_kwh: Annotated[float, Field(ge=0)]
    plan_summary: str = Field(min_length=1)