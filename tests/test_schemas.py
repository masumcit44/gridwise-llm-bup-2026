"""Tests for the exact API contract, schema validation, and endpoint scaffolding.

Covers:
- Structurally valid request parsing
- Missing fields / missing top-level keys
- operator_notes count (0, 4) and empty-note rejection
- Duplicate, missing, and out-of-range hour entries
- Negative and non-finite numeric values
- Invalid battery bounds (minimum/initial > capacity)
- Rejection of invented extra fields (battery_efficiency etc.)
- Directive type and adjustment shape construction
- Response model construction
- POST /optimize-energy: valid request returns the official 200 response; invalid requests return 400/422; provider/interpretation/solver failures return a controlled 500.
"""

from __future__ import annotations

import math

import pytest
import json as _json
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.main import app
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
    OptimizeEnergyRequest,
    OptimizeEnergyResponse,
    SolarReductionAdjustment,
)

client = TestClient(app)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _valid_hours() -> list[dict]:
    return [{"hour": h, "demand_kwh": 100, "solar_kwh": 50, "tariff_bdt_per_kwh": 10} for h in range(24)]


def _valid_battery() -> dict:
    return {
        "capacity_kwh": 200,
        "initial_energy_kwh": 100,
        "minimum_energy_kwh": 40,
        "max_charge_kwh_per_hour": 50,
        "max_discharge_kwh_per_hour": 50,
    }


def _valid_payload(**overrides) -> dict:
    base = {
        "scenario_id": "TEST-01",
        "operator_notes": ["Cloud cover will reduce output tomorrow."],
        "hours": _valid_hours(),
        "battery": _valid_battery(),
    }
    base.update(overrides)
    return base


def _post_raw(payload: dict) -> object:
    """POST a payload that may contain NaN/Infinity JSON literals.

    The TestClient `json=` argument serializes with standard JSON rules and
    rejects non-finite floats before the request is sent, so those values are
    injected as raw JSON text instead (json.dumps allow_nan=True).
    """
    return client.post(
        "/optimize-energy",
        content=_json.dumps(payload, allow_nan=True),
        headers={"Content-Type": "application/json"},
    )


# --------------------------------------------------------------------------- #
# Valid request parsing
# --------------------------------------------------------------------------- #


class TestValidRequestParsing:
    def test_full_valid_payload(self) -> None:
        req = OptimizeEnergyRequest(**_valid_payload())
        assert req.scenario_id == "TEST-01"
        assert len(req.hours) == 24
        assert len(req.operator_notes) == 1

    def test_valid_with_three_notes(self) -> None:
        req = OptimizeEnergyRequest(**_valid_payload(
            operator_notes=["Note one", "Note two", "Note three"],
        ))
        assert len(req.operator_notes) == 3

    def test_notes_are_stripped(self) -> None:
        req = OptimizeEnergyRequest(**_valid_payload(
            operator_notes=["  Some note  "],
        ))
        assert req.operator_notes[0] == "Some note"


# --------------------------------------------------------------------------- #
# POST /optimize-energy returns 200 (official success) for valid requests
# --------------------------------------------------------------------------- #


class TestPostOptimizeEnergySuccess:
    def test_valid_request_succeeds(self) -> None:
        import app.main as m
        import json as _json

        _no_op = _json.dumps(
            {
                "directive_interpretation": [
                    {
                        "note_index": 0,
                        "applies": False,
                        "directive_type": "no_op",
                        "structured_adjustment": None,
                        "explanation": "irrelevant note",
                    }
                ]
            }
        )
        original = getattr(m.interpreter, "_call_groq", None)
        m.interpreter._call_groq = lambda s, u: _no_op  # type: ignore[method-assign]
        try:
            resp = client.post("/optimize-energy", json=_valid_payload())
        finally:
            if original is None:
                del m.interpreter._call_groq
            else:
                m.interpreter._call_groq = original  # type: ignore[method-assign]
        assert resp.status_code == 200
        body = resp.json()
        assert body["scenario_id"] == "TEST-01"
        assert set(body) == {
            "scenario_id",
            "directive_interpretation",
            "hourly_plan",
            "total_grid_kwh",
            "total_cost_bdt",
            "peak_grid_kwh",
            "plan_summary",
        }





# --------------------------------------------------------------------------- #
# Missing fields
# --------------------------------------------------------------------------- #


class TestMissingFields:
    @pytest.mark.parametrize("missing_key", [
        "scenario_id", "operator_notes", "hours", "battery",
    ])
    def test_missing_top_level_field(self, missing_key: str) -> None:
        payload = _valid_payload()
        payload.pop(missing_key)
        resp = client.post("/optimize-energy", json=payload)
        assert resp.status_code == 422

    def test_missing_hourly_entry_field(self) -> None:
        hours = _valid_hours()
        del hours[0]["demand_kwh"]
        payload = _valid_payload(hours=hours)
        resp = client.post("/optimize-energy", json=payload)
        assert resp.status_code == 422

    def test_missing_battery_field(self) -> None:
        bat = _valid_battery()
        del bat["capacity_kwh"]
        payload = _valid_payload(battery=bat)
        resp = client.post("/optimize-energy", json=payload)
        assert resp.status_code == 422

    def test_missing_hour_entry(self) -> None:
        hours = _valid_hours()
        hours.pop(0)
        payload = _valid_payload(hours=hours)
        resp = client.post("/optimize-energy", json=payload)
        assert resp.status_code == 422

    def test_empty_body(self) -> None:
        resp = client.post("/optimize-energy", json={})
        assert resp.status_code == 422

    def test_missing_scenario_id(self) -> None:
        payload = _valid_payload()
        del payload["scenario_id"]
        resp = client.post("/optimize-energy", json=payload)
        assert resp.status_code == 422


# --------------------------------------------------------------------------- #
# operator_notes count and empty-note rejection
# --------------------------------------------------------------------------- #


class TestOperatorNotesValidation:
    @pytest.mark.parametrize("count", [0, 2, 4, 5, 10])
    def test_wrong_note_count(self, count: int) -> None:
        notes = ["n"] * count if count > 0 else []
        if 1 <= count <= 3:
            pytest.skip("valid count")
        payload = _valid_payload(operator_notes=notes)
        resp = client.post("/optimize-energy", json=payload)
        assert resp.status_code == 422

    def test_zero_notes_rejected(self) -> None:
        payload = _valid_payload(operator_notes=[])
        resp = client.post("/optimize-energy", json=payload)
        assert resp.status_code == 422

    def test_four_notes_rejected(self) -> None:
        payload = _valid_payload(operator_notes=["a", "b", "c", "d"])
        resp = client.post("/optimize-energy", json=payload)
        assert resp.status_code == 422

    def test_empty_note_rejected(self) -> None:
        payload = _valid_payload(operator_notes=[""])
        resp = client.post("/optimize-energy", json=payload)
        assert resp.status_code == 422

    def test_whitespace_only_note_rejected(self) -> None:
        payload = _valid_payload(operator_notes=["   "])
        resp = client.post("/optimize-energy", json=payload)
        assert resp.status_code == 422

    def test_empty_note_in_valid_list_rejected(self) -> None:
        payload = _valid_payload(operator_notes=["valid", "", "also valid"])
        resp = client.post("/optimize-energy", json=payload)
        assert resp.status_code == 422


# --------------------------------------------------------------------------- #
# Duplicate, missing, and out-of-range hours
# --------------------------------------------------------------------------- #


class TestHoursValidation:
    def test_duplicate_hour_rejected(self) -> None:
        hours = _valid_hours()
        hours[0]["hour"] = 1  # duplicate hour=1
        payload = _valid_payload(hours=hours)
        resp = client.post("/optimize-energy", json=payload)
        assert resp.status_code == 422

    def test_hour_out_of_range_high(self) -> None:
        hours = _valid_hours()
        hours[0]["hour"] = 24
        payload = _valid_payload(hours=hours)
        resp = client.post("/optimize-energy", json=payload)
        assert resp.status_code == 422

    def test_hour_out_of_range_negative(self) -> None:
        hours = _valid_hours()
        hours[0]["hour"] = -1
        payload = _valid_payload(hours=hours)
        resp = client.post("/optimize-energy", json=payload)
        assert resp.status_code == 422

    def test_hour_not_integer(self) -> None:
        hours = _valid_hours()
        hours[0]["hour"] = 1.5
        payload = _valid_payload(hours=hours)
        resp = client.post("/optimize-energy", json=payload)
        assert resp.status_code == 422

    def test_too_few_hours(self) -> None:
        hours = _valid_hours()[:23]
        payload = _valid_payload(hours=hours)
        resp = client.post("/optimize-energy", json=payload)
        assert resp.status_code == 422

    def test_too_many_hours(self) -> None:
        hours = _valid_hours()
        hours.append({"hour": 0, "demand_kwh": 100, "solar_kwh": 0, "tariff_bdt_per_kwh": 5})
        payload = _valid_payload(hours=hours)
        resp = client.post("/optimize-energy", json=payload)
        assert resp.status_code == 422

    def test_missing_hour_23(self) -> None:
        hours = [h for h in _valid_hours() if h["hour"] != 23]
        payload = _valid_payload(hours=hours)
        resp = client.post("/optimize-energy", json=payload)
        assert resp.status_code == 422


# --------------------------------------------------------------------------- #
# Negative and non-finite numbers
# --------------------------------------------------------------------------- #


class TestNumericValidation:
    @pytest.mark.parametrize("field_name", [
        "demand_kwh", "solar_kwh", "tariff_bdt_per_kwh",
    ])
    def test_negative_hourly_field_rejected(self, field_name: str) -> None:
        hours = _valid_hours()
        hours[0][field_name] = -1
        payload = _valid_payload(hours=hours)
        resp = client.post("/optimize-energy", json=payload)
        assert resp.status_code == 422

    @pytest.mark.parametrize("field_name", [
        "capacity_kwh", "initial_energy_kwh", "minimum_energy_kwh",
        "max_charge_kwh_per_hour", "max_discharge_kwh_per_hour",
    ])
    def test_negative_battery_field_rejected(self, field_name: str) -> None:
        bat = _valid_battery()
        bat[field_name] = -5
        payload = _valid_payload(battery=bat)
        resp = client.post("/optimize-energy", json=payload)
        assert resp.status_code == 422

    def test_demand_kwh_nan_rejected(self) -> None:
        payload = _valid_payload()
        payload["hours"][0]["demand_kwh"] = float("nan")
        resp = _post_raw(payload)
        assert resp.status_code == 422

    def test_demand_kwh_inf_rejected(self) -> None:
        payload = _valid_payload()
        payload["hours"][0]["demand_kwh"] = float("inf")
        resp = _post_raw(payload)
        assert resp.status_code == 422

    def test_solar_kwh_nan_rejected(self) -> None:
        payload = _valid_payload()
        payload["hours"][0]["solar_kwh"] = float("nan")
        resp = _post_raw(payload)
        assert resp.status_code == 422

    def test_tariff_kwh_inf_rejected(self) -> None:
        payload = _valid_payload()
        payload["hours"][0]["tariff_bdt_per_kwh"] = float("inf")
        resp = _post_raw(payload)
        assert resp.status_code == 422

    def test_battery_capacity_nan_rejected(self) -> None:
        bat = _valid_battery()
        bat["capacity_kwh"] = float("nan")
        resp = _post_raw(_valid_payload(battery=bat))
        assert resp.status_code == 422

    def test_battery_capacity_inf_rejected(self) -> None:
        bat = _valid_battery()
        bat["capacity_kwh"] = float("inf")
        resp = _post_raw(_valid_payload(battery=bat))
        assert resp.status_code == 422

    def test_zero_capacity_with_zero_initial_and_min_accepted(self, monkeypatch) -> None:
        import app.main as m
        import json as _json

        _no_op = _json.dumps(
            {
                "directive_interpretation": [
                    {
                        "note_index": 0,
                        "applies": False,
                        "directive_type": "no_op",
                        "structured_adjustment": None,
                        "explanation": "irrelevant",
                    }
                ]
            }
        )
        monkeypatch.setattr(
            m.interpreter,
            "_call_groq",
            lambda s, u: _no_op,
        )
        bat = _valid_battery()
        bat["capacity_kwh"] = 0
        bat["initial_energy_kwh"] = 0
        bat["minimum_energy_kwh"] = 0
        payload = _valid_payload(battery=bat)
        resp = client.post("/optimize-energy", json=payload)
        assert resp.status_code == 200



# --------------------------------------------------------------------------- #
# Invalid battery bounds
# --------------------------------------------------------------------------- #


class TestBatteryBounds:
    def test_minimum_exceeds_capacity_rejected(self) -> None:
        bat = _valid_battery()
        bat["capacity_kwh"] = 100
        bat["minimum_energy_kwh"] = 150
        payload = _valid_payload(battery=bat)
        resp = client.post("/optimize-energy", json=payload)
        assert resp.status_code == 422

    def test_initial_exceeds_capacity_rejected(self) -> None:
        bat = _valid_battery()
        bat["capacity_kwh"] = 100
        bat["initial_energy_kwh"] = 150
        payload = _valid_payload(battery=bat)
        resp = client.post("/optimize-energy", json=payload)
        assert resp.status_code == 422

    def test_initial_exceeds_capacity_error_message(self) -> None:
        with pytest.raises(ValidationError, match="initial_energy_kwh"):
            BatteryRequest(
                capacity_kwh=100,
                initial_energy_kwh=150,
                minimum_energy_kwh=40,
                max_charge_kwh_per_hour=50,
                max_discharge_kwh_per_hour=50,
            )

    def test_minimum_exceeds_capacity_error_message(self) -> None:
        with pytest.raises(ValidationError, match="minimum_energy_kwh"):
            BatteryRequest(
                capacity_kwh=100,
                initial_energy_kwh=50,
                minimum_energy_kwh=120,
                max_charge_kwh_per_hour=50,
                max_discharge_kwh_per_hour=50,
            )

    def test_equal_valid_bounds_accepted(self) -> None:
        req = BatteryRequest(
            capacity_kwh=200,
            initial_energy_kwh=200,
            minimum_energy_kwh=200,
            max_charge_kwh_per_hour=50,
            max_discharge_kwh_per_hour=50,
        )
        assert req.capacity_kwh == 200


# --------------------------------------------------------------------------- #
# Rejection of invented extra fields
# --------------------------------------------------------------------------- #


class TestExtraFieldRejection:
    def test_top_level_battery_efficiency_rejected(self) -> None:
        payload = _valid_payload()
        payload["battery_efficiency"] = 0.95
        resp = client.post("/optimize-energy", json=payload)
        assert resp.status_code == 422

    def test_hour_level_waste_kwh_rejected(self) -> None:
        payload = _valid_payload()
        payload["hours"][0]["waste_kwh"] = 10
        resp = client.post("/optimize-energy", json=payload)
        assert resp.status_code == 422

    def test_battery_level_efficiency_rejected(self) -> None:
        payload = _valid_payload()
        payload["battery"]["efficiency"] = 0.95
        resp = client.post("/optimize-energy", json=payload)
        assert resp.status_code == 422

    def test_top_level_extra_model_rejected(self) -> None:
        with pytest.raises(ValidationError):
            OptimizeEnergyRequest(
                scenario_id="X",
                operator_notes=["a"],
                hours=_valid_hours(),
                battery=_valid_battery(),
                extra_field="bad",
            )

    def test_hour_extra_model_rejected(self) -> None:
        with pytest.raises(ValidationError):
            HourRequest(
                hour=0,
                demand_kwh=100,
                solar_kwh=0,
                tariff_bdt_per_kwh=10,
                waste_kwh=5,
            )

    def test_battery_extra_model_rejected(self) -> None:
        with pytest.raises(ValidationError):
            BatteryRequest(
                capacity_kwh=200,
                initial_energy_kwh=100,
                minimum_energy_kwh=40,
                max_charge_kwh_per_hour=50,
                max_discharge_kwh_per_hour=50,
                efficiency=0.95,
            )

    def test_response_extra_model_rejected(self) -> None:
        with pytest.raises(ValidationError):
            OptimizeEnergyResponse(
                scenario_id="X",
                directive_interpretation=[],
                hourly_plan=[],
                total_grid_kwh=0,
                total_cost_bdt=0,
                peak_grid_kwh=0,
                plan_summary="ok",
                extra_field="bad",
            )


# --------------------------------------------------------------------------- #
# Directive type and adjustment shape construction
# --------------------------------------------------------------------------- #


class TestDirectiveConstruction:
    def test_solar_reduction_adjustment(self) -> None:
        adj = SolarReductionAdjustment(hours=[13, 14], factor=0.2)
        assert adj.hours == [13, 14]
        assert adj.factor == 0.2

    def test_minimum_battery_reserve_adjustment(self) -> None:
        adj = MinimumBatteryReserveAdjustment(hours=[18, 19, 20], minimum_energy_kwh=100)
        assert adj.minimum_energy_kwh == 100

    def test_no_charge_window_adjustment(self) -> None:
        adj = NoChargeWindowAdjustment(hours=[2, 3, 4])
        assert adj.hours == [2, 3, 4]

    def test_no_discharge_window_adjustment(self) -> None:
        adj = NoDischargeWindowAdjustment(hours=[18, 19])
        assert adj.hours == [18, 19]

    def test_max_grid_window_adjustment(self) -> None:
        adj = MaxGridWindowAdjustment(hours=[19, 20], max_grid_kwh=180)
        assert adj.max_grid_kwh == 180

    def test_directive_type_enum_values(self) -> None:
        values = [dt.value for dt in DirectiveType]
        assert set(values) == {
            "solar_reduction",
            "minimum_battery_reserve",
            "no_charge_window",
            "no_discharge_window",
            "max_grid_window",
            "no_op",
        }

    def test_battery_action_enum_values(self) -> None:
        values = [ba.value for ba in BatteryAction]
        assert set(values) == {"charge", "discharge", "idle"}

    def test_directive_interpretation_no_op(self) -> None:
        entry = DirectiveInterpretation(
            note_index=0,
            applies=False,
            directive_type=DirectiveType.NO_OP,
            structured_adjustment=None,
            explanation="Irrelevant note.",
        )
        assert entry.applies is False
        assert entry.structured_adjustment is None

    def test_directive_interpretation_solar_reduction(self) -> None:
        entry = DirectiveInterpretation(
            note_index=0,
            applies=True,
            directive_type=DirectiveType.SOLAR_REDUCTION,
            structured_adjustment=SolarReductionAdjustment(hours=[12, 13], factor=0.25),
            explanation="Cleaning window.",
        )
        assert entry.applies is True
        assert isinstance(entry.structured_adjustment, SolarReductionAdjustment)

    def test_hours_ascending_only_accepted(self) -> None:
        SolarReductionAdjustment(hours=[10, 11, 12], factor=0.5)

    def test_hours_descending_rejected(self) -> None:
        with pytest.raises(ValidationError, match="ascending"):
            SolarReductionAdjustment(hours=[12, 11], factor=0.5)

    def test_hours_duplicate_rejected(self) -> None:
        with pytest.raises(ValidationError, match="unique"):
            SolarReductionAdjustment(hours=[10, 10], factor=0.5)

    def test_hours_empty_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SolarReductionAdjustment(hours=[], factor=0.5)

    def test_hours_out_of_range_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SolarReductionAdjustment(hours=[0, 24], factor=0.5)


# --------------------------------------------------------------------------- #
# Response model construction
# --------------------------------------------------------------------------- #


class TestResponseConstruction:
    def _make_plan_entry(self, hour: int) -> dict:
        return {
            "hour": hour,
            "grid_kwh": 100,
            "solar_used_kwh": 50,
            "battery_action": "idle",
            "battery_kwh": 0,
            "battery_energy_after_kwh": 100,
        }

    def test_full_response_construction(self) -> None:
        resp = OptimizeEnergyResponse(
            scenario_id="TEST-01",
            directive_interpretation=[
                DirectiveInterpretation(
                    note_index=0,
                    applies=True,
                    directive_type=DirectiveType.SOLAR_REDUCTION,
                    structured_adjustment=SolarReductionAdjustment(hours=[13, 14], factor=0.2),
                    explanation="Reduced solar.",
                ),
            ],
            hourly_plan=[self._make_plan_entry(h) for h in range(24)],
            total_grid_kwh=2400,
            total_cost_bdt=30000,
            peak_grid_kwh=150,
            plan_summary="Test plan.",
        )
        assert resp.scenario_id == "TEST-01"
        assert len(resp.hourly_plan) == 24
        assert resp.total_grid_kwh == 2400

    def test_hourly_plan_entry_construction(self) -> None:
        entry = HourlyPlanEntry(
            hour=0,
            grid_kwh=90,
            solar_used_kwh=0,
            battery_action=BatteryAction.IDLE,
            battery_kwh=0,
            battery_energy_after_kwh=110,
        )
        assert entry.hour == 0
        assert entry.battery_action == BatteryAction.IDLE

    def test_hourly_plan_rejects_extra_field(self) -> None:
        with pytest.raises(ValidationError):
            HourlyPlanEntry(
                hour=0,
                grid_kwh=100,
                solar_used_kwh=0,
                battery_action="idle",
                battery_kwh=0,
                battery_energy_after_kwh=100,
                battery_efficiency=0.95,
            )

    def test_directive_interpretation_rejects_extra(self) -> None:
        with pytest.raises(ValidationError):
            DirectiveInterpretation(
                note_index=0,
                applies=True,
                directive_type=DirectiveType.NO_OP,
                structured_adjustment=None,
                explanation="Note.",
                priority="high",
            )

    def test_solar_reduction_factor_out_of_range(self) -> None:
        with pytest.raises(ValidationError):
            SolarReductionAdjustment(hours=[10], factor=1.5)

    def test_solar_reduction_factor_negative(self) -> None:
        with pytest.raises(ValidationError):
            SolarReductionAdjustment(hours=[10], factor=-0.1)