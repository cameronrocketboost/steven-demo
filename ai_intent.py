from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Optional

try:
    from openai import OpenAI
except Exception:  # pragma: no cover - optional dependency
    OpenAI = None  # type: ignore[assignment]


@dataclass(frozen=True)
class IntentResult:
    action: str
    confidence: float
    updates: dict[str, Any]
    clarification: Optional[str] = None
    raw_json: Optional[dict[str, Any]] = None


def _truthy_env(name: str) -> bool:
    return str(os.getenv(name, "")).strip().lower() in {"1", "true", "yes", "y", "on"}


def ai_intent_enabled() -> bool:
    """
    Whether GPT intent recognition is enabled.

    This is intentionally env-driven so the demo runs without an API key by default.
    """
    if not _truthy_env("OPENAI_INTENT_ENABLED"):
        return False
    if not str(os.getenv("OPENAI_API_KEY", "")).strip():
        return False
    return OpenAI is not None


def ai_intent_model() -> str:
    return str(os.getenv("OPENAI_INTENT_MODEL", "")).strip() or "gpt-5-mini"


_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def _extract_json_object(text: str) -> Optional[dict[str, Any]]:
    """
    Extract and parse the first JSON object found in a string.
    """
    t = (text or "").strip()
    if not t:
        return None
    try:
        if t.startswith("{") and t.endswith("}"):
            return json.loads(t)
    except Exception:
        pass

    m = _JSON_OBJECT_RE.search(t)
    if not m:
        return None
    candidate = m.group(0)
    try:
        return json.loads(candidate)
    except Exception:
        return None


def _clamp_float(value: Any, lo: float, hi: float) -> float:
    try:
        f = float(value)
    except Exception:
        return lo
    if f != f:  # NaN
        return lo
    return max(lo, min(hi, f))


def _intent_spec_for_step(step_key: str) -> dict[str, Any]:
    """
    Step-specific intent contract: allowed actions + validation hints.
    """
    # NOTE: contracts are intentionally small; we validate critical fields in local code.
    return {
        "built_size": {
            "actions": {
                "set_style_size",
                "set_style",
                "set_size",
                "apply_suggestion",
                "cancel_suggestion",
                "clarify",
                "noop",
            }
        },
        "leg_height": {"actions": {"set_leg_height", "clarify", "noop"}},
        "openings_types": {"actions": {"set_openings_types", "clear_openings_types", "clarify", "noop"}},
        "openings_placement": {
            "actions": {"set_openings_placements", "clear_openings_placements", "bulk_place", "clarify", "noop"}
        },
        "options": {"actions": {"set_options", "clear_options", "clarify", "noop"}},
        "colors": {"actions": {"set_colors", "clarify", "noop"}},
        "notes": {"actions": {"set_notes", "clarify", "noop"}},
        "quote": {"actions": {"noop"}},
        "done": {"actions": {"noop"}},
    }.get(step_key, {"actions": {"noop"}})


_STEP_OUTPUT_CONTRACT: dict[str, dict[str, Any]] = {
    "built_size": {
        "actions": [
            "set_style_size",
            "set_style",
            "set_size",
            "apply_suggestion",
            "cancel_suggestion",
            "clarify",
            "noop",
        ],
        "updates": {
            "demo_style": "One of: 'Regular (Horizontal)', 'A-Frame (Horizontal)', 'A-Frame (Vertical)'",
            "width_ft": "integer feet",
            "length_ft": "integer feet",
        },
        "notes": "If a priced-size suggestion is pending and user intends to accept it, use action=apply_suggestion.",
    },
    "leg_height": {
        "actions": ["set_leg_height", "clarify", "noop"],
        "updates": {"leg_height_ft": "integer feet"},
    },
    "openings_types": {
        "actions": ["set_openings_types", "clear_openings_types", "clarify", "noop"],
        "updates": {
            "walk_in_door_type": "label string from context",
            "walk_in_door_count": "integer",
            "window_size": "label string from context",
            "window_count": "integer",
            "garage_door_type": "'Roll-up' or 'None'",
            "garage_door_size": "size token like '10x8' from context",
            "garage_door_count": "integer",
        },
    },
    "openings_placement": {
        "actions": ["set_openings_placements", "clear_openings_placements", "bulk_place", "clarify", "noop"],
        "updates": {
            "placements": "list of {kind:'door|window|garage', side:'front|back|left|right', offset_ft:int}",
            "bulk": "optional {kind, side, count:int} for phrases like '3 doors on the left'",
        },
    },
    "options": {"actions": ["set_options", "clear_options", "clarify", "noop"], "updates": {"option_codes": "list[str]"}},
    "colors": {
        "actions": ["set_colors", "clarify", "noop"],
        "updates": {"roof_color": "string", "trim_color": "string", "side_color": "string"},
    },
    "notes": {"actions": ["set_notes", "clarify", "noop"], "updates": {"internal_notes": "string"}},
}


def _build_step_prompt(step_key: str, *, user_text: str, context: dict[str, Any]) -> tuple[str, str]:
    """
    Build a strict, step-scoped prompt that returns a JSON command.
    """
    # The caller provides step-specific context (allowed values, current selections).
    system = (
        "You are an intent recognizer for a local carport quoting demo.\n"
        "You MUST output ONLY a single JSON object (no markdown, no commentary).\n"
        "If the user is ambiguous, output action=clarify with a short clarification question.\n"
        "Never invent unavailable option codes/sizes; if unsure, clarify.\n"
    )

    contract = {
        "type": "object",
        "required": ["action", "confidence", "updates"],
        "properties": {
            "action": {"type": "string"},
            "confidence": {"type": "number"},
            "updates": {"type": "object"},
            "clarification": {"type": ["string", "null"]},
        },
        "additionalProperties": True,
    }

    user = (
        f"STEP={step_key}\n"
        f"USER_TEXT={user_text}\n\n"
        "Return JSON with shape:\n"
        f"{json.dumps(contract, indent=2)}\n\n"
        "Step-specific contract:\n"
        f"{json.dumps(_STEP_OUTPUT_CONTRACT.get(step_key, {}), indent=2)}\n\n"
        "Step context (authoritative):\n"
        f"{json.dumps(context, indent=2)}\n\n"
        "Examples:\n"
        "- If user types 'standard x3' on openings_types, set walk_in_door_type to the standard label and walk_in_door_count=3.\n"
        "- If user types '3 doors on the left' on openings_placement, output bulk_place with kind=door side=left count=3.\n"
        "- If user types 'a frame vertical 22 26' on built_size, set_style_size with style='A-Frame (Vertical)' and width_ft=22 length_ft=26.\n"
        "- If user types '/apply' or 'apply' while a suggestion is pending on built_size, output apply_suggestion.\n"
    )
    return system, user


def recognize_step_intent(*, step_key: str, user_text: str, context: dict[str, Any], timeout_s: float = 6.0) -> Optional[IntentResult]:
    """
    Use GPT to recognize user intent for a single wizard step.

    Returns None when AI is disabled or the response is unusable.
    """
    if not ai_intent_enabled():
        return None

    api_key = str(os.getenv("OPENAI_API_KEY", "")).strip()
    if not api_key:
        return None
    if OpenAI is None:
        return None

    model = ai_intent_model()
    system, user = _build_step_prompt(step_key, user_text=user_text, context=context)

    try:
        client = OpenAI(api_key=api_key)
        resp = client.responses.create(
            model=model,
            input=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
    except Exception:
        return None

    text = ""
    try:
        text = (resp.output_text or "").strip()  # type: ignore[attr-defined]
    except Exception:
        try:
            text = str(resp)
        except Exception:
            text = ""

    payload = _extract_json_object(text)
    if not isinstance(payload, dict):
        return None

    action = str(payload.get("action") or "").strip()
    confidence = _clamp_float(payload.get("confidence"), 0.0, 1.0)
    updates = payload.get("updates")
    clarification = payload.get("clarification")
    if not isinstance(updates, dict):
        updates = {}
    if clarification is not None and not isinstance(clarification, str):
        clarification = None

    spec = _intent_spec_for_step(step_key)
    allowed_actions = set(spec.get("actions") or [])
    if action not in allowed_actions:
        return None

    return IntentResult(
        action=action,
        confidence=confidence,
        updates=dict(updates),
        clarification=clarification,
        raw_json=payload,
    )
