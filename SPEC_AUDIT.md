# GridWise LLM — Specification Audit

**BUP CSE Fest 2026 · Online Preliminary · LLM-Assisted Operator Directive Interpretation**

This document is the consolidated specification audit produced from the three official files inside `docs/`:

1. `BUP_CSE_FEST_2026_Preliminary_Problem_Statement_GridWise_LLM.pdf` (Problem Statement, referenced as **PS §n**)
2. `BUP_CSE_FEST_2026_Participant_Guide_&_Evaluation_Rubric_GridWise_LLM.pdf` (Participant Guide, referenced as **Guide §n**)
3. `BUP_CSE_FEST_2026_Preli_Public_Sample_Cases.json` (Public Sample Cases v2.0, referenced as **Samples**)

Authority per the official documents:

- The **Problem Statement** is canonical for API fields, directive types, guardrails, battery behavior, energy accounting, and optimization validity (PS §12, Guide §04).
- The **Participant Guide** is canonical for deployment, repository, submission, performance, scoring, penalties, and tie-break rules (PS §12).
- The **Public Sample Cases** are worked validation examples, not hidden judge cases (Samples `_meta`).

All 10 public sample cases were independently replayed and verified (energy balance, effective-solar ceiling, battery transitions/bounds/rate limits, end-of-day neutrality, totals, peak, and directive extraction) — every case passes.

---

## 1. Sources & Verification

| Source | Role | Status |
|---|---|---|
| Preliminary Problem Statement | Canonical: endpoints, schemas, directives, guardrails, battery, energy accounting, validity, objective | Fully extracted; no contradictions found with samples |
| Participant Guide & Evaluation Rubric | Canonical: deployment, repository, submission, scoring 100 pts, penalties, tie-breaks, Docker, README, video | Fully extracted; spelling-out of scoring consistent with statement |
| Public Sample Cases JSON (v2.0) | Worked examples; not hidden judge set | 10/10 replay-clean; every rule in the statement cross-checks against at least one case |

Verification method: independent script recomputed `total_grid_kwh`, `total_cost_bdt`, `peak_grid_kwh`, per-hour energy balance, solar ceilings, battery transitions (`charge`/`discharge`/`idle`), battery bounds (`minimum_energy_kwh ≤ E_after ≤ capacity_kwh`), charge/discharge rate limits, and end-of-day neutrality for all 10 cases. All passed to 1e-6.

---

## 2. Confirmed Official Requirements

### 2.1 GET /health contract

- Return **HTTP 200** with a JSON object containing **`{"status": "ok"}`** when the service is ready (PS §6.1, §6.2; Guide §08).
- Readiness must be reported **within 60 seconds of service start** (Guide §08).
- It is the only harness-facing readiness check. Minimal response (`{"status":"ok"}` exactly) is the safest interpretation.

### 2.2 POST /optimize-energy — exact request schema (PS §7)

One JSON object. `hours` must contain exactly 24 entries for hours 0–23. `operator_notes` must contain 1–3 non-empty natural-language strings referring to the same 24-hour scenario.

**Top-level fields (all required, Samples `schema_notes`)**

| Field | Type | Constraint |
|---|---|---|
| `scenario_id` | string | Unique synthetic scenario identifier |
| `operator_notes` | array[1..3] of string | Natural-language campus operator notes to interpret |
| `hours` | array[24] | Hourly demand, solar availability, and grid tariff |
| `battery` | object | Capacity, starting energy, reserve, and hourly limits |

**Hour entry (all fields required)**

| Field | Type | Meaning |
|---|---|---|
| `hour` | integer | Unique integer from 0 to 23 |
| `demand_kwh` | number | Campus demand that must be supplied in this hour |
| `solar_kwh` | number | Base solar energy available before operator-note adjustments |
| `tariff_bdt_per_kwh` | number | Grid electricity price for this hour |

**Battery object (all fields required — no efficiency field exists; do not invent one)**

| Field | Meaning |
|---|---|
| `capacity_kwh` | Maximum energy the battery can store |
| `initial_energy_kwh` | Battery energy at the start of hour 0 |
| `minimum_energy_kwh` | Base reserve level the battery must never go below |
| `max_charge_kwh_per_hour` | Maximum energy that may be added in one hour |
| `max_discharge_kwh_per_hour` | Maximum energy that may be removed in one hour |

### 2.3 POST /optimize-energy — exact successful response schema (PS §10)

**Top-level response fields**

| Field | Type | Requirement |
|---|---|---|
| `scenario_id` | string | Must match the request scenario_id |
| `directive_interpretation` | array | One machine-checkable entry for every operator note |
| `hourly_plan` | array[24] | One plan entry for every hour 0–23 |
| `total_grid_kwh` | number | Sum of `grid_kwh` across all 24 hours |
| `total_cost_bdt` | number | Sum of `grid_kwh × tariff_bdt_per_kwh` across all hours |
| `peak_grid_kwh` | number | Maximum hourly `grid_kwh` in the returned plan |
| `plan_summary` | string | Short human-readable explanation of the final strategy |

**Directive interpretation entry**

| Field | Requirement |
|---|---|
| `note_index` | Zero-based index of the corresponding `operator_notes` entry |
| `applies` | `true` for every applicable non-no_op directive; `false` only for no_op |
| `directive_type` | One supported directive type (Section 2.4); `no_op` required when `applies = false` |
| `structured_adjustment` | Exact machine-checkable object (Section 2.4), or `null` only for no_op |
| `explanation` | Short explanation of the interpretation (free text; not byte-matched) |

Rules: exactly one entry per note, returned in `note_index` order 0..N-1, no missing or duplicate mappings (PS §5.1, §10.2; Guide §02, §08; Samples `_meta`).

**Hourly plan entry**

| Field | Allowed value / meaning |
|---|---|
| `hour` | Integer 0 through 23 |
| `grid_kwh` | Non-negative grid energy purchased in this hour |
| `solar_used_kwh` | Solar energy used in this hour; cannot exceed effective solar |
| `battery_action` | Exactly one of `charge`, `discharge`, `idle` |
| `battery_kwh` | Non-negative magnitude of the battery action; must be 0 when idle |
| `battery_energy_after_kwh` | Battery energy immediately after completing this hour |

`total_grid_kwh`, `total_cost_bdt`, and `peak_grid_kwh` must match values recalculated from `hourly_plan`; the `hourly_plan` is the source of truth (PS §11.3; Guide §09; Samples `_meta`).

### 2.4 Supported directive types and structured_adjustment shapes (PS §4.1; Samples `allowed_enums`)

| Directive type | Meaning | Required `structured_adjustment` |
|---|---|---|
| `solar_reduction` | Reduce usable solar during specific hours | `{"hours":[...], "factor": number}` |
| `minimum_battery_reserve` | Keep battery energy at or above a required level | `{"hours":[...], "minimum_energy_kwh": number}` |
| `no_charge_window` | Battery charging unavailable during specific hours | `{"hours":[...]}` |
| `no_discharge_window` | Battery discharging unavailable during specific hours | `{"hours":[...]}` |
| `max_grid_window` | Grid import may not exceed a stated amount in specific hours | `{"hours":[...], "max_grid_kwh": number}` |
| `no_op` | Note does not affect the current 24-hour energy schedule | `null` |

**Deterministic effect applied by the optimizer (PS §5.3)**

| Directive | Effect |
|---|---|
| `solar_reduction` | `effective_solar[h] = original_solar[h] × factor` for each listed hour |
| `minimum_battery_reserve` | `battery_energy_after_kwh[h] >= max(base minimum_energy_kwh, directive minimum_energy_kwh)` for each listed hour |
| `no_charge_window` | battery charge amount = 0 in the listed hours |
| `no_discharge_window` | battery discharge amount = 0 in the listed hours |
| `max_grid_window` | `grid_kwh[h] <= max_grid_kwh` in the listed hours |
| `no_op` | No change to the optimization model |

Only these types are accepted. Hidden notes map to exactly one supported type or `no_op`; they will never require an unpublished directive type (PS §11.4; Guide §10).

### 2.5 Time-window normalization rules

- Whole-hour intervals; **start hour inclusive, end hour excluded**: `1 PM to 3 PM → hours [13, 14]` (PS §4.2, §5.1; Samples `_meta`).
- Verified against samples: `2 AM until 5 AM → [2,3,4]` (SAMPLE-02); `noon until 2 PM → [12,13]` (SAMPLE-01); `10 AM until noon → [10,11]` (SAMPLE-06); `6 PM until 9 PM → [18,19,20]` (SAMPLE-04, SAMPLE-05); `11 AM AND 2 PM → [11,12,13]` (SAMPLE-09).
- Every `hours` array inside `structured_adjustment` must contain unique integers 0–23 in ascending order (PS §5.1, §08; Samples `interpretation_rules`).
- Invalid hours must not be silently clipped or reordered-with-invention; they are guardrail failures.

### 2.6 Solar-reduction percentage normalization rules

- `factor` = the **usable fraction that remains**, and must be between 0 and 1 inclusive (PS §08; Samples `_meta`).
- An **80% reduction means `factor = 0.2`** (PS §5.1, §08; SAMPLE-09).
- Verified: `roughly 25% → 0.25` (SAMPLE-01); `half → 0.5` (SAMPLE-06); `80% reduction → 0.2` / `one-fifth → 0.2` (SAMPLE-09; PS §11.4 paraphrase examples).
- `effective_solar[h] = solar_kwh[h] × factor`; `solar_used_kwh` may be less than effective (unused solar is curtailed); grid export is not part of the challenge (PS §9.4).

### 2.7 Battery state-transition, bounds, and rate-limit rules

- **State transitions (PS §9.1):**
  - `charge`: `E_after = E_before + battery_kwh`
  - `discharge`: `E_after = E_before - battery_kwh`
  - `idle`: `E_after = E_before` and `battery_kwh = 0`
- **Bounds (PS §9.2):** `minimum_energy_kwh <= E_after <= capacity_kwh`. A `minimum_battery_reserve` directive may raise the floor (`max(base, directive)`) in listed hours.
- **Rate limits (PS §9.3):** if `charge`, `battery_kwh <= max_charge_kwh_per_hour`; if `discharge`, `battery_kwh <= max_discharge_kwh_per_hour`.
- Charge is additionally bounded by capacity headroom (because `E_after <= capacity`); samples idle when full.
- **No efficiency/loss/conversion factor** exists anywhere in the official rules — verified by exact replay of all 10 samples.

### 2.8 Hourly energy-balance equation (PS §9.5; Samples `_meta`)

```
grid_kwh + solar_used_kwh + battery_discharge_kwh = demand_kwh + battery_charge_kwh   (every hour)
```

- Charge enters on the demand side; discharge on the supply side. Exactly one `battery_action` per hour; `battery_kwh` is its non-negative magnitude.

### 2.9 End-of-day battery-neutrality rule (PS §9.6)

- `battery_energy_after_kwh` at hour 23 **must equal `initial_energy_kwh`** (within tolerance 0.01).
- Rationale: the battery may shift energy across hours but cannot be consumed as a free one-time source (PS §9.6).

### 2.10 Optimization objective (PS §5.2; Guide §08)

- Minimize `total_cost_bdt = SUM(grid_kwh[h] * tariff_bdt_per_kwh[h])` for h = 0..23.
- Validity first, then cost: a low-cost schedule is invalid if it breaks any energy, battery, or operator-directive rule.
- **Optimization quality formula** (Guide §08): `quality_ratio = min(1, organizer_optimal_cost / recalculated_team_cost)`; `Optimization Quality = 10 × average(quality_ratio)` over optimization hidden cases. Invalid cases get zero optimization credit.
  - If both organizer and team costs are within tolerance of 0 → `quality_ratio = 1`.
  - If organizer cost is within tolerance of 0 but team cost is above tolerance → the PDF text is truncated; the implied `quality_ratio = 0` is treated as an assumption (Section 3).

### 2.11 Deterministic LLM guardrail requirements (PS §02, §08; Guide §03, §04)

- The LLM (or other language-capable generative model) **must be part of the operator-note interpretation path**, producing the structured `directive_interpretation` consumed by the optimizer.
- Using an LLM only for `plan_summary`, documentation, or cosmetic text **does not satisfy** the requirement (PS §02; Guide §02, §04, §09).
- **Hard-coded phrase matching as the sole interpreter is not compliant.** Hidden notes may paraphrase; the model must be part of the path (Guide §04; Samples `how_to_use`).
- LLM output must be treated as untrusted structured data until **deterministic validation** passes (PS §08):
  - `directive_type` must be one of the allowed values.
  - `note_index` must identify an existing note; each note appears exactly once.
  - Every listed hour must be a unique integer 0–23, ascending.
  - `solar_reduction.factor` must be 0–1 inclusive.
  - Reserve values finite, non-negative, and not exceeding capacity.
  - `max_grid_kwh` finite and non-negative.
  - No invention: the interpretation may not change base demand, tariff, or battery parameters unless a supported directive explicitly allows it.
  - `applies` semantics: `no_op` → `applies=false`, `structured_adjustment=null`; every other directive → `applies=true` with the exact required shape.
  - **Final replay**: the completed schedule is replayed after optimization to verify every extracted directive was actually followed (PS §08, §11.2).
- Does not treat malformed output, provider failures, or validation failures as `no_op`; safe-failure handling is required instead.

### 2.12 Error-handling requirements (PS §6.1; Guide §05, §08)

| Code | Meaning |
|---|---|
| 200 | Successful health or optimization response |
| 400 | Malformed JSON or structurally invalid request |
| 422 | Optional: semantically invalid but well-formed request |
| 500 | Controlled internal error; no secrets, no raw stack traces |

- Robustness required: malformed JSON, invalid structured input, LLM/provider errors, repeated requests, and unexpected valid numeric combinations must not crash the service (Guide §05).
- Secret safety: no API keys, tokens, raw secrets, or sensitive stack traces in repo, logs, or responses (Guide §04, §08).

### 2.13 Performance and latency requirements (Guide §08)

- `GET /health` returns `{"status":"ok"}` within 60 s of service start.
- `POST /optimize-energy` must complete within **30 seconds**; responses beyond that are failures.
- p95 latency: `<=5s` → 3/3 points; `>5s to 15s` → 2/3; `>15s to 30s` → 1/3; `>30s` → 0/3 and timed-out requests fail.
- Failure rate: valid requests should not return 5xx, invalid JSON, or no response; stable across repeated LLM-backed requests.

### 2.14 Deployment, Docker, repository, README, and video requirements (Guide §02–§05, §08)

- **Deliverable:** one deployed public HTTP API exposing both exact endpoints; judge reaches them without login, dashboard, manual approval, VPN, or private network (Guide §03).
- **Reachability:** endpoints must work from outside the dev environment and remain reachable throughout the evaluation window, including repeated LLM-backed requests.
- **Repository policy:** create a new GitHub repository after question reveal; keep it private during the event; make it public after the submission deadline for evaluation (Guide §04–§05).
- **README.md:** self-contained; includes source setup, environment-variable names, model/provider or local model identifier, LLM role, guardrails, optimizer/solver, exact run command, `/health` and `/optimize-energy` curl examples, public-sample test command, dependencies, and known limitations. No secret values (Guide §02, §05, §08).
- **Docker fallback image:** pullable registry reference with exact tag or digest; image exposes the documented service port, binds to `0.0.0.0`, contains no baked-in secrets, and reaches `/health` using the documented command (Guide §02, §03, §08).
- **3-minute solution video:** accessible; maximum 3 minutes; explains the problem, architecture overview, LLM → deterministic guardrails → optimizer flow, and how the submission is run/tested. **The video contributes 0 base points and is a tie-break resource only** (Guide §06, §10).
- LLM availability/responsibility: teams own keys, quota, rate limits, and provider availability (Guide §03–§04).

### 2.15 Full 100-point scoring breakdown (Guide §06, §07)

| # | Category | Points | Sub-scoring |
|---|---|---|---|
| 1 | LLM Directive Interpretation | 25 | 5 relevance/no_op + 5 directive_type + 5 affected hours + 5 numeric values/required shape + 5 paraphrase robustness |
| 2 | Directive Application & Constraint Correctness | 25 | 10 organizer-ground-truth application + 5 hourly energy balance/effective-solar validity + 5 battery transitions/bounds/rate limits + 5 action consistency/end-of-day neutrality/non-negative values |
| 3 | Optimization Quality | 10 | cost-quality ratio over optimization hidden cases; invalid → 0 |
| 4 | API Contract & Schema | 10 | 2 endpoints/status + 2 request validation + 3 directive_interpretation schema/order/types + 3 hourly_plan/top-level response & scenario_id echo |
| 5 | Performance & Reliability | 10 | 2 health readiness + 3 p95 latency + 3 valid-request stability/failure rate + 2 controlled failure handling & secret safety |
| 6 | Deployment & Docker Fallback | 10 | 3 live endpoint reachability + 4 working pullable Docker fallback reaching /health + 2 clean startup/reproducibility + 1 no judge debugging/manual changes |
| 7 | Documentation & Local Reproducibility | 10 | 3 clean local quickstart + 2 environment/config/model-provider docs + 2 public-sample test procedure + 1 LLM/guardrail/optimizer architecture explanation + 1 Docker pull/run instructions + 1 dependencies/limitations/secret handling |

**Total: 100.**

Key evaluation principles (Guide §06, §07, §09):
- LLM interpretation and downstream application are scored separately; correct extraction without correct scheduling is insufficient.
- Optimization credit is considered only after the case is valid under organizer **ground-truth** directives and normal GridWise rules. The judge replays using the true directive, not the team-reported interpretation.
- Reported totals disagreeing with `hourly_plan`, or repeated critical invalidity, cause recalculation/scoring deduction and may block qualification eligibility.

### 2.16 Hidden-test findings (PS §11.4, §11.5; Guide §10; Samples `_meta`)

- The same underlying directive may appear paraphrased: e.g., `"PV production will drop to about 20% between 13:00 and 15:00"`, `"Panel washing from one until three will leave roughly one-fifth of normal solar output"`, `"Expect an 80% reduction in rooftop solar during the 1-3 PM maintenance window"` — all `solar_reduction`, `hours [13,14]`, `factor 0.2` (PS §11.4).
- Hidden cases vary demand, solar, tariff, battery state, reserve/rate limits, and directive combinations (SAMPLE-07/10 combine reserve + grid cap; SAMPLE-08 has separate charge/discharge windows; SAMPLE-01/06/09 mix a directive + distractor).
- No byte-for-byte matching: equivalent optimal schedules are accepted; judging is based on interpretation ground truth, directive application, validity, and recalculated cost (PS §11.4, §11.5; Samples `equivalence_note`).
- Do not hard-code public note wording, case IDs, numeric values, or reference schedules (Samples `how_to_use`).
- Numeric tolerance: absolute tolerance of **0.01 kWh / 0.01 BDT** unless the official judge package specifies a stricter value (PS §11.5; Guide §08; Samples `constraint_reminders`).
- A `max_grid_window` cap applies only in its listed hours; a higher `peak_grid_kwh` outside the window is legal (SAMPLE-05: cap 155 in hours 18–20, `peak_grid_kwh` = 175 at hour 21).
- Organizer valid scoring scenarios are feasible and do not require mutually contradictory hard directives (PS §5.1, §08, §11.4; Guide §10).

### 2.17 Cross-check of every important rule against the Public Sample Cases

| Rule | Confirmed by |
|---|---|
| One interpretation entry per note, `note_index` order | SAMPLE-01..10 (all) |
| `no_op` → `applies=false`, `structured_adjustment=null`; others `applies=true` | SAMPLE-01, SAMPLE-06, SAMPLE-09, SAMPLE-10 (distractors) |
| Start-inclusive / end-exclusive windows | SAMPLE-01 (12–13), SAMPLE-02 (2–4), SAMPLE-03 (18–20), SAMPLE-04 (18–19), SAMPLE-05 (18–20), SAMPLE-06 (10–11, 14–15), SAMPLE-07 (18–21, 19–20), SAMPLE-08 (11–12, 17–18), SAMPLE-09 (11–13), SAMPLE-10 (18–21, 19–21) |
| `factor` = usable remaining fraction | SAMPLE-01 (0.25), SAMPLE-06 (0.5), SAMPLE-09 (0.2 from 80% reduction) |
| Reserve from capacity percentage | SAMPLE-03 (50% of 200 → 100 kWh) |
| Directive hours unique 0–23 ascending | All cases |
| Exactly 24 hourly_plan entries, hours 0–23 | All cases |
| Energy balance holds each hour | All cases (replay-verified) |
| `solar_used_kwh` ≤ effective solar | All cases (replay-verified) |
| Battery bounds, transitions, rate limits | All cases (replay-verified) |
| `no_charge_window` → charge 0; `no_discharge_window` → discharge 0 | SAMPLE-02, SAMPLE-06, SAMPLE-08 |
| End-of-day neutrality = initial energy | All cases (replay-verified) |
| Totals/peak match `hourly_plan` | All cases (replay-verified) |

---

## 3. Unresolved / Assumed Behavior

The following are **not confirmed by the official documents**. No resolution is invented; each is marked as an explicit assumption to validate against hidden cases or the official judge package.

| # | Item | Status |
|---|---|---|
| 1 | **Optimization-quality zero-cost branch**: Guide §08 states `quality_ratio = 1` when organizer and team costs are both ≈0, but the sentence for "organizer ≈0, team > tolerance" is truncated in the PDF. | Assumption: treat as `quality_ratio = 0`. |
| 2 | **Overnight / wrap-around time windows** (e.g., "10 PM until 2 AM"): mapping to hours `[22,23,0,1]` is not documented; only same-day start<end windows are specified. | Unspecified; do not invent — flag for guardrail policy decision. |
| 3 | **Multiple overlapping directives of the same type** (e.g., two `solar_reduction` notes covering overlapping hours): composition behavior (multiply vs. pick strongest) is undefined. | Unspecified; organizers promise feasible, non-contradictory scoring notes, which reduces exposure. |
| 4 | **Reserve checks**: spec references `battery_energy_after_kwh` only; whether a transient intra-hour dip below the reserve is checked is undocumented. Samples only satisfy the after-hour check. | Assume after-hour check only. |
| 5 | **Extra fields in request/response**: the contract implies exact fields; whether additional fields are tolerated by strict schema checks is unspecified. | Assume exact-field discipline; return only the defined fields. |
| 6 | **400 vs 422 boundary**: 422 is explicitly optional, so the split between structural (400) and semantic (422) rejection is not prescribed. | Assume 400 for malformed JSON / invalid structure; controlled 422 for well-formed but semantically invalid input. |
| 7 | **`/health` extra fields**: shown schema is exactly `{"status":"ok"}`; tolerance for extra fields unspecified. | Assume minimal response only. |
| 8 | **`operator_notes` count enforcement** for 0 or 4+ notes: not explicitly spelled out; request validation must handle it. | Assume invalid request (400/422). |
| 9 | **Minute-level times** (e.g., "until 10:00 PM") and "midnight/midday" literal mappings: only whole-hour convention documented. | Unspecified; rely on LLM + whole-hour convention. |

---

## 4. Implementation Invariants

Rules that no implementation agent may violate. These are binding regardless of any convenience, performance, or simplification trade-off.

1. **No invented behavior.** Do not invent directive types, request/response fields, battery efficiency, rounding rules, or optimization semantics that are not in the Problem Statement. Do not import assumptions from AI-generated plans when they conflict with official documents.
2. **No silent repair of invalid input.** Do not silently clip invalid hours, clamp invalid numeric values, or coerce malformed LLM output. Invalid hours/values are guardrail failures handled in a controlled way.
3. **`no_op` is never a fallback.** Do not treat timeout, malformed JSON, provider/LLM failure, or validation failure as `no_op`. `no_op` is allowed only when a note is genuinely irrelevant to the current 24-hour energy schedule.
4. **LLM must drive interpretation.** The language model must be in the path that produces `directive_interpretation` consumed by the optimizer. Do not use hard-coded public sample phrases as the interpreter, and do not use the LLM only for `plan_summary`.
5. **Deterministic guardrails precede the optimizer.** Every directive passes deterministic validation (allowed types, 1:1 note mapping, hours unique 0–23 ascending, factor 0–1, finite non-negative numeric ranges, applies semantics) before it is applied.
6. **The plan must replay-clean.** The returned `hourly_plan` must satisfy the organizer-ground-truth semantics: exact 24 entries (hours 0–23), energy balance every hour, `solar_used_kwh` ≤ effective solar, battery transitions/bounds/rate limits, directive-specific constraints, and end-of-day neutrality, all within the 0.01 tolerance.
7. **Validity before cost.** Optimization credit presupposes a valid schedule under ground-truth directives and GridWise rules. Minimize `total_cost_bdt = Σ grid_kwh[h] * tariff_bdt_per_kwh[h]` only after correctness.
8. **Exact API contract.** Use the exact endpoint names, exact request/response field names and shapes, echo `scenario_id`, one directive_interpretation entry per note in `note_index` order, `applies` semantics exact, and totals that match recalculation from `hourly_plan`.
9. **Precision discipline.** Preserve numerical precision far beyond the 0.01 kWh / 0.01 BDT tolerance; do not round intermediates to integers.
10. **Controlled failure + secret safety.** Never crash on bad input or provider errors; never expose stack traces, keys, tokens, or raw prompts containing secrets in logs or responses; no secrets in repo/README/Docker image.
11. **Performance bounds.** `POST /optimize-energy` within 30 s (p95 ≤ 5 s preferred); `GET /health` ready within 60 s of start; stable under repeated requests.
12. **Do not modify `docs/`.** Never modify, rename, move, or delete any file inside `docs/` — the official problem statement, participant guide, and sample cases are canonical reference material.
13. **Tolerance semantics.** Judge-side absolute tolerance 0.01 kWh / 0.01 BDT applies to floating-point comparisons; stricter values may be defined by the official judge package.