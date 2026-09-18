"""Runtime, provider-independent LLM interpretation of operator notes.

The language model is the only component that turns natural-language operator
notes into ``directive_interpretation`` entries (Implementation Invariant 4).
There is no regex fallback, no hard-coded phrases, and no Ollama in the runtime
path. ``no_op`` is never produced by a technical failure (Invariant 3).

Design (binding):

- All operator notes are sent in ONE request to the primary provider.
- The output must be strict JSON with exactly one entry per note.
- Every parsed result is passed through the deterministic guardrails
  (:func:`app.guardrails.validate_directive_interpretation`) before it is
  returned to the caller.
- Malformed or guardrail-invalid output triggers at most one schema-guided
  repair call per provider (``LLM_REPAIR_ATTEMPTS``).
- On primary infrastructure failure the secondary provider is tried once.
- Any remaining failure raises the controlled :class:`InterpretationError`,
  which callers must map to a 500 response. Exceptions never carry raw
  prompts, raw responses, API keys, or stack traces (Invariant 10).

Supported providers use their official SDKs:

- ``groq`` -> ``groq`` SDK (OpenAI-compatible chat completions, JSON mode)
- ``gemini`` -> ``google-genai`` SDK (``response_mime_type="application/json"``)

Battery capacity is included in the prompt so the model can convert a
percentage reserve note ("keep at least 50%") into the absolute
``minimum_energy_kwh`` required by the official schema.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from app.config import settings
from app.guardrails import (
    GuardrailValidationError,
    validate_directive_interpretation,
)

__all__ = [
    "InterpretationError",
    "LLMInterpreter",
    "extract_json_payload",
    "build_interpret_prompts",
    "build_repair_prompts",
    "build_interpreter",
]


class InterpretationError(Exception):
    """Controlled failure of the interpretation step.

    Raised when every configured provider fails technically or keeps
    producing guardrail-invalid output after its repair attempt. The message
    is intentionally generic: it never contains prompts, model output, API
    keys, provider internals, or stack traces, so it is safe to surface as an
    HTTP 500 detail.
    """


# --------------------------------------------------------------------------- #
# Prompt construction
# --------------------------------------------------------------------------- #

_ENTRY_SCHEMA = """\
{
  "directive_interpretation": [
    {
      "note_index": 0,
      "applies": true,
      "directive_type": "solar_reduction",
      "structured_adjustment": {"hours": [12, 13], "factor": 0.5},
      "explanation": "Short human-readable justification."
    }
  ]
}"""

_SCHEMA_RULES = """\
Output contract (strict JSON only - no markdown, no prose, no code fences):
Return exactly one JSON object of the shape shown below, with EXACTLY ONE
entry per operator note, ordered by note_index from 0 upward.

Allowed directive_type values and their exact structured_adjustment shapes:
- "solar_reduction":          {"hours": [...], "factor": <number 0..1>}
- "minimum_battery_reserve":  {"hours": [...], "minimum_energy_kwh": <number>}
- "no_charge_window":         {"hours": [...]}
- "no_discharge_window":      {"hours": [...]}
- "max_grid_window":          {"hours": [...], "max_grid_kwh": <number>}
- "no_op":                    structured_adjustment must be null

Rules:
- "hours" is a non-empty list of unique integers 0..23 in ascending order.
- "factor" is the fraction of solar that REMAINS usable (0..1 inclusive).
- "minimum_energy_kwh" and "max_grid_kwh" are absolute kWh, finite, >= 0.
- A note expressed as a percentage of battery capacity must be converted to
  absolute kWh using the battery capacity given below.
- "no_op" is ONLY for a note genuinely irrelevant to the 24-hour schedule;
  it requires "applies": false and "structured_adjustment": null.
- Every applicable directive requires "applies": true and its full
  structured_adjustment object.
- Time ranges are start-inclusive and end-exclusive. "from A until B",
  "from A to B", "between A and B", and equivalent expressions include hour
  A but exclude hour B. An hour is the interval beginning at that hour
  (hour 18 is 6:00 PM-6:59 PM, hour 19 is 7:00 PM-7:59 PM, hour 20 is
  8:00 PM-8:59 PM, hour 21 is 9:00 PM-9:59 PM). Examples: 6 PM until 9 PM
  gives hours [18, 19, 20]; 1 PM to 3 PM gives hours [13, 14]; 11 AM until
  1 PM gives hours [11, 12]. Words such as "through" or "inclusive" do not
  add an extra hour beyond this rule.
- "explanation" is a short non-empty string."""


def build_interpret_prompts(
    operator_notes: Sequence[str],
    battery_capacity_kwh: float,
) -> tuple[str, str]:
    """Build (system, user) prompts for the initial interpretation call.

    All notes are included in the single user prompt; the battery capacity is
    stated explicitly so percentage reserves can be converted to kWh.
    """
    notes_block = "\\n".join(
        f"[note_index={index}] {note}" for index, note in enumerate(operator_notes)
    )
    system = (
        "You are the directive interpreter for the GridWise campus energy "
        "scheduler. You convert campus operator notes into machine-checkable "
        "24-hour directives.\\n\\n"
        f"Battery capacity: {battery_capacity_kwh!r} kWh.\\n\\n"
        f"{_SCHEMA_RULES}\\n\\n"
        f"Required JSON shape example:\\n{_ENTRY_SCHEMA}"
    )
    user = (
        "Interpret ALL of the following operator notes for the same 24-hour "
        f"scenario ({len(operator_notes)} note(s)). Return the strict JSON "
        "object now.\\n\\n"
        f"{notes_block}"
    )
    return system, user


def build_repair_prompts(
    system: str,
    user: str,
    invalid_output: str,
) -> tuple[str, str]:
    """Build (system, user) prompts for the one schema-guided repair call.

    The previously produced (invalid) output is included so the model can
    correct it against the schema. These prompts stay between the service and
    the provider; they are never echoed to clients.
    """
    repair_user = (
        f"{user}\\n\\n"
        "Your previous response was rejected because it violated the output "
        "contract above. Regenerate the complete strict JSON object, exactly "
        "one entry per note, fixing every violation.\\n\\n"
        f"Previous (rejected) output:\\n{invalid_output}"
    )
    return system, repair_user


def extract_json_payload(raw: str) -> Any:
    """Extract and decode the JSON value from a model response.

    Tolerates surrounding prose/markdown fences by taking the outermost JSON
    object or array. Raises ``ValueError`` for anything that is not valid
    JSON; nothing is coerced.
    """
    if not isinstance(raw, str):
        raise ValueError("model response is not text")
    text = raw.strip()
    start = text.find("{")
    alt = text.find("[")
    if alt != -1 and (start == -1 or alt < start):
        start = alt
        end_char = "]"
    else:
        end_char = "}"
    if start == -1:
        raise ValueError("no JSON payload found in model response")
    end = text.rfind(end_char)
    if end <= start:
        raise ValueError("unterminated JSON payload in model response")
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:  # noqa: PERF203
        raise ValueError("model response is not valid JSON") from exc


# --------------------------------------------------------------------------- #
# Interpreter
# --------------------------------------------------------------------------- #


class LLMInterpreter:
    """Provider-independent LLM interpreter with one repair call and failover."""

    def __init__(
        self,
        *,
        primary_provider: str | None = None,
        secondary_provider: str | None = None,
        timeout_seconds: float | None = None,
        repair_attempts: int | None = None,
    ) -> None:
        self.primary_provider = (
            primary_provider or settings.llm_primary_provider
        ).strip().lower()
        self.secondary_provider = (
            secondary_provider or settings.llm_secondary_provider
        ).strip().lower()
        self.timeout_seconds = (
            timeout_seconds
            if timeout_seconds is not None
            else settings.llm_timeout_seconds
        )
        self.repair_attempts = (
            repair_attempts
            if repair_attempts is not None
            else max(0, settings.llm_repair_attempts)
        )

    def interpret(
        self,
        operator_notes: Sequence[str],
        battery_capacity_kwh: float,
    ) -> list[Any]:
        """Interpret all notes in one request and return validated entries.

        Every parsed candidate is checked with the deterministic guardrails.
        Malformed or invalid output gets one schema-guided repair call per
        provider; primary infrastructure failure fails over to the secondary
        provider once. Total failure raises :class:`InterpretationError`.

        The returned list contains the validated raw entries (dicts); callers
        re-run :func:`app.guardrails.validate_directive_interpretation` to
        obtain the final pydantic models.
        """
        notes = list(operator_notes)
        system, user = build_interpret_prompts(notes, battery_capacity_kwh)

        for provider in self._provider_chain():
            # Initial call. A technical failure (transport, auth, timeout,
            # unconfigured credentials) fails over to the next provider.
            try:
                raw = self._call_provider(provider, system, user)
            except Exception:  # noqa: BLE001 - generic technical handling
                continue

            candidate = self._validated_entries(raw, notes, battery_capacity_kwh)
            if candidate is not None:
                return candidate

            # Schema-guided repair for malformed / guardrail-invalid output.
            repaired = self._repair(
                provider, system, user, raw, notes, battery_capacity_kwh
            )
            if repaired is not None:
                return repaired

        raise InterpretationError(
            "LLM interpretation failed after all provider attempts"
        )

    def _provider_chain(self) -> list[str]:
        if (
            not self.secondary_provider
            or self.secondary_provider == self.primary_provider
        ):
            return [self.primary_provider]
        return [self.primary_provider, self.secondary_provider]

    def _validated_entries(
        self,
        raw: str,
        notes: list[str],
        battery_capacity_kwh: float,
    ) -> list[Any] | None:
        """Parse and guardrail-validate a candidate; None when it is invalid."""
        try:
            payload = extract_json_payload(raw)
            entries = self._interpretation_list(payload)
            validate_directive_interpretation(entries, notes, battery_capacity_kwh)
        except (ValueError, TypeError, GuardrailValidationError):
            return None
        return entries

    @staticmethod
    def _interpretation_list(payload: Any) -> list[Any]:
        if isinstance(payload, dict) and "directive_interpretation" in payload:
            return payload["directive_interpretation"]
        if isinstance(payload, list):
            return payload
        raise ValueError("payload is not a directive_interpretation list")

    def _repair(
        self,
        provider: str,
        system: str,
        user: str,
        invalid_output: str,
        notes: list[str],
        battery_capacity_kwh: float,
    ) -> list[Any] | None:
        """Run at most ``repair_attempts`` schema-guided repair calls."""
        repair_system, repair_user = build_repair_prompts(
            system, user, invalid_output
        )
        for _ in range(max(0, self.repair_attempts)):
            try:
                raw = self._call_provider(provider, repair_system, repair_user)
            except Exception:  # noqa: BLE001
                return None
            entries = self._validated_entries(raw, notes, battery_capacity_kwh)
            if entries is not None:
                return entries
        return None

    def _call_provider(self, provider: str, system: str, user: str) -> str:
        """Dispatch one completion call; any failure raises (caught upstream)."""
        if provider == "groq":
            return self._call_groq(system, user)
        if provider == "gemini":
            return self._call_gemini(system, user)
        raise RuntimeError("unsupported provider")

    def _call_groq(self, system: str, user: str) -> str:
        """Primary provider: official Groq SDK, JSON response format."""
        if not settings.groq_api_key or not settings.groq_model:
            raise RuntimeError("groq provider is not configured")
        from groq import Groq  # official SDK, imported lazily

        client = Groq(
            api_key=settings.groq_api_key,
            timeout=self.timeout_seconds,
            max_retries=0,
        )
        completion = client.chat.completions.create(
            model=settings.groq_model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0,
            response_format={"type": "json_object"},
        )
        return completion.choices[0].message.content or ""

    def _call_gemini(self, system: str, user: str) -> str:
        """Secondary provider: official google-genai SDK, JSON mime type."""
        if not settings.gemini_api_key or not settings.gemini_model:
            raise RuntimeError("gemini provider is not configured")
        from google import genai  # official SDK, imported lazily
        from google.genai import types

        client = genai.Client(api_key=settings.gemini_api_key)
        response = client.models.generate_content(
            model=settings.gemini_model,
            contents=f"{system}\\n\\n{user}",
            config=types.GenerateContentConfig(
                temperature=0,
                response_mime_type="application/json",
                http_options=types.HttpOptions(
                    timeout=int(self.timeout_seconds * 1000)
                ),
            ),
        )
        return response.text or ""


def build_interpreter() -> LLMInterpreter:
    """Build an interpreter from the current application settings."""
    return LLMInterpreter()
