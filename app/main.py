"""FastAPI application entrypoint.

Exposes the two official endpoints:
- GET /health  -> 200 {"status": "ok"}
- POST /optimize-energy -> request schema validation -> LLM interpretation ->
  guardrail validation -> deterministic optimization -> official response.

Failure mapping (controlled, secret-free — Implementation Invariant 10):
- Invalid request bodies are rejected by the exact request schema as 400/422.
- Interpretation / provider failures (app.llm_interpreter.InterpretationError),
  guardrail violations, solver failures (SolverError), and replay-validation
  failures (PlanValidationError) all map to a controlled HTTP 500 with a
  generic message. No raw prompts, raw responses, API keys, or stack traces
  are ever included in a response.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.config import settings
from app.directive_applier import apply_directives
from app.guardrails import (
    GuardrailValidationError,
    validate_directive_interpretation,
)
from app.llm_interpreter import InterpretationError, build_interpreter
from app.optimizer import SolverError, optimize_hourly_plan
from app.response_builder import build_success_response
from app.schemas import (
    DirectiveInterpretation,
    HealthResponse,
    OptimizeEnergyRequest,
    OptimizeEnergyResponse,
)
from app.validator import PlanValidationError, validate_hourly_plan

app = FastAPI(
    title=settings.app_name,
    version="0.2.0",
)

#: Runtime LLM interpreter (Groq primary, Gemini secondary), built from
#: settings; tests replace this instance's methods — no real network in tests.
interpreter = build_interpreter()


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(
    request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    """Sanitized request-validation error response.

    FastAPI's default handler echoes the offending `input` value back in the
    error detail; a non-finite number (NaN/Infinity) then cannot be serialized
    and would turn a validation failure into a crash. The `input` field is
    therefore dropped from each reported error.

    Status codes follow the Problem Statement section 6.1: malformed/invalid
    JSON is 400; structurally invalid but well-formed JSON is 422.
    """
    errors: list[dict[str, Any]] = []
    code = 422
    for err in exc.errors():
        if err.get("type") == "json_invalid":
            code = 400
        errors.append(
            {
                "loc": list(err.get("loc", [])),
                "msg": err.get("msg", ""),
                "type": err.get("type", ""),
            }
        )
    return JSONResponse(status_code=code, content={"detail": errors})


def run_optimization_pipeline(
    request: OptimizeEnergyRequest,
    validated_directives: Sequence[DirectiveInterpretation],
) -> OptimizeEnergyResponse:
    """Run the deterministic pipeline over already-validated directives.

    Steps: apply directives -> optimize the 24-hour plan -> replay-validate the
    plan -> build the official response.

    ``validated_directives`` must already have passed the strict guardrail layer
    (:func:`app.guardrails.validate_directive_interpretation`); this function
    does not re-interpret operator notes.

    :raises app.optimizer.SolverError: when no optimal plan exists.
    :raises app.validator.PlanValidationError: when the plan fails replay.
    """
    inputs = apply_directives(
        validated_directives,
        request.hours,
        request.battery,
    )
    plan = optimize_hourly_plan(request.hours, request.battery, inputs)
    validate_hourly_plan(
        plan,
        hours=request.hours,
        battery=request.battery,
        inputs=inputs,
    )
    return build_success_response(request, validated_directives, plan)


@app.get(
    "/health",
    response_model=HealthResponse,
    status_code=200,
    tags=["health"],
)
def health() -> HealthResponse:
    """Readiness probe. Returns exactly {"status": "ok"}."""
    return HealthResponse(status="ok")


@app.post(
    "/optimize-energy",
    response_model=OptimizeEnergyResponse,
    status_code=200,
    tags=["optimize"],
)
def optimize_energy(request: OptimizeEnergyRequest) -> OptimizeEnergyResponse:
    """Interpret operator notes with the LLM, then optimize deterministically.

    The request has already been validated against the exact official schema;
    invalid bodies receive a 400/422 from request validation. Every remaining
    failure (interpretation/provider, guardrail, solver, replay validation) is
    mapped to a controlled HTTP 500 with a generic, secret-free message.
    """
    try:
        interpreted = interpreter.interpret(
            request.operator_notes,
            request.battery.capacity_kwh,
        )
    except InterpretationError:
        raise HTTPException(
            status_code=500,
            detail="LLM interpretation failed; please retry later",
        ) from None

    try:
        validated_directives = validate_directive_interpretation(
            interpreted,
            request.operator_notes,
            request.battery.capacity_kwh,
        )
    except GuardrailValidationError:
        raise HTTPException(
            status_code=500,
            detail="directive interpretation could not be validated",
        ) from None

    try:
        return run_optimization_pipeline(request, validated_directives)
    except (SolverError, PlanValidationError):
        raise HTTPException(
            status_code=500,
            detail="optimization failed; please retry later",
        ) from None
