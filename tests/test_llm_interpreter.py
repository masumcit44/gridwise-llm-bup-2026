"""Focused mocked tests for the runtime LLM interpreter and POST integration.

No real network or API calls: provider methods are monkeypatched on the
interpreter instance, and the FastAPI endpoint is exercised via TestClient
with the module-level interpreter stubbed.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app.llm_interpreter import (
    InterpretationError,
    LLMInterpreter,
    build_interpret_prompts,
    extract_json_payload,
)

NOTES = [
    "Cut usable solar by half from 12:00 to 14:00 for panel cleaning.",
    "Keep the battery at least 50% full all day.",
]
CAPACITY = 200.0

VALID_TWO_NOTES = {
    "directive_interpretation": [
        {
            "note_index": 0,
            "applies": True,
            "directive_type": "solar_reduction",
            "structured_adjustment": {"hours": [12, 13, 14], "factor": 0.5},
            "explanation": "Halve usable solar during cleaning.",
        },
        {
            "note_index": 1,
            "applies": True,
            "directive_type": "minimum_battery_reserve",
            "structured_adjustment": {"hours": list(range(24)), "minimum_energy_kwh": 100.0},
            "explanation": "50% of 200 kWh is 100 kWh.",
        },
    ]
}

VALID_NO_OP = {
    "directive_interpretation": [
        {
            "note_index": 0,
            "applies": False,
            "directive_type": "no_op",
            "structured_adjustment": None,
            "explanation": "Note is irrelevant to the 24-hour schedule.",
        }
    ]
}


def make_interpreter() -> LLMInterpreter:
    return LLMInterpreter(
        primary_provider="groq",
        secondary_provider="gemini",
        timeout_seconds=6,
        repair_attempts=1,
    )


def stub(
    interpreter: LLMInterpreter,
    *,
    groq=None,
    gemini=None,
) -> dict[str, list[tuple[str, str]]]:
    """Monkeypatch provider call methods; returns per-provider call logs."""

    calls: dict[str, list[tuple[str, str]]] = {"groq": [], "gemini": []}

    def make(name: str, handler):
        if handler is None:
            return None
        log = calls[name]

        def _call(system: str, user: str) -> str:
            log.append((system, user))
            result = handler(system, user, len(log))
            if isinstance(result, Exception):
                raise result
            return result

        return _call

    groq_call = make("groq", groq)
    gemini_call = make("gemini", gemini)
    if groq_call is not None:
        interpreter._call_groq = groq_call  # type: ignore[method-assign]
    if gemini_call is not None:
        interpreter._call_gemini = gemini_call  # type: ignore[method-assign]
    return calls


# --------------------------------------------------------------------------- #
# Interpreter behavior
# --------------------------------------------------------------------------- #


class TestValidInterpretation:
    def test_valid_output_returns_validated_entries(self, monkeypatch) -> None:
        interp = make_interpreter()
        calls = stub(
            interp,
            groq=lambda s, u, n: json.dumps(VALID_TWO_NOTES),
            gemini=lambda s, u, n: pytest.fail("secondary must not be called"),
        )
        entries = interp.interpret(NOTES, CAPACITY)

        assert len(calls["groq"]) == 1
        assert calls["gemini"] == []
        assert len(entries) == 2
        assert entries[0]["directive_type"] == "solar_reduction"
        assert entries[1]["structured_adjustment"]["minimum_energy_kwh"] == 100.0

    def test_no_op_entry_is_valid_output(self, monkeypatch) -> None:
        interp = make_interpreter()
        stub(interp, groq=lambda s, u, n: json.dumps(VALID_NO_OP))
        entries = interp.interpret(NOTES[:1], CAPACITY)
        assert entries[0]["directive_type"] == "no_op"
        assert entries[0]["applies"] is False
        assert entries[0]["structured_adjustment"] is None

    def test_all_notes_in_one_request_with_capacity(self) -> None:
        system, user = build_interpret_prompts(NOTES, CAPACITY)
        assert "[note_index=0]" in user
        assert "[note_index=1]" in user
        assert "200.0" in system
        assert "minimum_energy_kwh" in system


# --------------------------------------------------------------------------- #
# Repair and failover
# --------------------------------------------------------------------------- #


class TestRepairAndFailover:
    def test_malformed_json_repaired_in_one_extra_call(self) -> None:
        interp = make_interpreter()
        calls = stub(
            interp,
            groq=lambda s, u, n: "not json at all"
            if n == 1
            else json.dumps(VALID_TWO_NOTES),
        )
        entries = interp.interpret(NOTES, CAPACITY)
        assert len(calls["groq"]) == 2  # initial + one schema-guided repair
        assert len(entries) == 2

    def test_guardrail_failure_repaired(self) -> None:
        interp = make_interpreter()
        invalid = json.dumps(
            {
                "directive_interpretation": [
                    {
                        "note_index": 0,
                        "applies": True,
                        "directive_type": "solar_reduction",
                        "structured_adjustment": {"hours": [12], "factor": 1.5},
                        "explanation": "invalid factor",
                    }
                ]
            }
        )
        valid = json.dumps(VALID_NO_OP)
        calls = stub(interp, groq=lambda s, u, n: invalid if n == 1 else valid)
        entries = interp.interpret(NOTES[:1], CAPACITY)
        assert len(calls["groq"]) == 2
        assert entries[0]["directive_type"] == "no_op"

    def test_repair_exhausted_fails_over_to_secondary(self) -> None:
        interp = make_interpreter()
        invalid = json.dumps(VALID_NO_OP)  # wrong entry count for two notes
        calls = stub(
            interp,
            groq=lambda s, u, n: invalid,
            gemini=lambda s, u, n: json.dumps(VALID_TWO_NOTES),
        )
        entries = interp.interpret(NOTES, CAPACITY)
        assert len(calls["groq"]) == 2  # initial + repair, then failover
        assert len(calls["gemini"]) == 1
        assert len(entries) == 2

    def test_primary_technical_failure_fails_over_once(self) -> None:
        interp = make_interpreter()
        calls = stub(
            interp,
            groq=lambda s, u, n: RuntimeError("groq unavailable"),
            gemini=lambda s, u, n: json.dumps(VALID_TWO_NOTES),
        )
        entries = interp.interpret(NOTES, CAPACITY)
        assert len(calls["groq"]) == 1  # infrastructure failure: no repair call
        assert len(calls["gemini"]) == 1
        assert len(entries) == 2

    def test_total_failure_raises_controlled_error(self) -> None:
        interp = make_interpreter()
        stub(
            interp,
            groq=lambda s, u, n: RuntimeError("down"),
            gemini=lambda s, u, n: RuntimeError("down"),
        )
        with pytest.raises(InterpretationError):
            interp.interpret(NOTES, CAPACITY)

    def test_invalid_after_repair_is_never_no_op(self) -> None:
        interp = make_interpreter()
        invalid = json.dumps(VALID_NO_OP)  # wrong count; must not become no_op
        stub(
            interp,
            groq=lambda s, u, n: invalid,
            gemini=lambda s, u, n: invalid,
        )
        with pytest.raises(InterpretationError):
            interp.interpret(NOTES, CAPACITY)

    def test_error_message_is_secret_free(self) -> None:
        interp = make_interpreter()
        stub(
            interp,
            groq=lambda s, u, n: RuntimeError("SECRET-KEY-12345 leaked"),
            gemini=lambda s, u, n: "SECRET-KEY-12345 SECRET-KEY-12345",
        )
        with pytest.raises(InterpretationError) as exc_info:
            interp.interpret(NOTES, CAPACITY)
        message = str(exc_info.value)
        assert "SECRET-KEY-12345" not in message
        assert "Cut usable solar" not in message
        assert "Traceback" not in message


# --------------------------------------------------------------------------- #
# POST /optimize-energy integration (mocked interpreter, no network)
# --------------------------------------------------------------------------- #


def _request_payload() -> dict:
    hours = [
        {
            "hour": hour,
            "demand_kwh": 100.0,
            "solar_kwh": 30.0 if 10 <= hour <= 15 else 0.0,
            "tariff_bdt_per_kwh": 12.0 if 18 <= hour <= 21 else 6.0,
        }
        for hour in range(24)
    ]
    return {
        "scenario_id": "POST-1",
        "operator_notes": ["Cut usable solar by half from 12:00 to 14:00."],
        "hours": hours,
        "battery": {
            "capacity_kwh": 200.0,
            "initial_energy_kwh": 100.0,
            "minimum_energy_kwh": 40.0,
            "max_charge_kwh_per_hour": 50.0,
            "max_discharge_kwh_per_hour": 50.0,
        },
    }


OFFICIAL_RESPONSE_FIELDS = {
    "scenario_id",
    "directive_interpretation",
    "hourly_plan",
    "total_grid_kwh",
    "total_cost_bdt",
    "peak_grid_kwh",
    "plan_summary",
}


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(
        main_module.interpreter,
        "_call_groq",
        lambda s, u: json.dumps(VALID_NO_OP),
        raising=False,
    )
    return TestClient(main_module.app)


class TestOptimizeEnergyEndpoint:
    def test_success_returns_official_response(self, client) -> None:
        resp = client.post("/optimize-energy", json=_request_payload())
        assert resp.status_code == 200
        body = resp.json()
        assert body["scenario_id"] == "POST-1"
        assert len(body["directive_interpretation"]) == 1
        assert body["directive_interpretation"][0]["directive_type"] == "no_op"
        assert len(body["hourly_plan"]) == 24
        assert body["hourly_plan"][0]["hour"] == 0
        assert set(body) == OFFICIAL_RESPONSE_FIELDS

    def test_invalid_body_is_422(self, client) -> None:
        payload = _request_payload()
        payload["operator_notes"] = []  # official schema requires 1..3 notes
        resp = client.post("/optimize-energy", json=payload)
        assert resp.status_code == 422

    def test_malformed_json_body_is_400(self, client) -> None:
        resp = client.post(
            "/optimize-energy",
            content="{not json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 400

    def test_interpretation_failure_maps_to_500_secret_free(
        self, client, monkeypatch
    ) -> None:
        def boom(notes, capacity):
            raise InterpretationError(
                "LLM interpretation failed after all provider attempts"
            )

        monkeypatch.setattr(main_module.interpreter, "interpret", boom)
        resp = client.post("/optimize-energy", json=_request_payload())
        assert resp.status_code == 500
        text = resp.text
        assert "Cut usable solar" not in text
        assert "SECRET" not in text.upper()
        assert "Traceback" not in text

    def test_solver_failure_maps_to_500(self, client, monkeypatch) -> None:
        from app.optimizer import SolverError

        def boom(request, directives):
            raise SolverError("no feasible plan", status="Infeasible")

        monkeypatch.setattr(main_module, "run_optimization_pipeline", boom)
        resp = client.post("/optimize-energy", json=_request_payload())
        assert resp.status_code == 500
        assert "no feasible plan" not in resp.text
