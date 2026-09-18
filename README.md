# GridWise LLM — BUP CSE Fest 2026 (Preliminary)

LLM-assisted campus energy optimizer. A language model interprets free-text
operator notes into machine-checkable directives; a deterministic pipeline then
produces the optimal 24-hour battery/grid schedule.

**Public API:** https://gridwise-llm-bup-2026.onrender.com
**Docs (Swagger):** https://gridwise-llm-bup-2026.onrender.com/docs

## Problem statement summary

Given 24 hourly records (`demand_kwh`, `solar_kwh`, `tariff_bdt_per_kwh`),
battery parameters, and 1–3 natural-language operator notes, return an optimal
24-hour plan minimizing grid cost while honoring the interpreted directives,
battery physics, energy balance, and end-of-day neutrality. See `docs/` for
the official Problem Statement, Participant Guide, and public samples.

## Architecture flow

POST /optimize-energy runs: schema validation (400/422) -> LLM
interpretation (ALL notes, ONE request, strict JSON, one entry per note) ->
guardrail validation -> directive application -> PuLP/CBC optimization ->
replay validation -> official 200 response. Any interpretation, guardrail,
solver, or replay failure maps to a controlled 500 with a generic message.

## Supported directive types

- `solar_reduction`: hours + factor 0..1 (solar fraction that REMAINS usable)
- `minimum_battery_reserve`: hours + minimum_energy_kwh (absolute kWh)
- `no_charge_window`: hours
- `no_discharge_window`: hours
- `max_grid_window`: hours + max_grid_kwh
- `no_op`: null (requires applies=false; irrelevant notes only)

Hours: non-empty unique ascending integers 0–23.

## LLM interpretation and failure policy

Groq primary, Gemini secondary (official SDKs). Groq model:
`qwen/qwen3.8-27b`. No regex fallback, no hard-coded phrases, no Ollama. One
schema-guided repair per provider (`LLM_REPAIR_ATTEMPTS=1`); primary infra
failure fails over once. Total failure -> HTTP 500, never `no_op`.

## Deterministic guardrails

`app/guardrails.py` enforces: one entry per note in order; six official
types only; no_op iff applies=false + null adjustment; exact shapes and
ranges otherwise; no extra fields. Nothing is clipped or downgraded.

## Optimization model (PuLP + CBC)

`app/optimizer.py` solves a 24-hour MILP (CBC) minimizing tariff-weighted
grid energy under balance, battery, window, reserve, cap, and neutrality
constraints. Infeasibility raises `SolverError`.

## Independent replay validation

`app/validator.py` replays the plan hour by hour. Mismatch raises
`PlanValidationError`.

## API endpoints and contracts

- `GET /health` -> `200 {"status": "ok"}`.
- `POST /optimize-energy` -> `200` with `scenario_id`,
  `directive_interpretation`, `hourly_plan` (24 entries),
  `total_grid_kwh`, `total_cost_bdt`, `peak_grid_kwh`, `plan_summary`.
- Malformed JSON -> 400; invalid schema -> 422; pipeline failures -> 500.

## Local installation (Python 3.11)

python -m venv .venv; activate it; then: pip install -r requirements.txt

## Environment variables

Copy `.env.example` to `.env`; never commit `.env`. Names only:

LLM_PROVIDER= / LLM_MODEL= / LLM_API_KEY= / LLM_TIMEOUT_SECONDS=30 /
LLM_PRIMARY_PROVIDER=groq / LLM_SECONDARY_PROVIDER=gemini /
GROQ_API_KEY= / GROQ_MODEL= / GEMINI_API_KEY= / GEMINI_MODEL= /
LLM_REPAIR_ATTEMPTS=1 / REQUEST_TIMEOUT_SECONDS=30 /
HEALTH_READY_TIMEOUT_SECONDS=60

## Run locally

uvicorn app.main:app --host 0.0.0.0 --port 8000

## Health check

curl http://127.0.0.1:8000/health

## Optimize-energy example

POST JSON: scenario_id, operator_notes, 24 hours entries, battery object.
Valid inputs are the case.input objects in the official sample JSON.

## Automated tests

python -m pytest -q  (373 passed, 1 skipped, 0 failed; mocked LLM, no keys)

## Docker

docker build -t gridwise-llm:submission .
docker run --rm -d --name gridwise-test --env-file .env -p 8000:8000 gridwise-llm:submission

Image note: Dockerfile + .dockerignore provided (3.11-slim, $PORT default
8000, .env/keys excluded). Not executed locally; Docker was unavailable.

## Public deployment

- Base: https://gridwise-llm-bup-2026.onrender.com
- Health: https://gridwise-llm-bup-2026.onrender.com/health
- Docs: https://gridwise-llm-bup-2026.onrender.com/docs
- Optimize: https://gridwise-llm-bup-2026.onrender.com/optimize-energy

## Repository structure

app/ = FastAPI service; tests/ = suite; docs/ = official PDFs + samples.
Also: Dockerfile, .dockerignore, .env.example, requirements.txt,
SPEC_AUDIT.md, prompt.txt.

## Security and secret handling

Keys only in gitignored `.env` (also excluded from the image). Error
responses/logs never include keys, prompts, raw output, or traces.

## Performance and verification results

Suite: 373 passed, 1 skipped, 0 failed. Deployed SAMPLE-01: PASS — 200;
2 notes -> solar_reduction [12,13] x0.25 + no_op; 24 entries hours 0-23;
grid 2692.5, cost 38365.0, peak 187.5 (match plan); neutrality holds.
Latency: 2.23 s.

## Known limitations

Groq free-tier rate limits (429 under back-to-back load; controlled 500 +
one failover). Render cold starts add latency after idle. Only SAMPLE-01
verified live, by design.

## Submission

- Docker image: <registry/repository:tag> (TODO)
- Demo video URL: <url> (TODO)
