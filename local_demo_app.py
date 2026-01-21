from __future__ import annotations

import csv
import difflib
import io
import json
import os
import re
import hmac
import hashlib
import base64
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Mapping, Optional, TypedDict
from urllib import error as urllib_error
from urllib import request as urllib_request

import streamlit as st
import streamlit.components.v1 as components

import ai_intent

from building_views import (
    BuildingColorScheme,
    BuildingOpening,
    BuildingOpeningKind,
    BuildingSide,
    render_building_views_png,
)
from normalized_pricebooks import (
    build_demo_pricebook_r29,
    build_pricebook_from_normalized,
    find_normalized_pricebooks,
    load_normalized_pricebook,
)
from quote_pdf import (
    QuotePdfArtifact,
    QuotePdfLineItem,
    QuotePdfTotals,
    logo_png_bytes_from_svg,
    make_quote_pdf_bytes,
)
from pricing_engine import (
    CarportStyle,
    PriceBook,
    PriceBookError,
    QuoteInput,
    RoofStyle,
    SectionPlacement,
    SelectedOption,
    generate_quote,
)


def _format_usd(amount: int) -> str:
    return f"${amount:,.0f}"


def _read_secret_or_env_str(key: str) -> str:
    """
    Read a configuration value from Streamlit Secrets (preferred) or environment variables.

    Returns a stripped string; returns "" when missing.
    """
    val: object = ""
    try:
        # `st.secrets` is Mapping-like; `.get` is supported in Streamlit.
        val = st.secrets.get(key, "")  # type: ignore[attr-defined]
    except Exception:
        val = ""
    if not val:
        val = os.environ.get(key, "")
    if isinstance(val, str):
        return val.strip()
    return str(val).strip() if val is not None else ""


def _sync_openai_intent_env_from_secrets() -> None:
    """
    Mirror Streamlit secrets into environment variables for the intent recognizer.

    This keeps `ai_intent.py` Streamlit-free while still allowing `.streamlit/secrets.toml`
    to control OpenAI usage.
    """
    api_key = _read_secret_or_env_str("OPENAI_API_KEY")
    if api_key:
        os.environ["OPENAI_API_KEY"] = api_key
    model = _read_secret_or_env_str("OPENAI_INTENT_MODEL")
    if model:
        os.environ["OPENAI_INTENT_MODEL"] = model
    enabled = _read_secret_or_env_str("OPENAI_INTENT_ENABLED")
    if enabled:
        os.environ["OPENAI_INTENT_ENABLED"] = enabled


def _apply_ai_intent_env_from_ui_state() -> None:
    """
    Apply Streamlit UI state to env vars before chat handling runs.
    """
    enabled = bool(st.session_state.get("ai_intent_enabled_ui", False))
    os.environ["OPENAI_INTENT_ENABLED"] = "true" if enabled else "false"
    model = str(st.session_state.get("ai_intent_model_ui") or "").strip()
    if model:
        os.environ["OPENAI_INTENT_MODEL"] = model


def _sha256_hex(text: str) -> str:
    if not isinstance(text, str):
        raise TypeError("text must be str")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _password_gate() -> None:
    """
    Optional in-app password gate for hosted demos.

    Enable by setting ONE of:
    - APP_PASSWORD (plain text), or
    - APP_PASSWORD_SHA256 (hex sha256 of the password)

    If neither is set, the app runs without a gate.
    """
    expected_password = _read_secret_or_env_str("APP_PASSWORD")
    expected_sha = _read_secret_or_env_str("APP_PASSWORD_SHA256").lower()
    gate_enabled = bool(expected_password) or bool(expected_sha)
    if not gate_enabled:
        return

    if bool(st.session_state.get("_auth_ok", False)):
        if st.sidebar.button("Log out", key="auth_logout", use_container_width=True):
            st.session_state["_auth_ok"] = False
            st.rerun()
        return

    # Branding even before login.
    _render_logo(where="sidebar")

    st.markdown("## Login")
    st.caption("Enter the password to access this demo.")

    with st.form("auth_form", clear_on_submit=False):
        pw = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Sign in", use_container_width=True)

    if submitted:
        pw = str(pw or "")
        ok = False
        if expected_sha:
            ok = hmac.compare_digest(_sha256_hex(pw), expected_sha)
        elif expected_password:
            ok = hmac.compare_digest(pw, expected_password)

        if ok:
            st.session_state["_auth_ok"] = True
            st.rerun()
        st.error("Incorrect password.")

    st.stop()


@st.cache_data(show_spinner=False)
def _cached_building_isometric_png(
    *,
    width_ft: int,
    length_ft: int,
    height_ft: int,
    roof_color: str,
    trim_color: str,
    side_color: str,
    openings: tuple[BuildingOpening, ...],
) -> bytes:
    views = render_building_views_png(
        width_ft=width_ft,
        length_ft=length_ft,
        height_ft=height_ft,
        colors=BuildingColorScheme(roof=roof_color, trim=trim_color, sides=side_color),
        openings=openings,
        view_names=("isometric",),
        canvas_px=(900, 520),
    )
    return views["isometric"]


@st.cache_data(show_spinner=False)
def _cached_building_views_png(
    *,
    width_ft: int,
    length_ft: int,
    height_ft: int,
    roof_color: str,
    trim_color: str,
    side_color: str,
    openings: tuple[BuildingOpening, ...],
) -> dict[str, bytes]:
    return render_building_views_png(
        width_ft=width_ft,
        length_ft=length_ft,
        height_ft=height_ft,
        colors=BuildingColorScheme(roof=roof_color, trim=trim_color, sides=side_color),
        openings=openings,
        view_names=("isometric", "front", "back", "left", "right"),
        canvas_px=(900, 520),
    )


def _preview_openings_from_state() -> tuple[BuildingOpening, ...]:
    """
    Map the current UI state into drawable doors/windows.

    If explicit openings are configured (wall + offset), we use those.
    Otherwise, we fall back to the legacy qty-based heuristics.
    """
    return _preview_openings_from_mapping(st.session_state)


def _preview_openings_from_mapping(state: Mapping[str, object]) -> tuple[BuildingOpening, ...]:
    """
    Map a state mapping into drawable doors/windows.

    This is used for quote/PDF previews, so we can render using shadow-protected
    effective state instead of relying solely on `st.session_state`.
    """
    explicit = state.get("openings")
    if isinstance(explicit, list) and explicit:
        return _openings_to_building_openings(explicit, state=state)

    openings: list[BuildingOpening] = []

    # Garage doors (default to FRONT). Roll-up sizes are in feet like "10x8", "10x10".
    gd_count = int(state.get("garage_door_count") or 0)
    gd_kind = str(state.get("garage_door_type") or "None")
    gd_size = str(state.get("garage_door_size") or "")
    if gd_count > 0 and gd_kind != "None":
        w_ft, h_ft = 10, 8
        m = re.match(r"^\s*(\d+)\s*x\s*(\d+)\s*$", gd_size)
        if m:
            w_ft, h_ft = int(m.group(1)), int(m.group(2))
        for _ in range(min(4, gd_count)):
            openings.append(
                BuildingOpening(
                    side=BuildingSide.FRONT,
                    kind=BuildingOpeningKind.GARAGE_DOOR if gd_kind == "Roll-up" else BuildingOpeningKind.DOOR,
                    width_ft=w_ft,
                    height_ft=h_ft,
                )
            )

    # Walk-in doors: auto distribute across FRONT then RIGHT then LEFT then BACK.
    wid_count = int(state.get("walk_in_door_count") or 0)
    for idx in range(min(8, wid_count)):
        side = [BuildingSide.FRONT, BuildingSide.RIGHT, BuildingSide.LEFT, BuildingSide.BACK][idx % 4]
        openings.append(BuildingOpening(side=side, kind=BuildingOpeningKind.DOOR, width_ft=3, height_ft=7))

    # Windows: default to RIGHT; if many, spill to LEFT.
    win_count = int(state.get("window_count") or 0)
    win_label = str(state.get("window_size") or "")
    ww_ft, wh_ft = 2, 3
    m = re.match(r"^\s*(\d+)\s*x\s*(\d+)\s*$", win_label)
    if m:
        ww_ft, wh_ft = max(1, int(m.group(1)) // 12), max(1, int(m.group(2)) // 12)
    # If label is "24x36" (inches), convert to feet.
    m2 = re.match(r"^\s*(\d{2})\s*x\s*(\d{2})\s*$", win_label)
    if m2:
        ww_ft, wh_ft = max(1, int(m2.group(1)) // 12), max(1, int(m2.group(2)) // 12)

    for idx in range(min(12, win_count)):
        side = BuildingSide.RIGHT if idx < 4 else BuildingSide.LEFT
        openings.append(BuildingOpening(side=side, kind=BuildingOpeningKind.WINDOW, width_ft=ww_ft, height_ft=wh_ft))

    return tuple(openings)


def _openings_to_building_openings(
    openings_state: list[object], *, state: Optional[Mapping[str, object]] = None
) -> tuple[BuildingOpening, ...]:
    """
    Convert the persisted openings state into `BuildingOpening` for drawing.
    """
    out: list[BuildingOpening] = []

    # Current size selections drive actual drawn sizes (v1).
    s: Mapping[str, object] = state if state is not None else st.session_state
    win_label = str(s.get("window_size") or "")
    ww_ft, wh_ft = _parse_window_size_ft(win_label)

    gd_kind = str(s.get("garage_door_type") or "None")
    gd_size = str(s.get("garage_door_size") or "")
    g_w_ft, g_h_ft = _parse_garage_size_ft(gd_size)

    for row in openings_state:
        if not isinstance(row, dict):
            continue
        kind = str(row.get("kind") or "").strip().lower()
        side = str(row.get("side") or "front").strip().lower()
        offset = row.get("offset_ft")
        offset_ft = int(offset) if isinstance(offset, (int, float)) else None

        try:
            side_enum = BuildingSide(side)
        except Exception:
            side_enum = BuildingSide.FRONT

        if kind == "window":
            out.append(
                BuildingOpening(
                    side=side_enum,
                    kind=BuildingOpeningKind.WINDOW,
                    width_ft=ww_ft,
                    height_ft=wh_ft,
                    offset_ft=offset_ft,
                )
            )
        elif kind == "garage":
            if gd_kind == "None":
                continue
            out.append(
                BuildingOpening(
                    side=side_enum,
                    kind=BuildingOpeningKind.GARAGE_DOOR if gd_kind == "Roll-up" else BuildingOpeningKind.DOOR,
                    width_ft=g_w_ft,
                    height_ft=g_h_ft,
                    offset_ft=offset_ft,
                )
            )
        else:
            # walk-in door
            out.append(
                BuildingOpening(
                    side=side_enum,
                    kind=BuildingOpeningKind.DOOR,
                    width_ft=3,
                    height_ft=7,
                    offset_ft=offset_ft,
                )
            )

    return tuple(out)


def _parse_window_size_ft(label: str) -> tuple[int, int]:
    """
    Parse window size label like "24x36" (inches) into (ft, ft) for drawing.
    """
    t = (label or "").strip()
    m = re.match(r"^\s*(\d+)\s*x\s*(\d+)\s*$", t)
    if not m:
        return (2, 3)
    w_in = int(m.group(1))
    h_in = int(m.group(2))
    return (max(1, w_in // 12), max(1, h_in // 12))


def _parse_garage_size_ft(label: str) -> tuple[int, int]:
    t = (label or "").strip()
    m = re.match(r"^\s*(\d+)\s*x\s*(\d+)\s*$", t)
    if not m:
        return (10, 8)
    return (int(m.group(1)), int(m.group(2)))


@st.cache_data(show_spinner=False)
def _cached_logo_png_bytes() -> Optional[bytes]:
    return logo_png_bytes_from_svg(_LOGO_SVG_PATH)


_LOGO_SVG_PATH = Path(__file__).resolve().parent / "assets" / "coast to coast image.svg"


def _svg_data_uri(path: Path) -> Optional[str]:
    """
    Return a data URI for an SVG file so it can be rendered via <img> in Streamlit.
    """
    try:
        svg = path.read_text(encoding="utf-8")
    except Exception:
        return None
    b64 = base64.b64encode(svg.encode("utf-8")).decode("ascii")
    return f"data:image/svg+xml;base64,{b64}"


def _render_logo(*, where: Literal["sidebar", "main"]) -> None:
    uri = _svg_data_uri(_LOGO_SVG_PATH)
    if not uri:
        return
    html = (
        '<div style="text-align:center; padding: 0.25rem 0 0.75rem 0;">'
        f'<img src="{uri}" alt="Coast to Coast" style="max-width: 100%; height: auto;" />'
        "</div>"
    )
    if where == "sidebar":
        st.sidebar.markdown(html, unsafe_allow_html=True)
    else:
        st.markdown(html, unsafe_allow_html=True)


# region lead capture + chat
class ChatMessage(TypedDict, total=False):
    role: Literal["assistant", "user"]
    content: str
    tag: str
    created_at_ms: int
    visible_at_ms: int


_CHAT_ASSISTANT_MESSAGE_DELAY_MS = 1000


def _init_lead_state() -> None:
    if "lead_name" not in st.session_state:
        st.session_state["lead_name"] = ""
    if "lead_email" not in st.session_state:
        st.session_state["lead_email"] = ""
    if "lead_captured" not in st.session_state:
        st.session_state["lead_captured"] = False
    if "lead_saved_quote_id" not in st.session_state:
        st.session_state["lead_saved_quote_id"] = None
    if "chat_messages" not in st.session_state:
        st.session_state["chat_messages"] = []
    if "chat_last_scrolled_at_ms" not in st.session_state:
        st.session_state["chat_last_scrolled_at_ms"] = 0
    if "chat_last_visible_at_ms" not in st.session_state:
        st.session_state["chat_last_visible_at_ms"] = 0
    if "chat_last_prompted_step" not in st.session_state:
        st.session_state["chat_last_prompted_step"] = None
    if "chat_prompt_seq" not in st.session_state:
        st.session_state["chat_prompt_seq"] = 1
    if "chat_built_size_has_style" not in st.session_state:
        st.session_state["chat_built_size_has_style"] = False
    if "chat_built_size_has_dims" not in st.session_state:
        st.session_state["chat_built_size_has_dims"] = False
    if "_lead_shadow" not in st.session_state:
        st.session_state["_lead_shadow"] = {"name": "", "email": "", "captured": False}
    if "ai_intent_enabled_ui" not in st.session_state:
        st.session_state["ai_intent_enabled_ui"] = _truthy_str(_read_secret_or_env_str("OPENAI_INTENT_ENABLED"))
    if "ai_intent_model_ui" not in st.session_state:
        st.session_state["ai_intent_model_ui"] = _read_secret_or_env_str("OPENAI_INTENT_MODEL") or "gpt-5-mini"


def _sync_lead_shadow() -> None:
    """
    Keep lead fields stable across Streamlit reruns.

    Streamlit may drop/reset widget-backed session_state keys when the widget isn't rendered.
    In the Chat flow, the lead capture form is no longer rendered once `lead_captured=True`,
    so we persist name/email in a separate shadow dict and restore if they go missing.
    """
    shadow_obj = st.session_state.get("_lead_shadow")
    if not isinstance(shadow_obj, dict):
        shadow_obj = {"name": "", "email": "", "captured": False}

    live_name = str(st.session_state.get("lead_name") or "").strip()
    live_email = str(st.session_state.get("lead_email") or "").strip()
    live_captured = bool(st.session_state.get("lead_captured"))

    shadow_name = str(shadow_obj.get("name") or "").strip()
    shadow_email = str(shadow_obj.get("email") or "").strip()
    shadow_captured = bool(shadow_obj.get("captured"))

    restored = False
    if live_captured and (not live_name or not live_email):
        # Restore if we have a better shadow value.
        if shadow_name and not live_name:
            st.session_state["lead_name"] = shadow_name
            restored = True
        if shadow_email and not live_email:
            st.session_state["lead_email"] = shadow_email
            restored = True

    # Update shadow from live whenever live is non-empty.
    new_shadow = dict(shadow_obj)
    if live_name:
        new_shadow["name"] = live_name
    if live_email:
        new_shadow["email"] = live_email
    new_shadow["captured"] = bool(live_captured or shadow_captured)
    st.session_state["_lead_shadow"] = new_shadow


_EMAIL_RE = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")


def _truthy_str(value: str) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _normalize_email(value: str) -> str:
    return value.strip().lower()


def _extract_email(text: str) -> Optional[str]:
    m = _EMAIL_RE.search(text or "")
    if not m:
        return None
    return _normalize_email(m.group(0))


def _lead_is_valid(*, name: str, email: str) -> bool:
    if not name.strip():
        return False
    return _extract_email(email) is not None


def _chat_messages() -> list[ChatMessage]:
    raw = st.session_state.get("chat_messages")
    if isinstance(raw, list):
        # best-effort runtime check
        out: list[ChatMessage] = []
        for item in raw:
            if (
                isinstance(item, dict)
                and item.get("role") in ("assistant", "user")
                and isinstance(item.get("content"), str)
            ):
                created_at_ms = int(item.get("created_at_ms") or 0)
                out.append(
                    {
                        "role": item["role"],  # type: ignore[index]
                        "content": item["content"],  # type: ignore[index]
                        "tag": item.get("tag"),
                        "created_at_ms": created_at_ms,
                        "visible_at_ms": int(item.get("visible_at_ms") or created_at_ms),
                    }
                )
        return out
    return []


def _chat_add(*, role: Literal["assistant", "user"], content: str, tag: Optional[str] = None) -> None:
    messages = _chat_messages()
    clean = (content or "").strip()
    if not clean:
        return
    if tag:
        # Make tagged messages idempotent across the whole chat history so reruns don't
        # spam the same prompt repeatedly.
        for m in messages:
            if m.get("role") == role and m.get("tag") == tag:
                return
    now_ms = int(time.time() * 1000)
    visible_at_ms = now_ms
    if role == "assistant":
        try:
            last_visible_at_ms = int(st.session_state.get("chat_last_visible_at_ms") or 0)
        except Exception:
            last_visible_at_ms = 0
        visible_at_ms = max(now_ms, last_visible_at_ms + _CHAT_ASSISTANT_MESSAGE_DELAY_MS)
        st.session_state["chat_last_visible_at_ms"] = visible_at_ms
    msg: ChatMessage = {
        "role": role,
        "content": clean,
        "created_at_ms": now_ms,
        "visible_at_ms": visible_at_ms,
    }
    if tag:
        msg["tag"] = tag
    messages.append(msg)
    st.session_state["chat_messages"] = messages
    st.session_state["chat_last_message_at_ms"] = msg["created_at_ms"]


def _parse_dimensions_ft(text: str) -> Optional[tuple[int, int]]:
    """
    Parse a width x length input like '12x21', '12 x 21', '12 by 21'.
    Returns (width_ft, length_ft) when plausible, else None.
    """
    t = (text or "").lower().strip()
    # Allow common separators and unit markers (ft / feet / ').
    # Examples: "12x21", "12 x 21", "12×21", "12 by 21", "12' x 21'".
    m = re.search(
        r"\b(\d{1,3})\s*(?:ft|feet|foot|['’′])?\s*(?:x|×|by)\s*(\d{1,3})\s*(?:ft|feet|foot|['’′])?(?!\d)",
        t,
    )
    if not m:
        # Loose fallback for inputs like "22 26" on the Built & Size step.
        nums = re.findall(r"\b\d{1,3}\b", t)
        if len(nums) != 2:
            return None
        try:
            w = int(nums[0])
            l = int(nums[1])
        except Exception:
            return None
        if w <= 0 or l <= 0 or w > 60 or l > 200:
            return None
        return (w, l)
    w = int(m.group(1))
    l = int(m.group(2))
    # Guardrails: this demo pricebook is in feet and typical sizes are not thousands.
    if w <= 0 or l <= 0 or w > 60 or l > 200:
        return None
    return (w, l)


def _parse_leg_height_ft(text: str) -> Optional[int]:
    t = (text or "").lower().strip()
    m = re.search(r"\b(\d{1,2})\s*(ft|')?\b", t)
    if not m:
        return None
    h = int(m.group(1))
    if h <= 0 or h > 30:
        return None
    return h


def _parse_style_label(text: str) -> Optional[str]:
    t = (text or "").lower().strip()
    if not t:
        return None

    tokens = re.findall(r"[a-z]+", t)

    def _token_close_to(target: str, *, cutoff: float) -> bool:
        for tok in tokens:
            if tok == target:
                return True
            if difflib.SequenceMatcher(a=tok, b=target).ratio() >= cutoff:
                return True
        return False

    if "regular" in t or "standard" in t or _token_close_to("regular", cutoff=0.78) or "reg" in tokens:
        return "Regular (Horizontal)"

    has_aframe = ("a-frame" in t) or ("aframe" in t) or ("a" in tokens and "frame" in tokens) or _token_close_to("aframe", cutoff=0.82)
    wants_vertical = ("vertical" in t) or ("vert" in tokens) or _token_close_to("vertical", cutoff=0.82)
    wants_horizontal = ("horizontal" in t) or ("horiz" in tokens) or _token_close_to("horizontal", cutoff=0.82)

    if has_aframe:
        return "A-Frame (Vertical)" if wants_vertical else "A-Frame (Horizontal)"
    if wants_vertical:
        return "A-Frame (Vertical)"
    if wants_horizontal:
        return "A-Frame (Horizontal)"
    return None


def _ensure_chat_quick_pick_state(*, book: PriceBook) -> None:
    """
    Tabs render in the same run, so Conversation widgets must NOT reuse the wizard form keys.
    We keep separate 'chat_*' widget state and sync into the real wizard state on confirm.
    """
    if "chat_demo_style" not in st.session_state:
        st.session_state["chat_demo_style"] = str(st.session_state.get("demo_style") or "A-Frame (Horizontal)")
    if "chat_width_ft" not in st.session_state:
        st.session_state["chat_width_ft"] = int(
            st.session_state.get("width_ft") or (book.allowed_widths_ft[0] if book.allowed_widths_ft else 12)
        )
    if "chat_length_ft" not in st.session_state:
        st.session_state["chat_length_ft"] = int(st.session_state.get("length_ft") or 21)


def _ensure_chat_action_state(*, book: PriceBook) -> None:
    """
    Initialize the Conversation-tab Action Card state (chat_action_*) from current wizard state.

    Important: Tabs render in the same run, so we must NOT reuse the wizard keys directly.
    """
    if "chat_action_demo_style" not in st.session_state:
        st.session_state["chat_action_demo_style"] = str(st.session_state.get("demo_style") or "A-Frame (Horizontal)")
    if "chat_action_width_ft" not in st.session_state:
        st.session_state["chat_action_width_ft"] = int(
            st.session_state.get("width_ft") or (book.allowed_widths_ft[0] if book.allowed_widths_ft else 12)
        )
    if "chat_action_length_ft" not in st.session_state:
        st.session_state["chat_action_length_ft"] = int(st.session_state.get("length_ft") or 21)

    if "chat_action_leg_height_ft" not in st.session_state:
        st.session_state["chat_action_leg_height_ft"] = int(
            st.session_state.get("leg_height_ft") or (book.allowed_leg_heights_ft[0] if book.allowed_leg_heights_ft else 6)
        )

    if "chat_action_walk_in_door_type" not in st.session_state:
        st.session_state["chat_action_walk_in_door_type"] = str(st.session_state.get("walk_in_door_type") or "None")
    if "chat_action_walk_in_door_count" not in st.session_state:
        st.session_state["chat_action_walk_in_door_count"] = int(st.session_state.get("walk_in_door_count") or 0)
    if "chat_action_window_size" not in st.session_state:
        st.session_state["chat_action_window_size"] = str(st.session_state.get("window_size") or "None")
    if "chat_action_window_count" not in st.session_state:
        st.session_state["chat_action_window_count"] = int(st.session_state.get("window_count") or 0)
    if "chat_action_garage_door_type" not in st.session_state:
        st.session_state["chat_action_garage_door_type"] = str(st.session_state.get("garage_door_type") or "None")
    if "chat_action_garage_door_size" not in st.session_state:
        st.session_state["chat_action_garage_door_size"] = str(st.session_state.get("garage_door_size") or "10x8")
    if "chat_action_garage_door_count" not in st.session_state:
        st.session_state["chat_action_garage_door_count"] = int(st.session_state.get("garage_door_count") or 0)
    if "chat_action_openings" not in st.session_state or not isinstance(st.session_state.get("chat_action_openings"), list):
        live_openings = st.session_state.get("openings")
        st.session_state["chat_action_openings"] = list(live_openings) if isinstance(live_openings, list) else []
    if "chat_action_opening_seq" not in st.session_state:
        st.session_state["chat_action_opening_seq"] = int(st.session_state.get("opening_seq") or 1)

    if "chat_action_include_ground_certification" not in st.session_state:
        st.session_state["chat_action_include_ground_certification"] = bool(
            st.session_state.get("include_ground_certification") or False
        )
    if "chat_action_selected_option_codes" not in st.session_state:
        live_codes = st.session_state.get("selected_option_codes")
        st.session_state["chat_action_selected_option_codes"] = list(live_codes) if isinstance(live_codes, list) else []
    if "chat_action_extra_panel_count" not in st.session_state:
        st.session_state["chat_action_extra_panel_count"] = int(st.session_state.get("extra_panel_count") or 0)

    if "chat_action_roof_color" not in st.session_state:
        st.session_state["chat_action_roof_color"] = str(st.session_state.get("roof_color") or "White")
    if "chat_action_trim_color" not in st.session_state:
        st.session_state["chat_action_trim_color"] = str(st.session_state.get("trim_color") or "White")
    if "chat_action_side_color" not in st.session_state:
        st.session_state["chat_action_side_color"] = str(st.session_state.get("side_color") or "White")

    if "chat_action_internal_notes" not in st.session_state:
        st.session_state["chat_action_internal_notes"] = str(st.session_state.get("internal_notes") or "")

    # Streamlit selectbox widgets will raise if the current session value is not in `options`.
    # This can happen across deploys (stale session_state), or if a key is reset to 0/None.
    style_labels = ["Regular (Horizontal)", "A-Frame (Horizontal)", "A-Frame (Vertical)"]
    default_style = "A-Frame (Horizontal)" if "A-Frame (Horizontal)" in style_labels else style_labels[0]
    if str(st.session_state.get("chat_action_demo_style") or "") not in style_labels:
        st.session_state["chat_action_demo_style"] = default_style

    allowed_widths = list(book.allowed_widths_ft)
    if allowed_widths:
        raw_width = st.session_state.get("chat_action_width_ft")
        coerced_width: Optional[int] = None
        try:
            if isinstance(raw_width, bool):
                coerced_width = None
            elif isinstance(raw_width, int):
                coerced_width = raw_width
            elif isinstance(raw_width, float) and raw_width.is_integer():
                coerced_width = int(raw_width)
            elif isinstance(raw_width, str):
                s = raw_width.strip()
                if s.isdigit():
                    coerced_width = int(s)
        except Exception:
            coerced_width = None

        if coerced_width in allowed_widths:
            st.session_state["chat_action_width_ft"] = int(coerced_width)
        else:
            st.session_state["chat_action_width_ft"] = allowed_widths[0]


def _sync_chat_action_from_wizard(*, book: PriceBook, step_key: str) -> None:
    """
    Keep the Action Card controls aligned with current wizard state for the active step.
    """
    _ensure_chat_action_state(book=book)
    if step_key == "built_size":
        st.session_state["chat_action_demo_style"] = str(st.session_state.get("demo_style") or st.session_state.get("chat_action_demo_style"))
        st.session_state["chat_action_width_ft"] = int(st.session_state.get("width_ft") or st.session_state.get("chat_action_width_ft") or 0)
        st.session_state["chat_action_length_ft"] = int(st.session_state.get("length_ft") or st.session_state.get("chat_action_length_ft") or 0)
    elif step_key == "leg_height":
        st.session_state["chat_action_leg_height_ft"] = int(
            st.session_state.get("leg_height_ft") or st.session_state.get("chat_action_leg_height_ft") or 0
        )
    elif step_key == "openings_types":
        st.session_state["chat_action_walk_in_door_type"] = str(
            st.session_state.get("walk_in_door_type") or st.session_state.get("chat_action_walk_in_door_type")
        )
        st.session_state["chat_action_walk_in_door_count"] = int(
            st.session_state.get("walk_in_door_count") or st.session_state.get("chat_action_walk_in_door_count") or 0
        )
        st.session_state["chat_action_window_size"] = str(st.session_state.get("window_size") or st.session_state.get("chat_action_window_size"))
        st.session_state["chat_action_window_count"] = int(
            st.session_state.get("window_count") or st.session_state.get("chat_action_window_count") or 0
        )
        st.session_state["chat_action_garage_door_type"] = str(
            st.session_state.get("garage_door_type") or st.session_state.get("chat_action_garage_door_type")
        )
        st.session_state["chat_action_garage_door_size"] = str(
            st.session_state.get("garage_door_size") or st.session_state.get("chat_action_garage_door_size")
        )
        st.session_state["chat_action_garage_door_count"] = int(
            st.session_state.get("garage_door_count") or st.session_state.get("chat_action_garage_door_count") or 0
        )
    elif step_key == "openings_placement":
        live_openings = st.session_state.get("openings")
        if isinstance(live_openings, list):
            st.session_state["chat_action_openings"] = list(live_openings)
        st.session_state["chat_action_opening_seq"] = int(st.session_state.get("opening_seq") or st.session_state.get("chat_action_opening_seq") or 1)
    elif step_key == "options":
        st.session_state["chat_action_include_ground_certification"] = bool(st.session_state.get("include_ground_certification") or False)
        live_codes = st.session_state.get("selected_option_codes")
        if isinstance(live_codes, list):
            st.session_state["chat_action_selected_option_codes"] = list(live_codes)
        st.session_state["chat_action_extra_panel_count"] = int(st.session_state.get("extra_panel_count") or 0)
        # placement keys are handled via per-option widgets and copied on apply.
    elif step_key == "colors":
        st.session_state["chat_action_roof_color"] = str(st.session_state.get("roof_color") or st.session_state.get("chat_action_roof_color"))
        st.session_state["chat_action_trim_color"] = str(st.session_state.get("trim_color") or st.session_state.get("chat_action_trim_color"))
        st.session_state["chat_action_side_color"] = str(st.session_state.get("side_color") or st.session_state.get("chat_action_side_color"))
    elif step_key == "notes":
        st.session_state["chat_action_internal_notes"] = str(st.session_state.get("internal_notes") or st.session_state.get("chat_action_internal_notes"))


def _maybe_sync_chat_action_for_step(*, book: PriceBook, step_key: str) -> None:
    """
    Sync wizard -> chat_action values *only* when entering a step (or when explicitly requested).

    We do NOT continuously sync on every rerun, otherwise draft selections in the Conversation tab
    get overwritten by the wizard's canonical values before the user clicks "Apply & continue".
    """
    _ensure_chat_action_state(book=book)

    last = st.session_state.get("chat_action_last_synced_step")
    if last == step_key:
        return

    is_dirty = bool(st.session_state.get(f"chat_action_dirty_{step_key}", False))
    if not is_dirty:
        _sync_chat_action_from_wizard(book=book, step_key=step_key)

    st.session_state["chat_action_last_synced_step"] = step_key


def _apply_chat_action_to_wizard(*, step_key: str) -> None:
    """
    Copy the current Action Card selections into the wizard's canonical state keys.
    """
    if step_key == "built_size":
        st.session_state["demo_style"] = str(st.session_state.get("chat_action_demo_style") or st.session_state.get("demo_style"))
        st.session_state["width_ft"] = int(st.session_state.get("chat_action_width_ft") or st.session_state.get("width_ft") or 0)
        st.session_state["length_ft"] = int(st.session_state.get("chat_action_length_ft") or st.session_state.get("length_ft") or 0)
        # Keep style-prev consistent with current style to avoid length mapping surprises.
        st.session_state["demo_style_prev"] = str(st.session_state.get("demo_style") or "")
    elif step_key == "leg_height":
        st.session_state["leg_height_ft"] = int(st.session_state.get("chat_action_leg_height_ft") or st.session_state.get("leg_height_ft") or 0)
    elif step_key == "openings_types":
        st.session_state["walk_in_door_type"] = str(st.session_state.get("chat_action_walk_in_door_type") or "None")
        st.session_state["walk_in_door_count"] = int(st.session_state.get("chat_action_walk_in_door_count") or 0)
        if str(st.session_state.get("walk_in_door_type") or "None") == "None":
            st.session_state["walk_in_door_count"] = 0

        st.session_state["window_size"] = str(st.session_state.get("chat_action_window_size") or "None")
        st.session_state["window_count"] = int(st.session_state.get("chat_action_window_count") or 0)
        if str(st.session_state.get("window_size") or "None") == "None":
            st.session_state["window_count"] = 0

        st.session_state["garage_door_type"] = str(st.session_state.get("chat_action_garage_door_type") or "None")
        st.session_state["garage_door_size"] = str(st.session_state.get("chat_action_garage_door_size") or "10x8")
        st.session_state["garage_door_count"] = int(st.session_state.get("chat_action_garage_door_count") or 0)
        if str(st.session_state.get("garage_door_type") or "None") == "None":
            st.session_state["garage_door_count"] = 0

        # When the user edits qty/type mode, clear advanced placement so the preview matches.
        st.session_state["openings"] = []
        st.session_state["opening_seq"] = int(st.session_state.get("opening_seq") or 1)

    elif step_key == "openings_placement":
        openings = st.session_state.get("chat_action_openings")
        if isinstance(openings, list):
            st.session_state["openings"] = list(openings)
        st.session_state["opening_seq"] = int(st.session_state.get("chat_action_opening_seq") or st.session_state.get("opening_seq") or 1)
    elif step_key == "options":
        st.session_state["include_ground_certification"] = bool(st.session_state.get("chat_action_include_ground_certification") or False)
        codes = st.session_state.get("chat_action_selected_option_codes")
        st.session_state["selected_option_codes"] = list(codes) if isinstance(codes, list) else []
        st.session_state["extra_panel_count"] = int(st.session_state.get("chat_action_extra_panel_count") or 0)
        # Copy per-option placements when present.
        selected_codes = st.session_state.get("selected_option_codes")
        if isinstance(selected_codes, list):
            for code in selected_codes:
                if isinstance(code, str) and code:
                    chat_key = f"chat_action_placement_{code}"
                    if chat_key in st.session_state:
                        st.session_state[f"placement_{code}"] = st.session_state.get(chat_key)
    elif step_key == "colors":
        st.session_state["roof_color"] = str(st.session_state.get("chat_action_roof_color") or "White")
        st.session_state["trim_color"] = str(st.session_state.get("chat_action_trim_color") or "White")
        st.session_state["side_color"] = str(st.session_state.get("chat_action_side_color") or "White")
    elif step_key == "notes":
        st.session_state["internal_notes"] = str(st.session_state.get("chat_action_internal_notes") or "")


def _maybe_autoscroll_chat() -> None:
    """
    Streamlit's chat UI doesn't always keep the newest messages in view, especially once the
    page grows and the user switches tabs. This injects a small JS snippet that scrolls the
    main page container to the bottom when new chat messages arrive.
    """
    messages = _chat_messages()
    if not messages:
        return

    latest_ms = int(messages[-1].get("created_at_ms") or 0)
    last_scrolled_ms = int(st.session_state.get("chat_last_scrolled_at_ms") or 0)
    if latest_ms <= last_scrolled_ms:
        return

    components.html(
        """
<script>
(() => {
  try {
    const doc = window.parent.document;
    window.requestAnimationFrame(() => {
      // Scroll the newest message into view. If it's inside a scrollable Streamlit container,
      // this will scroll that container rather than pushing the whole page down.
      const anchor = doc.getElementById("chat-scroll-anchor");
      if (anchor) {
        try { anchor.scrollIntoView({ behavior: "smooth", block: "end" }); } catch (e) {}
      }
    });
  } catch (e) {}
})();
</script>
""",
        height=0,
    )
    st.session_state["chat_last_scrolled_at_ms"] = latest_ms


def _quote_input_signature(book: PriceBook, quote) -> str:
    """
    Build a deterministic signature for the current lead+quote snapshot so we can avoid
    auto-saving duplicates across reruns.
    """
    payload = _quote_export_payload(book, quote)
    base = json.dumps(payload.get("input", {}), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(base.encode("utf-8")).hexdigest()[:12]


def _chat_input_placeholder(step_key: str) -> str:
    """
    Step-specific chat input placeholder to reduce confusion about typing vs clicking.
    """
    openings_stage = _openings_types_stage()
    options_stage = _options_stage()
    openings_placeholder = (
        "Type door selection (example: “standard door x2”), or “/hint”…"
        if openings_stage == "doors"
        else "Type window selection (example: “add 2 windows 24x36”), or “/hint”…"
        if openings_stage == "windows"
        else "Type garage door selection (example: “2 roll-up 10x8”), or “/hint”…"
    )
    options_placeholder = (
        "Type “yes” or “no” for ground certification, or “/hint”…"
        if options_stage == "ground_certification"
        else "Type “j trim”, “double leg”, “none”, or “/hint”…"
    )
    placeholders: dict[str, str] = {
        "built_size": "Type style + size (example: “A-Frame 12x21”), or type “/hint”…",
        "leg_height": "Type leg height (example: “10 ft”), or “/hint”…",
        "openings_types": openings_placeholder,
        "openings_placement": "Type placement (example: “garage front 0”), or “/hint”…",
        "options": options_placeholder,
        "colors": "Type “skip” to keep defaults, or “/hint”…",
        "notes": "Type notes, “none”, or “/hint”…",
        "quote": "Review the quote in the Configuration tab (type “/hint” for help)…",
        "done": "All set (type “/hint” for what you can do next)…",
    }
    return placeholders.get(step_key, "Type here…")


def _chat_command_tokens(text: str) -> set[str]:
    """
    Extract normalized command-ish tokens from freeform chat input.

    This lets users type things like "none continue" or "skip, next" and still
    get the intended behavior.
    """
    t = (text or "").lower()
    return set(re.findall(r"[a-z]+", t))


_CHAT_SLASH_COMMAND_RE = re.compile(r"^\s*/([a-zA-Z_]+)\b")

_KNOWN_CHAT_SLASH_COMMANDS = {
    "hint",
    "help",
    "next",
    "continue",
    "apply",
    "ok",
    "cancel",
    "no",
}


def _chat_slash_command(text: str) -> Optional[str]:
    """
    Extract a leading slash-command like "/next" or "/hint".

    Returns the normalized command name (lowercase) without the leading slash.
    """
    m = _CHAT_SLASH_COMMAND_RE.match(text or "")
    if not m:
        return None
    cmd = str(m.group(1)).strip().lower()
    if cmd in _KNOWN_CHAT_SLASH_COMMANDS:
        return cmd
    # Be forgiving for intentional slash commands with minor typos (e.g. "/spply").
    close = difflib.get_close_matches(cmd, list(_KNOWN_CHAT_SLASH_COMMANDS), n=1, cutoff=0.75)
    if close:
        return close[0]
    return cmd


def _chat_bare_command(text: str) -> Optional[str]:
    """
    Detect a bare command like "next" or "reset".

    Important: this is intentionally strict to avoid accidental triggers like "next week".
    """
    t = (text or "").strip().lower()
    if not t:
        return None
    tokens = re.findall(r"[a-z]+", t)
    if len(tokens) != 1:
        return None
    return tokens[0]


def _ai_intent_context_for_step(step_key: str, book: PriceBook) -> dict[str, object]:
    style_labels = ["Regular (Horizontal)", "A-Frame (Horizontal)", "A-Frame (Vertical)"]
    allowed_widths = list(book.allowed_widths_ft)
    horizontal_lengths = [21, 26, 31, 36]
    vertical_lengths = [20, 25, 30, 35]
    allowed_leg_heights = list(book.allowed_leg_heights_ft)

    pending = st.session_state.get("chat_pending_suggestion")
    pending_obj = pending if isinstance(pending, dict) else None

    ctx: dict[str, object] = {
        "step_key": step_key,
        "current": {
            "demo_style": str(st.session_state.get("demo_style") or ""),
            "width_ft": int(st.session_state.get("width_ft") or 0),
            "length_ft": int(st.session_state.get("length_ft") or 0),
            "leg_height_ft": int(st.session_state.get("leg_height_ft") or 0),
            "walk_in_door_type": str(st.session_state.get("walk_in_door_type") or "None"),
            "walk_in_door_count": int(st.session_state.get("walk_in_door_count") or 0),
            "window_size": str(st.session_state.get("window_size") or "None"),
            "window_count": int(st.session_state.get("window_count") or 0),
            "garage_door_type": str(st.session_state.get("garage_door_type") or "None"),
            "garage_door_size": str(st.session_state.get("garage_door_size") or "10x8"),
            "garage_door_count": int(st.session_state.get("garage_door_count") or 0),
            "selected_option_codes": list(st.session_state.get("selected_option_codes") or []),
            "openings_types_stage": _openings_types_stage(),
            "options_stage": _options_stage(),
            "openings_placements_count": (
                len(st.session_state.get("openings") or []) if isinstance(st.session_state.get("openings"), list) else 0
            ),
        },
        "allowed": {
            "style_labels": style_labels,
            "widths_ft": allowed_widths,
            "horizontal_lengths_ft": horizontal_lengths,
            "vertical_lengths_ft": vertical_lengths,
            "leg_heights_ft": allowed_leg_heights,
            "walk_in_door_labels": ["None"] + _available_accessory_labels(book, WALK_IN_DOOR_OPTIONS),
            "window_sizes": ["None"] + _available_accessory_labels(book, WINDOW_OPTIONS),
            "roll_up_door_sizes": ["None"] + _available_accessory_labels(book, ROLL_UP_DOOR_OPTIONS),
            "option_codes": _available_option_codes(book),
            "colors": ["White", "Gray", "Black", "Tan", "Sandstone", "Brown", "Red", "Burgundy", "Blue", "Green"],
            "placements": ["front", "rear", "back", "left", "right"],
            "opening_kinds": ["door", "window", "garage"],
        },
        "pending": pending_obj,
    }
    return ctx


def _bulk_offsets_for_wall(*, side: str, count: int, width_ft: int, length_ft: int) -> list[int]:
    """
    Generate 'reasonable' offsets for bulk placement when the user says e.g. "3 doors on the left".
    """
    side_norm = str(side or "").strip().lower()
    wall_len = int(width_ft if side_norm in {"front", "back"} else length_ft)
    wall_len = max(0, wall_len)
    count = max(0, int(count))
    if count <= 0:
        return []
    if wall_len <= 1:
        return [0 for _ in range(count)]
    if count == 1:
        return [0]
    span = wall_len - 1
    return [int(round(i * span / (count - 1))) for i in range(count)]


def _parse_section_placement(text: str) -> Optional[SectionPlacement]:
    t = (text or "").lower()
    if re.search(r"\bfront\b", t):
        return SectionPlacement.FRONT
    if re.search(r"\brear\b", t) or re.search(r"\bback\b", t):
        return SectionPlacement.BACK
    if re.search(r"\bleft\b", t):
        return SectionPlacement.LEFT
    if re.search(r"\bright\b", t):
        return SectionPlacement.RIGHT
    return None


def _try_handle_with_ai_intent(*, step_key: str, raw: str, step_index: int, max_step_index: int, book: PriceBook) -> bool:
    """
    Optional GPT intent recognition layer.

    Returns True if it handled the input (and typically calls st.rerun()).
    """
    if not ai_intent.ai_intent_enabled():
        return False

    ctx = _ai_intent_context_for_step(step_key, book)
    intent = ai_intent.recognize_step_intent(step_key=step_key, user_text=raw, context=ctx)
    if intent is None:
        return False

    if intent.action == "noop":
        return False

    if intent.action == "clarify":
        msg = intent.clarification or "Can you clarify what you want to do for this step?"
        _chat_add(role="assistant", content=msg)
        st.rerun()

    # Built & Size
    if step_key == "built_size":
        if intent.action == "apply_suggestion":
            pending = st.session_state.get("chat_pending_suggestion")
            if isinstance(pending, dict) and pending.get("kind") == "built_size":
                suggested = pending.get("suggested")
                if isinstance(suggested, dict):
                    w = suggested.get("width_ft")
                    l = suggested.get("length_ft")
                    if isinstance(w, int) and isinstance(l, int):
                        st.session_state["width_ft"] = int(w)
                        st.session_state["length_ft"] = int(l)
                        st.session_state["chat_built_size_has_dims"] = True
                        _clear_pending_chat_suggestion()
                        _chat_add(role="assistant", content=f"Applied: **{int(w)}x{int(l)} ft**. Next step.")
                        st.session_state["wizard_step"] = min(max_step_index, step_index + 1)
                        st.rerun()
            _chat_add(role="assistant", content="Nothing to apply right now. Type **/hint** for examples.")
            st.rerun()

        if intent.action == "cancel_suggestion":
            if st.session_state.get("chat_pending_suggestion") is not None:
                _clear_pending_chat_suggestion()
                _chat_add(role="assistant", content="OK — keeping what you typed. Continue with a size like **12x21**.")
                st.rerun()
            _chat_add(role="assistant", content="Nothing to cancel. Type **/hint** for examples.")
            st.rerun()

        updates = dict(intent.updates or {})
        style = updates.get("demo_style")
        if isinstance(style, str) and style.strip() in {"Regular (Horizontal)", "A-Frame (Horizontal)", "A-Frame (Vertical)"}:
            st.session_state["demo_style"] = style.strip()
            st.session_state["chat_built_size_has_style"] = True
        width = updates.get("width_ft")
        length = updates.get("length_ft")
        if isinstance(width, int) and isinstance(length, int) and width > 0 and length > 0:
            st.session_state["width_ft"] = int(width)
            st.session_state["length_ft"] = int(length)
            st.session_state["chat_built_size_has_dims"] = True

        # Finish like the deterministic flow: suggest next-size-up if needed, otherwise advance.
        has_style = bool(st.session_state.get("chat_built_size_has_style")) and bool(str(st.session_state.get("demo_style") or "").strip())
        has_dims = bool(st.session_state.get("chat_built_size_has_dims")) and int(st.session_state.get("width_ft") or 0) > 0 and int(st.session_state.get("length_ft") or 0) > 0
        st.session_state["chat_built_size_has_style"] = bool(has_style)
        st.session_state["chat_built_size_has_dims"] = bool(has_dims)

        if not has_style:
            _chat_add(
                role="assistant",
                content="Which style? **Regular**, **A-Frame Horizontal**, or **A-Frame Vertical**. (Type **/hint**.)",
            )
            st.rerun()

        if not has_dims:
            _chat_add(role="assistant", content="What size in feet? Example: **12x21**. (Type **/hint** for examples.)")
            st.rerun()

        style_now = str(st.session_state.get("demo_style") or "")
        width_now = int(st.session_state.get("width_ft") or 0)
        length_now = int(st.session_state.get("length_ft") or 0)
        allowed_lengths = [20, 25, 30, 35] if style_now == "A-Frame (Vertical)" else [21, 26, 31, 36]

        suggested_w = width_now if width_now in set(book.allowed_widths_ft) else _next_size_up(width_now, list(book.allowed_widths_ft))
        suggested_l = length_now if length_now in set(allowed_lengths) else _next_size_up(length_now, allowed_lengths)
        if suggested_w is not None and suggested_l is not None and (suggested_w != width_now or suggested_l != length_now):
            st.session_state["chat_pending_suggestion"] = {
                "kind": "built_size",
                "suggested": {"width_ft": int(suggested_w), "length_ft": int(suggested_l)},
            }
            _chat_add(
                role="assistant",
                content=(
                    "Per manufacturer rule, we price at the **next size up** when a size isn’t listed.\n\n"
                    f"Suggested priced size: **{int(suggested_w)}x{int(suggested_l)} ft**.\n\n"
                    "Type **/apply** to use that, or **/cancel** to keep what you typed."
                ),
            )
            st.rerun()

        can_advance, reason = _chat_can_advance_step(st.session_state, "built_size", book)
        if can_advance:
            _chat_add(
                role="assistant",
                content=f"OK — you chose **{style_now}**, **{width_now}x{length_now} ft**.",
            )
            st.session_state["wizard_step"] = min(max_step_index, step_index + 1)
            st.rerun()
        _chat_add(role="assistant", content=reason or "Type **/hint** for examples.")
        st.rerun()

    # Leg height
    if step_key == "leg_height" and intent.action == "set_leg_height":
        h = intent.updates.get("leg_height_ft")
        if isinstance(h, int) and h in set(book.allowed_leg_heights_ft):
            st.session_state["leg_height_ft"] = int(h)
            _chat_add(role="assistant", content=f"OK — you chose **{int(h)} ft** leg height.")
            st.session_state["wizard_step"] = min(max_step_index, step_index + 1)
            st.rerun()
        _chat_add(role="assistant", content=f"Pick one of the allowed leg heights: **{', '.join(str(x) for x in book.allowed_leg_heights_ft)}**.")
        st.rerun()

    # Openings (types)
    if step_key == "openings_types":
        if intent.action == "clear_openings_types":
            st.session_state["walk_in_door_type"] = "None"
            st.session_state["walk_in_door_count"] = 0
            st.session_state["window_size"] = "None"
            st.session_state["window_count"] = 0
            st.session_state["garage_door_type"] = "None"
            st.session_state["garage_door_count"] = 0
            st.session_state["openings"] = []
            st.session_state["opening_seq"] = 1
            _chat_add(role="assistant", content="OK — no openings.")
            st.session_state["wizard_step"] = min(max_step_index, step_index + 1)
            st.rerun()

        if intent.action == "set_openings_types":
            door_labels = ["None"] + _available_accessory_labels(book, WALK_IN_DOOR_OPTIONS)
            win_labels = ["None"] + _available_accessory_labels(book, WINDOW_OPTIONS)
            rollup_sizes = ["None"] + _available_accessory_labels(book, ROLL_UP_DOOR_OPTIONS)

            updates = dict(intent.updates or {})
            door_type = updates.get("walk_in_door_type")
            door_count = updates.get("walk_in_door_count")
            if isinstance(door_type, str) and door_type in door_labels:
                st.session_state["walk_in_door_type"] = door_type
            if isinstance(door_count, int):
                st.session_state["walk_in_door_count"] = max(0, min(12, int(door_count)))

            window_size = updates.get("window_size")
            window_count = updates.get("window_count")
            if isinstance(window_size, str) and window_size in win_labels:
                st.session_state["window_size"] = window_size
            if isinstance(window_count, int):
                st.session_state["window_count"] = max(0, min(12, int(window_count)))

            garage_type = updates.get("garage_door_type")
            garage_size = updates.get("garage_door_size")
            garage_count = updates.get("garage_door_count")
            if isinstance(garage_type, str) and garage_type in {"None", "Roll-up"}:
                st.session_state["garage_door_type"] = garage_type
            if isinstance(garage_size, str) and garage_size in rollup_sizes:
                st.session_state["garage_door_size"] = garage_size
            if isinstance(garage_count, int):
                st.session_state["garage_door_count"] = max(0, min(8, int(garage_count)))

            _chat_add(role="assistant", content="OK — openings updated.")
            st.session_state["wizard_step"] = min(max_step_index, step_index + 1)
            st.rerun()

    # Openings (placement)
    if step_key == "openings_placement":
        if intent.action == "clear_openings_placements":
            st.session_state["openings"] = []
            st.session_state["opening_seq"] = 1
            _chat_add(role="assistant", content="OK — skipping placement.")
            st.session_state["wizard_step"] = min(max_step_index, step_index + 1)
            st.rerun()

        if intent.action in {"set_openings_placements", "bulk_place"}:
            w = int(st.session_state.get("width_ft") or 0)
            l = int(st.session_state.get("length_ft") or 0)
            updates = dict(intent.updates or {})

            placements: list[dict[str, object]] = []
            raw_placements = updates.get("placements")
            if isinstance(raw_placements, list):
                for p in raw_placements:
                    if not isinstance(p, dict):
                        continue
                    kind = str(p.get("kind") or "").lower()
                    side = str(p.get("side") or "").lower()
                    off = p.get("offset_ft")
                    if kind not in {"door", "window", "garage"} or side not in {"front", "back", "left", "right"}:
                        continue
                    if not isinstance(off, int):
                        continue
                    placements.append({"kind": kind, "side": side, "offset_ft": max(0, int(off))})

            if intent.action == "bulk_place":
                bulk = updates.get("bulk")
                if isinstance(bulk, dict):
                    kind = str(bulk.get("kind") or "").lower()
                    side = str(bulk.get("side") or "").lower()
                    count = bulk.get("count")
                    if kind in {"door", "window", "garage"} and side in {"front", "back", "left", "right"} and isinstance(count, int):
                        for off in _bulk_offsets_for_wall(side=side, count=int(count), width_ft=w, length_ft=l):
                            placements.append({"kind": kind, "side": side, "offset_ft": int(off)})

            if not placements:
                _chat_add(role="assistant", content="For placement, try: **door left 3**, **window right 5**, or **garage front 0** — or type **skip**.")
                st.rerun()

            st.session_state["openings"] = []
            st.session_state["opening_seq"] = 1
            oid = 1
            for p in placements:
                openings_list = st.session_state.get("openings")
                if isinstance(openings_list, list):
                    openings_list.append({"id": oid, **p})
                oid += 1
            st.session_state["opening_seq"] = oid
            _chat_add(role="assistant", content=f"Placed **{len(placements)}** opening(s). Add more, or type **/next**.")
            st.rerun()

    # Options
    if step_key == "options":
        if intent.action == "clear_options":
            st.session_state["include_ground_certification"] = False
            st.session_state["selected_option_codes"] = []
            st.session_state["extra_panel_count"] = 0
            placement_keys = [k for k in st.session_state if isinstance(k, str) and k.startswith("placement_")]
            for k in placement_keys:
                try:
                    del st.session_state[k]
                except Exception:
                    pass
            _chat_add(role="assistant", content="OK — no options.")
            st.session_state["wizard_step"] = min(max_step_index, step_index + 1)
            st.rerun()

        if intent.action == "set_options":
            allowed = set(_available_option_codes(book))
            updates = dict(intent.updates or {})
            raw_codes = updates.get("option_codes")
            codes: list[str] = []
            if isinstance(raw_codes, list):
                for c in raw_codes:
                    if isinstance(c, str) and c in allowed:
                        codes.append(c)
            if not codes:
                _chat_add(role="assistant", content="Which option code(s) should I add? Type **/hint** to see the list.")
                st.rerun()

            existing = st.session_state.get("selected_option_codes")
            existing_list = list(existing) if isinstance(existing, list) else []
            for c in codes:
                if c not in existing_list:
                    existing_list.append(c)
            st.session_state["selected_option_codes"] = existing_list

            inc_gc = updates.get("include_ground_certification")
            if isinstance(inc_gc, bool):
                st.session_state["include_ground_certification"] = bool(inc_gc)

            _chat_add(role="assistant", content=f"OK — added option(s): {', '.join(f'**{c}**' for c in codes)}.")
            st.session_state["wizard_step"] = min(max_step_index, step_index + 1)
            st.rerun()

    # Colors
    if step_key == "colors" and intent.action == "set_colors":
        allowed_colors = {"White", "Gray", "Black", "Tan", "Sandstone", "Brown", "Red", "Burgundy", "Blue", "Green"}
        updates = dict(intent.updates or {})
        changed = False
        for k in ("roof_color", "trim_color", "side_color"):
            v = updates.get(k)
            if isinstance(v, str) and v in allowed_colors:
                st.session_state[k] = v
                changed = True
        if not changed:
            _chat_add(role="assistant", content="Tell me roof/trim/side colors (or type **skip**). Type **/hint** for choices.")
            st.rerun()
        _chat_add(role="assistant", content="Colors saved. Next step.")
        st.session_state["wizard_step"] = min(max_step_index, step_index + 1)
        st.rerun()

    # Notes
    if step_key == "notes" and intent.action == "set_notes":
        note = intent.updates.get("internal_notes")
        if isinstance(note, str):
            st.session_state["internal_notes"] = note
            _chat_add(role="assistant", content="OK — notes saved.")
            st.session_state["wizard_step"] = min(max_step_index, step_index + 1)
            st.rerun()

    return False

def _chat_menu_for_step(step_key: str, book: PriceBook) -> str:
    """
    Return a short, step-specific help "menu" shown when the user types /hint.
    """
    style_now = str(st.session_state.get("demo_style") or "")
    width_now = int(st.session_state.get("width_ft") or 0)
    length_now = int(st.session_state.get("length_ft") or 0)
    leg_height_now = int(st.session_state.get("leg_height_ft") or 0)
    openings_stage = _openings_types_stage()
    options_stage = _options_stage()

    allowed_leg_heights = ", ".join(str(x) for x in (list(book.allowed_leg_heights_ft) or [6]))
    allowed_widths = ", ".join(str(x) for x in list(book.allowed_widths_ft))
    horizontal_lengths = ", ".join(str(x) for x in [21, 26, 31, 36])
    vertical_lengths = ", ".join(str(x) for x in [20, 25, 30, 35])

    pending = st.session_state.get("chat_pending_suggestion")
    has_pending_size_suggestion = isinstance(pending, dict) and pending.get("kind") == "built_size"
    apply_cancel = ""
    if has_pending_size_suggestion:
        apply_cancel = (
            "\n"
            "- **/apply** (accept the suggested priced size)\n"
            "- **/cancel** (keep what you typed)\n"
        )

    built_size = (
        "## Built & Size — menu\n\n"
        "**What we’re doing**: pick the exact building **style** and **size**.\n\n"
        "### Your current selection\n"
        f"- **Style**: **{style_now or '(not set yet)'}**\n"
        f"- **Size**: **{width_now}x{length_now} ft**\n\n"
        "### Options (pick one style)\n"
        "- **Regular (Horizontal)**\n"
        "- **A-Frame (Horizontal)**\n"
        "- **A-Frame (Vertical)**\n\n"
        "### Allowed sizes\n"
        f"- **Widths (ft)**: **{allowed_widths}**\n"
        f"- **Lengths for Regular / A-Frame Horizontal (ft)**: **{horizontal_lengths}**\n"
        f"- **Lengths for A-Frame Vertical (ft)**: **{vertical_lengths}**\n\n"
        "### Examples you can type\n"
        "- **A-Frame 12x21**\n"
        "- **Regular 18x26**\n\n"
        "### How pricing works if your size isn’t listed\n"
        "If you type a size that isn’t in the price book, we’ll suggest the **next size up** for pricing.\n\n"
        "### Commands\n"
        "- **/hint** (show this menu)\n"
        "- **/next** (only advances once **style + size** are set)\n"
        "- **continue** (same as /next)\n"
        f"{apply_cancel}"
    )

    leg_height = (
        "## Leg Height — menu\n\n"
        "**What we’re doing**: choose your building’s **leg height**.\n\n"
        "### Your current selection\n"
        f"- **Leg height**: **{leg_height_now} ft**\n\n"
        "### Allowed values\n"
        f"- **Leg height (ft)**: **{allowed_leg_heights}**\n\n"
        "### Examples you can type\n"
        "- **10 ft**\n"
        "- **12 ft**\n\n"
        "### Commands\n"
        "- **/hint** (show this menu)\n"
        "- **/next**\n"
        "- **continue** (same as /next)\n"
    )

    walk_in_labels = ["None"] + _available_accessory_labels(book, WALK_IN_DOOR_OPTIONS)
    window_labels = ["None"] + _available_accessory_labels(book, WINDOW_OPTIONS)
    rollup_labels = ["None"] + _available_accessory_labels(book, ROLL_UP_DOOR_OPTIONS)

    openings_focus = "**doors**" if openings_stage == "doors" else "**windows**" if openings_stage == "windows" else "**garage doors**"
    openings_options_and_examples = (
        f"- **Walk-in door types**: {', '.join(f'**{x}**' for x in walk_in_labels)}\n\n"
        "### Examples you can type\n"
        "- **standard door x2**\n"
        "- **add 1 door**\n"
        "- **none**\n\n"
        if openings_stage == "doors"
        else (
            f"- **Window sizes**: {', '.join(f'**{x}**' for x in window_labels)}\n\n"
            "### Examples you can type\n"
            "- **add 2 windows 24x36**\n"
            "- **windows 30x36 x4**\n"
            "- **none**\n\n"
            if openings_stage == "windows"
            else (
                f"- **Roll-up door sizes**: {', '.join(f'**{x}**' for x in rollup_labels)}\n\n"
                "### Examples you can type\n"
                "- **2 roll-up 10x8**\n"
                "- **roll-up 9x8 x1**\n"
                "- **none**\n\n"
            )
        )
    )
    openings_types = (
        "## Openings (Types) — menu\n\n"
        "**What we’re doing**: choose **types + quantities** (doors, windows, garage doors).\n\n"
        f"### Current micro-step\n- Focus: {openings_focus}\n\n"
        "### Your current selection\n"
        f"- **Walk-in door**: **{st.session_state.get('walk_in_door_type') or 'None'}** × **{int(st.session_state.get('walk_in_door_count') or 0)}**\n"
        f"- **Windows**: **{st.session_state.get('window_size') or 'None'}** × **{int(st.session_state.get('window_count') or 0)}**\n"
        f"- **Garage door**: **{st.session_state.get('garage_door_type') or 'None'}**"
        f" {st.session_state.get('garage_door_size') or ''} × **{int(st.session_state.get('garage_door_count') or 0)}**\n\n"
        "### Options you can choose (from this price book)\n"
        + openings_options_and_examples
        + "### Commands\n"
        "- **/hint** (show this menu)\n"
        "- **/next** (go to placement — optional)\n"
    )

    w = int(st.session_state.get("width_ft") or 0)
    l = int(st.session_state.get("length_ft") or 0)
    openings_count = len(st.session_state.get("openings") or []) if isinstance(st.session_state.get("openings"), list) else 0
    openings_placement = (
        "## Openings (Placement) — menu\n\n"
        "**Optional**: place openings by **wall + offset** so the drawing matches what you mean.\n\n"
        "### Your current selection\n"
        f"- **Placed openings**: **{int(openings_count)}**\n\n"
        "### How placement works\n"
        "- Pick a **wall**: **front / back / left / right**\n"
        "- Give an **offset (ft)** along that wall (0 is the start of the wall)\n"
        f"- Current building size: **{w}x{l} ft** (front/back wall length = width; left/right wall length = length)\n\n"
        "### Examples you can type\n"
        "- **door left 3**\n"
        "- **window right 5**\n"
        "- **garage front 0**\n"
        "- **3 doors left** (bulk place)\n"
        "- **skip** (use automatic placement)\n"
        "- **none** (no explicit placements)\n\n"
        "### Commands\n"
        "- **/hint** (show this menu)\n"
        "- **/next**\n"
    )

    option_codes = _available_option_codes(book)
    selected_codes = list(st.session_state.get("selected_option_codes") or []) if isinstance(st.session_state.get("selected_option_codes"), list) else []
    options_list = "\n".join(f"- **{code}**" for code in option_codes) if option_codes else "- (No option codes found in this price book.)"
    options_focus = "**ground certification**" if options_stage == "ground_certification" else "**J-Trim / Double Leg**"
    options = (
        "## Options — menu\n\n"
        "**What we’re doing**: add optional upgrades.\n\n"
        f"### Current micro-step\n- Focus: {options_focus}\n\n"
        "### Your current selection\n"
        f"- **Ground certification**: **{bool(st.session_state.get('include_ground_certification'))}**\n"
        f"- **Selected option codes**: {', '.join(f'**{c}**' for c in selected_codes) if selected_codes else '**(none)**'}\n\n"
        "### Most common choices\n"
        "- **ground certification**\n"
        "- **j trim**\n"
        "- **double leg**\n\n"
        "### All available option codes (this price book)\n"
        f"{options_list}\n\n"
        "### Examples you can type\n"
        "- **j trim**\n"
        "- **ground certification**\n"
        "- **double leg**\n"
        "- **none**\n\n"
        "### Commands\n"
        "- **/hint** (show this menu)\n"
        "- **/next**\n"
    )

    color_choices = ["White", "Gray", "Black", "Tan", "Sandstone", "Brown", "Red", "Burgundy", "Blue", "Green"]
    colors = (
        "## Colors — menu\n\n"
        "**What we’re doing**: choose colors for roof/trim/sides.\n\n"
        "### Your current selection\n"
        f"- **Roof**: **{st.session_state.get('roof_color') or 'White'}**\n"
        f"- **Trim**: **{st.session_state.get('trim_color') or 'White'}**\n"
        f"- **Sides**: **{st.session_state.get('side_color') or 'White'}**\n\n"
        "### All available colors\n"
        f"{', '.join(f'**{c}**' for c in color_choices)}\n\n"
        "### Examples you can type\n"
        "- **roof white, trim black, sides black**\n"
        "- **skip** (keep defaults)\n\n"
        "### Commands\n"
        "- **/hint** (show this menu)\n"
        "- **/next**\n"
    )

    notes = (
        "## Notes — menu\n\n"
        "**What we’re doing**: add any internal notes (demo-only).\n\n"
        "### Examples you can type\n"
        "- **Customer wants install next month**\n"
        "- **none**\n\n"
        "### Commands\n"
        "- **/hint** (show this menu)\n"
        "- **/next**\n"
    )

    quote = (
        "## Quote — menu\n\n"
        "Your quote is ready. Review it in the **Configuration** tab.\n\n"
        "### Commands\n"
        "- **/hint** (show this menu)\n"
        "- **/next** (finish)\n"
    )

    done = (
        "## Done — menu\n\n"
        "You’re done with the guided quote flow.\n\n"
        "### What you can do next\n"
        "- Review the quote in the **Configuration** tab\n"
        "- Use **Reset quote** in the sidebar to start over\n\n"
        "### Commands\n"
        "- **/hint** (show this menu)\n"
    )
    menus: dict[str, str] = {
        "built_size": built_size,
        "leg_height": leg_height,
        "openings_types": openings_types,
        "openings_placement": openings_placement,
        "options": options,
        "colors": colors,
        "notes": notes,
        "quote": quote,
        "done": done,
    }
    return menus.get(step_key, "Type **/hint** for the menu, or **/next** to continue.")


def _next_size_up(value: int, allowed: list[int]) -> Optional[int]:
    """
    Return the smallest allowed value >= value.
    """
    if value <= 0:
        return None
    for a in sorted(set(int(x) for x in allowed)):
        if a >= value:
            return a
    return None


def _clear_pending_chat_suggestion() -> None:
    st.session_state.pop("chat_pending_suggestion", None)


def _chat_can_advance_step(state: Mapping[str, object], step_key: str, book: PriceBook) -> tuple[bool, str]:
    """
    Determine whether a user can advance from the current step.

    This is used to keep "/next" intentional and to avoid users getting lost when the
    required inputs for a step are not complete yet.
    """
    if step_key == "built_size":
        style = str(state.get("demo_style") or "")
        width = int(state.get("width_ft") or 0)
        length = int(state.get("length_ft") or 0)
        if not style:
            return (False, "I still need a **style** (Regular / A-Frame Horizontal / A-Frame Vertical). Type **/hint** for examples.")
        if width not in set(book.allowed_widths_ft):
            return (False, "I still need a valid **width** from the allowed list. Type **/hint** to see examples.")
        allowed_lengths = [20, 25, 30, 35] if style == "A-Frame (Vertical)" else [21, 26, 31, 36]
        if length not in set(allowed_lengths):
            return (False, "I still need a valid **length** for that style. Type **/hint** for examples.")
        return (True, "")

    if step_key == "leg_height":
        leg_height = int(state.get("leg_height_ft") or 0)
        if leg_height not in set(book.allowed_leg_heights_ft):
            return (False, "I still need a valid **leg height**. Type **/hint** for allowed values.")
        return (True, "")

    # These steps are intentionally permissive; users can skip and adjust later.
    if step_key in {
        "openings_types",
        "openings_placement",
        "options",
        "colors",
        "notes",
        "quote",
        "done",
    }:
        return (True, "")

    return (True, "")


def _first_int_in_text(text: str) -> Optional[int]:
    m = re.search(r"\b(\d+)\b", text or "")
    if not m:
        return None
    try:
        return int(m.group(1))
    except Exception:
        return None


def _parse_size_token(text: str) -> Optional[str]:
    """
    Parse a simple WxH size token like "10x8" (allowing spaces) and return it normalized.
    """
    t = (text or "").lower()
    m = re.search(r"\b(\d{1,2})\s*x\s*(\d{1,2})\b", t)
    if not m:
        return None
    w = int(m.group(1))
    h = int(m.group(2))
    if w <= 0 or h <= 0:
        return None
    return f"{w}x{h}"


def _pick_walk_in_label_from_text(text: str, available_labels: list[str]) -> Optional[str]:
    """
    Best-effort mapping from chat phrasing to a walk-in door label.
    """
    t = (text or "").lower()
    if "standard" in t:
        return "Standard 36x80" if "Standard 36x80" in available_labels else None
    if "nine" in t and "lite" in t:
        return "Nine Lite 36x80" if "Nine Lite 36x80" in available_labels else None
    if "six" in t and "panel" in t and "window" in t:
        return "Six Panel w/ Window 36x80" if "Six Panel w/ Window 36x80" in available_labels else None
    if "six" in t and "panel" in t:
        return "Six Panel 36x80" if "Six Panel 36x80" in available_labels else None
    return None


def _find_count_for_keyword(text: str, keyword_re: str) -> Optional[int]:
    """
    Find a count adjacent to a keyword group, e.g. "2 doors" or "doors x2".
    """
    t = (text or "").lower()
    patterns = [
        rf"\b(\d+)\s*(?:x\s*)?{keyword_re}\b",
        rf"\b{keyword_re}\s*(?:x\s*)?(\d+)\b",
    ]
    for pat in patterns:
        m = re.search(pat, t)
        if not m:
            continue
        try:
            return int(m.group(1))
        except Exception:
            continue
    return None


def _contains_any(text: str, needles: list[str]) -> bool:
    t = (text or "").lower()
    return any(n in t for n in needles)


def _match_option_codes_from_text(text: str, allowed_codes: set[str]) -> list[str]:
    """
    Match option codes from freeform text, including spaced variants like "j trim" -> "J_TRIM".
    """
    raw = (text or "")
    upper = raw.upper()
    typed_codes = set(re.findall(r"\b[A-Z0-9_]{3,}\b", upper))
    normalized_text = re.sub(r"[^A-Z0-9]+", "", upper)
    picked: list[str] = []

    # Common spoken aliases that don't include the full code suffix.
    # Example: "double leg" should map to "DOUBLE_LEG_UP_TO_12" when that code exists.
    if "DOUBLE_LEG_UP_TO_12" in allowed_codes and "DOUBLELEG" in normalized_text:
        picked.append("DOUBLE_LEG_UP_TO_12")
    if "J_TRIM" in allowed_codes and "JTRIM" in normalized_text:
        picked.append("J_TRIM")

    for code in sorted(allowed_codes):
        if code in typed_codes:
            picked.append(code)
            continue
        norm_code = re.sub(r"[^A-Z0-9]+", "", code.upper())
        if norm_code and norm_code in normalized_text:
            picked.append(code)

    # Preserve insertion order, remove duplicates.
    out: list[str] = []
    seen: set[str] = set()
    for c in picked:
        if c not in seen:
            out.append(c)
            seen.add(c)
    return out


def _parse_color_assignments(text: str, allowed_colors: list[str]) -> dict[str, str]:
    """
    Parse color assignments like "roof white, trim black, sides black".
    Returns keys: roof_color/trim_color/side_color (subset).
    """
    t = (text or "").lower()
    allowed_lower = {c.lower(): c for c in allowed_colors}

    out: dict[str, str] = {}

    # Build a safe alternation for allowed colors so we can match explicitly.
    color_alt = "|".join(re.escape(c.lower()) for c in allowed_colors if isinstance(c, str) and c)
    if not color_alt:
        return out

    # Pattern A: "roof black", "trim white", "sides tan" (field then color).
    for m in re.finditer(rf"\b(roof|trim|side|sides)\b\s*[:=-]?\s*\b({color_alt})\b", t):
        field = str(m.group(1) or "")
        color = str(m.group(2) or "")
        if color in allowed_lower:
            key = "side_color" if field in {"side", "sides"} else f"{field}_color"
            out[key] = allowed_lower[color]

    # Pattern B: "black roof", "white trim", "tan sides" (color then field).
    for m in re.finditer(rf"\b({color_alt})\b\s*[:=-]?\s*\b(roof|trim|side|sides)\b", t):
        color = str(m.group(1) or "")
        field = str(m.group(2) or "")
        if color in allowed_lower:
            key = "side_color" if field in {"side", "sides"} else f"{field}_color"
            # Only fill missing fields here. This prevents accidental cross-boundary matches like:
            # "roof black trim white" → "black trim" should NOT override the explicit "trim white".
            if key not in out:
                out[key] = allowed_lower[color]

    return out


def _parse_opening_placement_instruction(text: str) -> Optional[dict[str, object]]:
    """
    Parse a placement instruction like "door left 3" or "garage front 0".

    Returns a dict with: kind (door/window/garage), side (front/back/left/right), offset_ft (int).
    """
    t = (text or "").lower()
    kind: Optional[str] = None
    for k in ("door", "window", "garage"):
        if re.search(rf"\b{k}s?\b", t):
            kind = k
            break
    if kind is None:
        return None

    side: Optional[str] = None
    for s in ("front", "back", "left", "right"):
        if re.search(rf"\b{s}\b", t):
            side = s
            break
    if side is None:
        return None

    offset_ft = _first_int_in_text(t)
    if offset_ft is None:
        offset_ft = 0
    offset_ft = max(0, int(offset_ft))
    return {"kind": kind, "side": side, "offset_ft": offset_ft}


def _parse_opening_bulk_placement_instruction(text: str) -> Optional[dict[str, object]]:
    """
    Parse a bulk placement instruction like "3 doors on the left" or "all windows back".

    Returns a dict with: kind, side, count (int).
    """
    t = (text or "").lower().strip()
    if not t:
        return None

    kind: Optional[str] = None
    for k in ("door", "window", "garage"):
        if re.search(rf"\b{k}s?\b", t):
            kind = k
            break
    if kind is None:
        return None

    side: Optional[str] = None
    for s in ("front", "back", "left", "right"):
        if re.search(rf"\b{s}\b", t):
            side = s
            break
    if side is None:
        return None

    if re.search(r"\ball\b", t):
        return {"kind": kind, "side": side, "count": -1}

    m = re.search(r"\b(\d+)\b", t)
    if not m:
        return None
    try:
        count = int(m.group(1))
    except Exception:
        return None
    if count <= 0:
        return None
    return {"kind": kind, "side": side, "count": count}


def _render_chat_action_card(*, step_key: str, step_index: int, max_step_index: int, book: PriceBook) -> None:
    """
    Right-side (Conversation tab) Action Card: a single, conditional UI element per step.

    This is intentionally separate from the Configuration tab widgets to avoid key collisions.
    """
    _maybe_sync_chat_action_for_step(book=book, step_key=step_key)

    with st.container(border=True):
        st.markdown("### Guided controls")
        st.caption("Make selections here (or type in chat). **Apply & continue** is the primary action.")

        with st.expander("More actions", expanded=False):
            more_cols = st.columns([1, 1, 1])
            if more_cols[0].button("Back", key="chat_cmd_back", use_container_width=True, disabled=step_index <= 0):
                st.session_state["wizard_step"] = max(0, step_index - 1)
                st.rerun()
            if more_cols[1].button("Reset", key="chat_cmd_reset", use_container_width=True):
                st.session_state["_chat_reset_requested"] = True
                st.rerun()
            if more_cols[2].button("Reset guided controls", key="chat_cmd_resync", use_container_width=True):
                # Re-sync this step from canonical wizard state.
                st.session_state.pop(f"chat_action_dirty_{step_key}", None)
                _sync_chat_action_from_wizard(book=book, step_key=step_key)
                st.session_state["chat_action_last_synced_step"] = step_key
                st.rerun()

        if step_key == "built_size":
            st.markdown("**Style + size**")
            style_labels = ["Regular (Horizontal)", "A-Frame (Horizontal)", "A-Frame (Vertical)"]
            st.selectbox("Style", options=style_labels, key="chat_action_demo_style")
            st.selectbox("Width (ft)", options=list(book.allowed_widths_ft), key="chat_action_width_ft")
            is_vertical = str(st.session_state.get("chat_action_demo_style")) == "A-Frame (Vertical)"
            allowed_lengths = [20, 25, 30, 35] if is_vertical else [21, 26, 31, 36]
            if st.session_state.get("chat_action_length_ft") not in allowed_lengths:
                st.session_state["chat_action_length_ft"] = allowed_lengths[0]
            st.selectbox("Length (ft)", options=allowed_lengths, key="chat_action_length_ft")
            st.caption("Tip: you can also type “A-Frame 12x21”.")

            if st.button("Apply & continue", key="chat_action_apply_built_size", use_container_width=True):
                _apply_chat_action_to_wizard(step_key=step_key)
                st.session_state["chat_built_size_has_style"] = True
                st.session_state["chat_built_size_has_dims"] = True
                _chat_add(
                    role="assistant",
                    tag="ack:built_size_action",
                    content=(
                        f"Locked in: **{str(st.session_state.get('demo_style') or '')}**, "
                        f"**{int(st.session_state.get('width_ft') or 0)}x{int(st.session_state.get('length_ft') or 0)} ft**."
                    ),
                )
                st.session_state["wizard_step"] = min(max_step_index, step_index + 1)
                st.rerun()

        elif step_key == "leg_height":
            st.markdown("**Leg height**")
            leg_heights = list(book.allowed_leg_heights_ft) or [6]
            if st.session_state.get("chat_action_leg_height_ft") not in leg_heights:
                st.session_state["chat_action_leg_height_ft"] = leg_heights[0]
            st.selectbox("Leg height (ft)", options=leg_heights, key="chat_action_leg_height_ft")
            st.caption("Tip: you can type “10 ft”.")

            if st.button("Apply & continue", key="chat_action_apply_leg_height", use_container_width=True):
                _apply_chat_action_to_wizard(step_key=step_key)
                _chat_add(
                    role="assistant",
                    tag="ack:leg_height_action",
                    content=f"Set leg height to **{int(st.session_state.get('leg_height_ft') or 0)} ft**.",
                )
                st.session_state["wizard_step"] = min(max_step_index, step_index + 1)
                st.rerun()

        elif step_key == "openings_types":
            st.markdown("**Openings (types + qty)**")
            st.caption("Choose the opening types and quantities. Placement is the next step.")

            st.markdown("**Walk-in doors**")
            d1, d2 = st.columns([3, 2], gap="medium")
            with d1:
                walk_in_labels = ["None"] + _available_accessory_labels(book, WALK_IN_DOOR_OPTIONS)
                if st.session_state.get("chat_action_walk_in_door_type") not in walk_in_labels:
                    st.session_state["chat_action_walk_in_door_type"] = "None"
                st.selectbox("Walk-in door type", options=walk_in_labels, key="chat_action_walk_in_door_type")
            with d2:
                st.number_input(
                    "Door qty",
                    min_value=0,
                    max_value=12,
                    step=1,
                    key="chat_action_walk_in_door_count",
                    disabled=str(st.session_state.get("chat_action_walk_in_door_type") or "None") == "None",
                )

            st.markdown("**Windows**")
            w1, w2 = st.columns([3, 2], gap="medium")
            with w1:
                window_labels = ["None"] + _available_accessory_labels(book, WINDOW_OPTIONS)
                if st.session_state.get("chat_action_window_size") not in window_labels:
                    st.session_state["chat_action_window_size"] = "None"
                st.selectbox("Window size", options=window_labels, key="chat_action_window_size")
            with w2:
                st.number_input(
                    "Window qty",
                    min_value=0,
                    max_value=24,
                    step=1,
                    key="chat_action_window_count",
                    disabled=str(st.session_state.get("chat_action_window_size") or "None") == "None",
                )

            st.markdown("**Garage doors**")
            g1, g2, g3 = st.columns([2, 2, 1], gap="medium")
            with g1:
                st.selectbox(
                    "Garage door type",
                    options=["None", "Roll-up", "Frame-out"],
                    key="chat_action_garage_door_type",
                )
            with g2:
                if st.session_state.get("chat_action_garage_door_type") == "Roll-up":
                    roll_up_labels = _available_accessory_labels(book, ROLL_UP_DOOR_OPTIONS)
                    if roll_up_labels:
                        if st.session_state.get("chat_action_garage_door_size") not in roll_up_labels:
                            st.session_state["chat_action_garage_door_size"] = roll_up_labels[0]
                        st.selectbox("Roll-up door size", options=roll_up_labels, key="chat_action_garage_door_size")
                    else:
                        st.warning("No roll-up door pricing found in this pricebook.")
                elif st.session_state.get("chat_action_garage_door_type") == "Frame-out":
                    st.caption("Frame-out openings are priced per opening (when available).")
                else:
                    st.caption("")
            with g3:
                st.number_input(
                    "Qty",
                    min_value=0,
                    max_value=4,
                    step=1,
                    key="chat_action_garage_door_count",
                    disabled=str(st.session_state.get("chat_action_garage_door_type") or "None") == "None",
                )

            if st.button("Apply & continue", key="chat_action_apply_openings_types", use_container_width=True):
                _apply_chat_action_to_wizard(step_key=step_key)
                _chat_add(role="assistant", tag="ack:openings_types_action", content="Openings saved. Next: placement (optional).")
                st.session_state["wizard_step"] = min(max_step_index, step_index + 1)
                st.rerun()

        elif step_key == "openings_placement":
            st.markdown("**Openings (placement)**")
            st.caption("Optional: set wall + offset for the drawing. If you skip, we’ll auto-place openings.")

            c1, c2 = st.columns([1, 1], gap="medium")
            if c1.button("Clear placements", key="chat_action_clear_openings", use_container_width=True):
                st.session_state[f"chat_action_dirty_{step_key}"] = True
                st.session_state["chat_action_openings"] = []
                st.rerun()
            if c2.button("Skip placement", key="chat_action_skip_openings", use_container_width=True):
                st.session_state[f"chat_action_dirty_{step_key}"] = True
                st.session_state["chat_action_openings"] = []
                _apply_chat_action_to_wizard(step_key=step_key)
                _chat_add(role="assistant", tag="ack:openings_placement_skip", content="Skipping placement — moving on.")
                st.session_state["wizard_step"] = min(max_step_index, step_index + 1)
                st.rerun()

            with st.expander("Add opening", expanded=False):
                a1, a2, a3 = st.columns([1, 1, 1])
                if a1.button("Door", key="chat_action_add_door", use_container_width=True):
                    st.session_state[f"chat_action_dirty_{step_key}"] = True
                    openings = st.session_state.get("chat_action_openings")
                    if isinstance(openings, list):
                        openings.append(
                            {
                                "id": int(st.session_state.get("chat_action_opening_seq") or 1),
                                "kind": "door",
                                "side": "front",
                                "offset_ft": 0,
                            }
                        )
                    st.session_state["chat_action_opening_seq"] = int(st.session_state.get("chat_action_opening_seq") or 1) + 1
                    st.rerun()
                if a2.button("Window", key="chat_action_add_window", use_container_width=True):
                    st.session_state[f"chat_action_dirty_{step_key}"] = True
                    openings = st.session_state.get("chat_action_openings")
                    if isinstance(openings, list):
                        openings.append(
                            {
                                "id": int(st.session_state.get("chat_action_opening_seq") or 1),
                                "kind": "window",
                                "side": "right",
                                "offset_ft": 0,
                            }
                        )
                    st.session_state["chat_action_opening_seq"] = int(st.session_state.get("chat_action_opening_seq") or 1) + 1
                    st.rerun()
                if a3.button("Garage", key="chat_action_add_garage", use_container_width=True):
                    st.session_state[f"chat_action_dirty_{step_key}"] = True
                    openings = st.session_state.get("chat_action_openings")
                    if isinstance(openings, list):
                        openings.append(
                            {
                                "id": int(st.session_state.get("chat_action_opening_seq") or 1),
                                "kind": "garage",
                                "side": "front",
                                "offset_ft": 0,
                            }
                        )
                    st.session_state["chat_action_opening_seq"] = int(st.session_state.get("chat_action_opening_seq") or 1) + 1
                    st.rerun()

            openings_now = st.session_state.get("chat_action_openings")
            if not isinstance(openings_now, list) or not openings_now:
                st.info("No explicit placements yet (auto-placement will be used).")
            else:
                st.caption(f"Placed openings: **{len(openings_now)}**")
                sides = ["front", "back", "left", "right"]
                for idx, row in enumerate(list(openings_now)):
                    if not isinstance(row, dict):
                        continue
                    oid = int(row.get("id") or (idx + 1))
                    with st.expander(f"Opening #{oid}", expanded=False):
                        r1, r2, r3, r4 = st.columns([1, 1, 1, 1])
                        kind = r1.selectbox(
                            "Type",
                            options=["door", "window", "garage"],
                            index=["door", "window", "garage"].index(str(row.get("kind") or "door")),
                            key=f"chat_action_opening_{oid}_kind",
                        )
                        side = r2.selectbox(
                            "Wall",
                            options=sides,
                            index=sides.index(str(row.get("side") or "front")) if str(row.get("side") or "front") in sides else 0,
                            key=f"chat_action_opening_{oid}_side",
                        )
                        wall_ft = (
                            int(st.session_state.get("width_ft") or 0)
                            if side in ("front", "back")
                            else int(st.session_state.get("length_ft") or 0)
                        )
                        max_offset = max(0, wall_ft)
                        offset_default = min(int(row.get("offset_ft") or 0), max_offset)
                        offset_ft = r3.number_input(
                            "Offset (ft)",
                            min_value=0,
                            max_value=max_offset,
                            step=1,
                            value=offset_default,
                            key=f"chat_action_opening_{oid}_offset",
                        )
                        if r4.button("Remove", key=f"chat_action_opening_{oid}_remove", use_container_width=True):
                            st.session_state[f"chat_action_dirty_{step_key}"] = True
                            st.session_state["chat_action_openings"] = [
                                o
                                for o in list(st.session_state.get("chat_action_openings") or [])
                                if not (isinstance(o, dict) and int(o.get("id") or -1) == oid)
                            ]
                            st.rerun()

                        row["kind"] = str(kind)
                        row["side"] = str(side)
                        row["offset_ft"] = int(offset_ft)
                    openings_after = st.session_state.get("chat_action_openings")
                    if isinstance(openings_after, list) and 0 <= idx < len(openings_after):
                        openings_after[idx] = row

            if st.button("Apply & continue", key="chat_action_apply_openings_placement", use_container_width=True):
                _apply_chat_action_to_wizard(step_key=step_key)
                _chat_add(role="assistant", tag="ack:openings_placement_action", content="Placement saved. Next step.")
                st.session_state["wizard_step"] = min(max_step_index, step_index + 1)
                st.rerun()

        elif step_key == "options":
            st.markdown("**Options**")
            st.checkbox("Ground certification", key="chat_action_include_ground_certification")
            option_codes = _available_option_codes(book)
            st.multiselect("Add options", options=option_codes, key="chat_action_selected_option_codes")
            placements = [
                None,
                SectionPlacement.FRONT,
                SectionPlacement.BACK,
                SectionPlacement.LEFT,
                SectionPlacement.RIGHT,
            ]
            codes = list(st.session_state.get("chat_action_selected_option_codes") or [])
            for code in codes:
                if not isinstance(code, str) or not code:
                    continue
                st.selectbox(
                    f"Placement for {code}",
                    options=placements,
                    format_func=lambda v: "(none)" if v is None else v.value,
                    key=f"chat_action_placement_{code}",
                )
            if "EXTRA_PANEL" in book.option_prices_by_length_usd:
                st.number_input(
                    "Extra panels",
                    min_value=0,
                    max_value=12,
                    step=1,
                    key="chat_action_extra_panel_count",
                )
            if st.button("Apply & continue", key="chat_action_apply_options", use_container_width=True):
                _apply_chat_action_to_wizard(step_key=step_key)
                _chat_add(role="assistant", tag="ack:options_action", content="Options saved. Next step.")
                st.session_state["wizard_step"] = min(max_step_index, step_index + 1)
                st.rerun()

        elif step_key == "colors":
            st.markdown("**Colors**")
            colors = ["White", "Gray", "Black", "Tan", "Sandstone", "Brown", "Red", "Burgundy", "Blue", "Green"]
            st.selectbox("Roof color", options=colors, key="chat_action_roof_color")
            st.selectbox("Trim color", options=colors, key="chat_action_trim_color")
            st.selectbox("Side color", options=colors, key="chat_action_side_color")
            if st.button("Apply & continue", key="chat_action_apply_colors", use_container_width=True):
                _apply_chat_action_to_wizard(step_key=step_key)
                _chat_add(role="assistant", tag="ack:colors_action", content="Colors saved. Next step.")
                st.session_state["wizard_step"] = min(max_step_index, step_index + 1)
                st.rerun()

        elif step_key == "notes":
            st.markdown("**Notes**")
            st.text_area("Internal notes (demo only)", key="chat_action_internal_notes", height=160)
            if st.button("Apply & continue", key="chat_action_apply_notes", use_container_width=True):
                _apply_chat_action_to_wizard(step_key=step_key)
                _chat_add(role="assistant", tag="ack:notes_action", content="Notes saved. Next step.")
                st.session_state["wizard_step"] = min(max_step_index, step_index + 1)
                st.rerun()

        elif step_key in {"quote", "done"}:
            st.info("Review the quote in the Configuration tab. Use the sidebar **Reset quote** if you want to start over.")


def _append_lead_snapshot(*, book: PriceBook, quote) -> None:
    quote_id = _quote_input_signature(book, quote)
    if st.session_state.get("lead_saved_quote_id") == quote_id:
        return

    lead_name = str(st.session_state.get("lead_name") or "").strip()
    lead_email = str(st.session_state.get("lead_email") or "").strip()
    if not _lead_is_valid(name=lead_name, email=lead_email):
        return

    payload = _quote_export_payload(book, quote)
    record = {
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "lead": {"name": lead_name, "email": _normalize_email(lead_email)},
        "quote_payload": payload,
        "quote_id": quote_id,
    }

    out_dir = Path(__file__).resolve().parent / "leads"
    out_dir.mkdir(parents=True, exist_ok=True)
    with (out_dir / "leads.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")
    st.session_state["lead_saved_quote_id"] = quote_id


def _lead_capture_form() -> None:
    st.subheader("Contact info (required)")
    st.caption("We’ll capture this first, then generate the quote.")
    st.text_input("Name", key="lead_name")
    st.text_input("Email", key="lead_email")

    lead_name = str(st.session_state.get("lead_name") or "")
    lead_email = str(st.session_state.get("lead_email") or "")
    can_continue = _lead_is_valid(name=lead_name, email=lead_email)

    if st.button("Continue to quote builder", disabled=not can_continue, use_container_width=True):
        st.session_state["lead_captured"] = True
        _chat_add(
            role="assistant",
            tag="lead_captured",
            content=(
                f"Thanks {lead_name.strip()} — got it. Next we’ll build your quote.\n\n"
                "Tell me **Style + Size** here (example: **A-Frame 12x21**). Type **/hint** anytime for details."
            ),
        )
        st.rerun()


def _openings_types_stage() -> str:
    """
    Micro-step stage for the "Openings (Types)" conversation.
    """
    stage = str(st.session_state.get("openings_types_stage") or "").strip().lower()
    if stage not in {"doors", "windows", "garage"}:
        stage = "doors"
    return stage


def _set_openings_types_stage(stage: str) -> None:
    """
    Set the micro-step stage for the "Openings (Types)" conversation.
    """
    s = str(stage or "").strip().lower()
    if s not in {"doors", "windows", "garage"}:
        s = "doors"
    st.session_state["openings_types_stage"] = s


def _options_stage() -> str:
    """
    Micro-step stage for the "Options" conversation.
    """
    stage = str(st.session_state.get("options_stage") or "").strip().lower()
    if stage not in {"ground_certification", "trim_or_double_leg"}:
        stage = "ground_certification"
    return stage


def _set_options_stage(stage: str) -> None:
    """
    Set the micro-step stage for the "Options" conversation.
    """
    s = str(stage or "").strip().lower()
    if s not in {"ground_certification", "trim_or_double_leg"}:
        s = "ground_certification"
    st.session_state["options_stage"] = s


def _chat_prompt_for_current_step(step_key: str) -> str:
    openings_stage = _openings_types_stage()
    options_stage = _options_stage()
    prompts: dict[str, str] = {
        "built_size": (
            "Awesome — let’s build your quote.\n\n"
            "First: **Style + Size**.\n\n"
            "- Type something like **A-Frame 12x21**\n"
            "- Type **/hint** for details\n"
        ),
        "leg_height": (
            "Next: choose **leg height**.\n\n"
            "- Reply with something like **10 ft** (or just **10**)\n"
            "- Type **/hint** for details\n"
            "- Type **go back** if you want to change the previous step\n"
        ),
        "openings_types": (
            (
                "Next: **openings** (we’ll do this in quick steps).\n\n"
                "First: **walk-in doors**.\n\n"
                "- Say **none** if you don’t want any doors\n"
                "- Examples: **standard door x2**, **add 1 door**\n"
                "- Type **/hint** for door types\n"
                "- Type **go back** if you want to change the previous step\n"
            )
            if openings_stage == "doors"
            else (
                "Next: **windows**.\n\n"
                "- Say **none** if you don’t want any windows\n"
                "- Examples: **add 2 windows 24x36**, **windows 30x36 x4**\n"
                "- Type **/hint** for window sizes\n"
                "- Type **go back** if you want to change the previous step\n"
            )
            if openings_stage == "windows"
            else (
                "Next: **garage doors** (roll-up).\n\n"
                "- Say **none** if you don’t want any garage doors\n"
                "- Examples: **2 roll-up 10x8**, **roll-up 9x8 x1**\n"
                "- Type **/hint** for roll-up sizes\n"
                "- Type **go back** if you want to change the previous step\n"
            )
        ),
        "openings_placement": (
            "Next (optional): place openings by **wall + offset** for the drawing.\n\n"
            "- Examples: **door left 3**, **window right 5**, **garage front 0**\n"
            "- Say **skip** to skip placement\n"
            "- Type **/hint** for details\n"
            "- Type **go back** if you want to change the previous step\n"
        ),
        "options": (
            (
                "Next: **options**.\n\n"
                "First: would you like **ground certification**?\n\n"
                "- Reply **yes** or **no**\n"
                "- Or type **ground certification** / **no ground certification**\n"
                "- Type **/hint** for details\n"
                "- Type **go back** if you want to change the previous step\n"
            )
            if options_stage == "ground_certification"
            else (
                "Next: would you like **J-Trim** or **Double Leg (up to 12 ft)**?\n\n"
                "- Reply with **j trim**, **double leg**, or **none**\n"
                "- Type **/hint** for details\n"
                "- Type **go back** if you want to change the previous step\n"
            )
        ),
        "colors": (
            "Next: pick **colors**.\n\n"
            "- Say **skip** to keep defaults\n"
            "- Type **/hint** for details\n"
            "- Type **go back** if you want to change the previous step\n"
        ),
        "notes": (
            "Next: any notes I should include?\n\n"
            "- Say **none** if you don’t have any\n"
            "- Or type the note text exactly as you want it saved\n"
            "- Type **go back** if you want to change the previous step\n"
        ),
        "quote": "Here’s the quote. Review it in the **Configuration** tab. Type **/hint** if you want the menu.",
        "done": "Your Quote has been finished — Thank you! A member of the team will be in contact.",
    }
    return prompts.get(step_key, "Type **/next** to continue, or **/hint** for the menu.")


def _wizard_step_key_for_index(step_index: int) -> str:
    steps = _wizard_steps()
    if not steps:
        return "built_size"
    idx = int(step_index)
    idx = max(0, min(idx, len(steps) - 1))
    return str(steps[idx][1])


def _chat_queue_step_prompt(step_key: str) -> None:
    if not bool(st.session_state.get("lead_captured")):
        return
    try:
        seq = int(st.session_state.get("chat_prompt_seq") or 1)
    except Exception:
        seq = 1
    _chat_add(
        role="assistant",
        tag=f"prompt:{step_key}:{seq}",
        content=_chat_prompt_for_current_step(step_key),
    )
    st.session_state["chat_last_prompted_step"] = step_key
    st.session_state["chat_prompt_seq"] = seq + 1


def _handle_chat_input(*, text: str, step_key: str, step_index: int, max_step_index: int, book: PriceBook) -> None:
    raw = (text or "").strip()
    if not raw:
        return

    _chat_add(role="user", content=raw)

    # Lead gating conversation: allow capture via chat as well.
    if not bool(st.session_state.get("lead_captured")):
        email = _extract_email(raw)
        if email and not str(st.session_state.get("lead_email") or "").strip():
            st.session_state["lead_email"] = email
        if not email and not str(st.session_state.get("lead_name") or "").strip():
            st.session_state["lead_name"] = raw.strip()

        lead_name = str(st.session_state.get("lead_name") or "").strip()
        lead_email = str(st.session_state.get("lead_email") or "").strip()
        if _lead_is_valid(name=lead_name, email=lead_email):
            st.session_state["lead_captured"] = True
            _chat_add(
                role="assistant",
                tag="lead_captured_chat",
                content=(
                    f"Perfect — saved **{lead_name}** / **{_normalize_email(lead_email)}**.\n\n"
                    "Now tell me **Style + Size** (example: **A-Frame 12x21**). Type **/hint** anytime for details."
                ),
            )
        else:
            missing = []
            if not lead_name:
                missing.append("name")
            if _extract_email(lead_email) is None:
                missing.append("email")
            _chat_add(
                role="assistant",
                tag="lead_missing",
                content=f"To start, I still need your {', '.join(missing)}. You can type it here or use the form.",
            )
        st.rerun()

    slash_cmd = _chat_slash_command(raw)
    bare_cmd = _chat_bare_command(raw)

    def _back_intent(t: str) -> bool:
        s = (t or "").strip().lower()
        return s in {"back", "go back", "goback", "previous", "prev"}

    def _no_intent(t: str) -> bool:
        s = (t or "").strip().lower()
        return s in {"no", "nope"}

    # If we just auto-advanced and the very next user input is "no" or "go back",
    # return to the previous step so they can overwrite.
    last_auto = st.session_state.get("chat_last_auto_advance")
    raw_norm = (raw or "").strip().lower()
    has_pending_option_placement = (
        step_key == "options" and isinstance(st.session_state.get("chat_pending_option_placement"), dict)
    )
    treat_back_as_placement = has_pending_option_placement and raw_norm == "back"

    treat_no_as_go_back = _no_intent(raw) and step_key not in {"openings_types", "openings_placement", "options", "notes"}
    if ((not treat_back_as_placement and _back_intent(raw)) or treat_no_as_go_back) and isinstance(last_auto, dict):
        try:
            from_idx = int(last_auto.get("from_step_index"))
            to_idx = int(last_auto.get("to_step_index"))
        except Exception:
            from_idx = -1
            to_idx = -1
        if to_idx == int(step_index) and 0 <= from_idx <= max_step_index:
            st.session_state.pop("chat_last_auto_advance", None)
            st.session_state["wizard_step"] = max(0, min(from_idx, max_step_index))
            _chat_add(role="assistant", tag="nav:go_back", content="No problem — going back so you can change that.")
            _chat_queue_step_prompt(_wizard_step_key_for_index(int(st.session_state.get("wizard_step") or 0)))
            st.rerun()

    # If the user explicitly says "go back" at any time, go back a step (not advertised in /hint).
    if not treat_back_as_placement and _back_intent(raw):
        st.session_state.pop("chat_last_auto_advance", None)
        st.session_state["wizard_step"] = max(0, step_index - 1)
        _chat_add(role="assistant", tag="nav:go_back_any", content="Okay — back one step. Type your updated choice (or **/hint**).")
        _chat_queue_step_prompt(_wizard_step_key_for_index(int(st.session_state.get("wizard_step") or 0)))
        st.rerun()

    # Clear any stale auto-advance marker once the user types something else.
    if st.session_state.get("chat_last_auto_advance") is not None:
        st.session_state.pop("chat_last_auto_advance", None)

    # Intentional command handling.
    if slash_cmd in {"hint", "help"}:
        _chat_add(role="assistant", tag=f"hint:{step_key}", content=_chat_menu_for_step(step_key, book))
        st.rerun()

    if slash_cmd in {"apply", "ok"} or bare_cmd == "apply":
        pending = st.session_state.get("chat_pending_suggestion")
        if isinstance(pending, dict) and pending.get("kind") == "built_size":
            suggested = pending.get("suggested")
            if isinstance(suggested, dict):
                w = suggested.get("width_ft")
                l = suggested.get("length_ft")
                if isinstance(w, int) and isinstance(l, int):
                    st.session_state["width_ft"] = int(w)
                    st.session_state["length_ft"] = int(l)
                    st.session_state["chat_built_size_has_dims"] = True
                    _chat_add(
                        role="assistant",
                        tag="ack:apply_suggestion",
                        content=(
                            f"Applied: **{int(st.session_state.get('width_ft') or 0)}x"
                            f"{int(st.session_state.get('length_ft') or 0)} ft**."
                        ),
                    )
                    _clear_pending_chat_suggestion()
                    if step_key == "built_size":
                        can_advance, reason = _chat_can_advance_step(st.session_state, step_key, book)
                        if can_advance:
                            next_idx = min(max_step_index, step_index + 1)
                            st.session_state["chat_last_auto_advance"] = {
                                "from_step_index": int(step_index),
                                "to_step_index": int(next_idx),
                            }
                            st.session_state["wizard_step"] = next_idx
                            st.rerun()
                        _chat_add(role="assistant", tag="help:after_apply_blocked", content=reason)
                        st.rerun()
                    _chat_add(role="assistant", tag="coach:after_apply", content="When you’re ready, type **/next**.")
                    st.rerun()
        _chat_add(role="assistant", tag="no_suggestion", content="Nothing to apply right now. Type **/hint** for examples.")
        st.rerun()

    if slash_cmd in {"cancel", "no"}:
        if st.session_state.get("chat_pending_suggestion") is not None:
            _clear_pending_chat_suggestion()
            _chat_add(
                role="assistant",
                tag="ack:cancel_suggestion",
                content="Okay — not changing anything. Type a size like **12x21** or **/hint**.",
            )
            st.rerun()
        _chat_add(role="assistant", tag="no_suggestion_to_cancel", content="Nothing to cancel. Type **/hint** for help.")
        st.rerun()

    if slash_cmd in {"next", "continue"} or bare_cmd in {"next", "continue"}:
        can_advance, reason = _chat_can_advance_step(st.session_state, step_key, book)
        if not can_advance:
            _chat_add(role="assistant", tag=f"blocked_next:{step_key}", content=reason)
            st.rerun()
        st.session_state["wizard_step"] = min(max_step_index, step_index + 1)
        st.rerun()

    tokens = _chat_command_tokens(raw)

    # Step-aware parsing so the user doesn't have to switch to the form.
    if step_key == "built_size":
        if "chat_built_size_has_style" not in st.session_state:
            st.session_state["chat_built_size_has_style"] = False
        if "chat_built_size_has_dims" not in st.session_state:
            st.session_state["chat_built_size_has_dims"] = False

        prev_style = str(st.session_state.get("demo_style") or "")
        prev_w = int(st.session_state.get("width_ft") or 0)
        prev_l = int(st.session_state.get("length_ft") or 0)

        style = _parse_style_label(raw)
        updated_style = False
        if style is not None:
            st.session_state["demo_style"] = style
            st.session_state["chat_built_size_has_style"] = True
            updated_style = True

        dims = _parse_dimensions_ft(raw)
        updated_dims = False
        if dims is not None:
            w, l = dims
            st.session_state["width_ft"] = w
            st.session_state["length_ft"] = l
            st.session_state["chat_built_size_has_dims"] = True
            updated_dims = True

        # Only clear a pending suggestion when the user actually changed style/size.
        if updated_style or updated_dims:
            _clear_pending_chat_suggestion()

        has_style = bool(st.session_state.get("chat_built_size_has_style"))
        has_dims = bool(st.session_state.get("chat_built_size_has_dims"))

        # Nothing parsed: show a gentle nudge.
        if style is None and dims is None:
            if _try_handle_with_ai_intent(
                step_key=step_key,
                raw=raw,
                step_index=step_index,
                max_step_index=max_step_index,
                book=book,
            ):
                return
            _chat_add(
                role="assistant",
                content=(
                    "I didn’t recognize that as a valid **style** or **size**.\n\n"
                    "Try **Regular 12x21**, **A-Frame 12x21**, or type **/hint**."
                ),
            )
            st.rerun()

        # We require both style + size to be explicitly provided (not just defaults).
        if not has_style:
            if dims is not None and (
                int(st.session_state.get("width_ft") or 0) != prev_w or int(st.session_state.get("length_ft") or 0) != prev_l
            ):
                _chat_add(
                    role="assistant",
                    content=(
                        f"Got it — **{int(st.session_state.get('width_ft') or 0)}x"
                        f"{int(st.session_state.get('length_ft') or 0)} ft**."
                    ),
                )
            _chat_add(
                role="assistant",
                content="Which style? **Regular**, **A-Frame Horizontal**, or **A-Frame Vertical**. (Type **/hint**.)",
            )
            st.rerun()

        if not has_dims:
            if style is not None and str(st.session_state.get("demo_style") or "") != prev_style:
                _chat_add(role="assistant", content=f"Got it — **{str(st.session_state.get('demo_style') or '')}**.")
            _chat_add(role="assistant", content="What size in feet? Example: **12x21**. (Type **/hint** for examples.)")
            st.rerun()

        style_now = str(st.session_state.get("demo_style") or "")
        width_now = int(st.session_state.get("width_ft") or 0)
        length_now = int(st.session_state.get("length_ft") or 0)
        allowed_lengths = [20, 25, 30, 35] if style_now == "A-Frame (Vertical)" else [21, 26, 31, 36]

        suggested_w = width_now if width_now in set(book.allowed_widths_ft) else _next_size_up(width_now, list(book.allowed_widths_ft))
        suggested_l = length_now if length_now in set(allowed_lengths) else _next_size_up(length_now, allowed_lengths)

        if suggested_w is not None and suggested_l is not None and (suggested_w != width_now or suggested_l != length_now):
            st.session_state["chat_pending_suggestion"] = {
                "kind": "built_size",
                "suggested": {"width_ft": int(suggested_w), "length_ft": int(suggested_l)},
            }
            _chat_add(
                role="assistant",
                content=(
                    "Per manufacturer rule, we price at the **next size up** when a size isn’t listed.\n\n"
                    f"Suggested priced size: **{int(suggested_w)}x{int(suggested_l)} ft**.\n\n"
                    "Type **/apply** to use that, or **/cancel** to keep what you typed."
                ),
            )
            st.rerun()

        can_advance, reason = _chat_can_advance_step(st.session_state, step_key, book)
        if can_advance:
            _chat_add(
                role="assistant",
                content=(
                    f"OK — you chose **{str(st.session_state.get('demo_style') or '')}**, "
                    f"**{int(st.session_state.get('width_ft') or 0)}x{int(st.session_state.get('length_ft') or 0)} ft**."
                ),
            )
            next_idx = min(max_step_index, step_index + 1)
            st.session_state["chat_last_auto_advance"] = {"from_step_index": int(step_index), "to_step_index": int(next_idx)}
            st.session_state["wizard_step"] = next_idx
            st.rerun()
        _chat_add(role="assistant", content=reason)
        st.rerun()

    if step_key == "leg_height":
        h = _parse_leg_height_ft(raw)
        if h is not None and h in set(book.allowed_leg_heights_ft):
            st.session_state["leg_height_ft"] = h
            _chat_add(role="assistant", tag="ack:leg_height", content=f"OK — you chose **{h} ft** leg height.")
            next_idx = min(max_step_index, step_index + 1)
            st.session_state["chat_last_auto_advance"] = {"from_step_index": int(step_index), "to_step_index": int(next_idx)}
            st.session_state["wizard_step"] = next_idx
            st.rerun()
        if _try_handle_with_ai_intent(step_key=step_key, raw=raw, step_index=step_index, max_step_index=max_step_index, book=book):
            return
        _chat_add(
            role="assistant",
            tag="help:leg_height",
            content=f"Pick one of the allowed leg heights: **{', '.join(str(x) for x in book.allowed_leg_heights_ft)}**.",
        )
        st.rerun()

    if step_key == "openings_types":
        stage = _openings_types_stage()
        add_count = _first_int_in_text(raw) or 1
        add_count = max(1, min(12, add_count))
        wants_add = "add" in tokens or "adding" in tokens
        wants_door = ("door" in tokens or "doors" in tokens) or ("walk" in tokens and "in" in tokens) or ("walkin" in tokens)
        wants_window = "window" in tokens or "windows" in tokens
        wants_garage = "garage" in tokens or ("roll" in tokens and "up" in tokens) or ("rollup" in tokens)

        # If the message clearly contains multiple opening categories with counts,
        # avoid applying "single category" heuristics first (prevents double-counting).
        door_count_guess = _find_count_for_keyword(raw, r"(?:walk[-\s]?in\s+)?doors?") if wants_door else None
        window_count_guess = _find_count_for_keyword(raw, r"windows?") if wants_window else None
        garage_count_guess = _find_count_for_keyword(raw, r"(?:garage|roll[-\s]?up)s?(?:\s+door)?s?") if wants_garage else None
        multi_categories = sum(1 for x in (door_count_guess, window_count_guess, garage_count_guess) if isinstance(x, int))

        # "none openings" / "no openings" clears everything.
        if (tokens & {"none", "no", "nope"}) and (("openings" in tokens) or ("opening" in tokens)):
            st.session_state["walk_in_door_type"] = "None"
            st.session_state["walk_in_door_count"] = 0
            st.session_state["window_size"] = "None"
            st.session_state["window_count"] = 0
            st.session_state["garage_door_type"] = "None"
            st.session_state["garage_door_count"] = 0
            st.session_state["openings"] = []
            st.session_state["opening_seq"] = 1
            _set_openings_types_stage("doors")
            _chat_add(role="assistant", tag="ack:openings_types_none", content="OK — no openings.")
            next_idx = min(max_step_index, step_index + 1)
            st.session_state["chat_last_auto_advance"] = {"from_step_index": int(step_index), "to_step_index": int(next_idx)}
            st.session_state["wizard_step"] = next_idx
            st.rerun()

        # Stage-local "none": move through doors → windows → garage.
        if (tokens & {"none", "no", "nope", "skip"}) and not (wants_door or wants_window or wants_garage):
            if stage == "doors":
                st.session_state["walk_in_door_type"] = "None"
                st.session_state["walk_in_door_count"] = 0
                _set_openings_types_stage("windows")
                _chat_add(role="assistant", content="OK — no doors.")
                _chat_add(role="assistant", content=_chat_prompt_for_current_step("openings_types"))
                st.rerun()
            if stage == "windows":
                st.session_state["window_size"] = "None"
                st.session_state["window_count"] = 0
                _set_openings_types_stage("garage")
                _chat_add(role="assistant", content="OK — no windows.")
                _chat_add(role="assistant", content=_chat_prompt_for_current_step("openings_types"))
                st.rerun()
            # stage == "garage"
            st.session_state["garage_door_type"] = "None"
            st.session_state["garage_door_count"] = 0
            _set_openings_types_stage("doors")
            _chat_add(role="assistant", content="OK — no garage doors.")
            next_idx = min(max_step_index, step_index + 1)
            st.session_state["chat_last_auto_advance"] = {"from_step_index": int(step_index), "to_step_index": int(next_idx)}
            st.session_state["wizard_step"] = next_idx
            st.rerun()

        updated_any = False
        handled_multi = False
        size_token = _parse_size_token(raw)

        # Single-category “type” phrasing even without an explicit "add".
        if size_token and multi_categories < 2:
            if (("roll-up" in raw.lower()) or ("rollup" in raw.lower()) or wants_garage) and size_token in ROLL_UP_DOOR_OPTIONS:
                st.session_state["garage_door_type"] = "Roll-up"
                st.session_state["garage_door_size"] = size_token
                if wants_add:
                    st.session_state["garage_door_count"] = int(st.session_state.get("garage_door_count") or 0) + min(8, int(add_count))
                else:
                    st.session_state["garage_door_count"] = min(8, int(add_count))
                updated_any = True
                _chat_add(
                    role="assistant",
                    tag="ack:openings_types_rollup",
                    content=f"OK — you chose **Roll-up {size_token}** × **{int(st.session_state.get('garage_door_count') or 0)}**.",
                )
            if wants_window and size_token in WINDOW_OPTIONS:
                st.session_state["window_size"] = size_token
                if wants_add:
                    st.session_state["window_count"] = int(st.session_state.get("window_count") or 0) + int(add_count)
                else:
                    st.session_state["window_count"] = int(add_count)
                updated_any = True
                _chat_add(
                    role="assistant",
                    tag="ack:openings_types_window_size",
                    content=f"OK — you chose windows **{size_token}** × **{int(st.session_state.get('window_count') or 0)}**.",
                )

        # Multi-intent: handle doors + windows (and/or garage) in one message.
        if wants_door or wants_window or wants_garage:
            door_count = _find_count_for_keyword(raw, r"(?:walk[-\s]?in\s+)?doors?")
            window_count = _find_count_for_keyword(raw, r"windows?")
            garage_count = _find_count_for_keyword(raw, r"(?:garage|roll[-\s]?up)s?(?:\s+door)?s?")
            categories = sum(1 for x in (door_count, window_count, garage_count) if isinstance(x, int))
            if categories >= 2:
                # Doors
                if isinstance(door_count, int):
                    door_labels = ["None"] + _available_accessory_labels(book, WALK_IN_DOOR_OPTIONS)
                    picked = _pick_walk_in_label_from_text(raw, door_labels)
                    if picked:
                        st.session_state["walk_in_door_type"] = picked
                    elif st.session_state.get("walk_in_door_type") in ("", "None") and len(door_labels) > 1:
                        st.session_state["walk_in_door_type"] = (
                            "Standard 36x80" if "Standard 36x80" in door_labels else door_labels[1]
                        )
                    if wants_add:
                        st.session_state["walk_in_door_count"] = int(st.session_state.get("walk_in_door_count") or 0) + int(door_count)
                    else:
                        st.session_state["walk_in_door_count"] = int(door_count)

                # Windows
                if isinstance(window_count, int):
                    win_labels = ["None"] + _available_accessory_labels(book, WINDOW_OPTIONS)
                    if st.session_state.get("window_size") in ("", "None"):
                        if size_token and size_token in WINDOW_OPTIONS:
                            st.session_state["window_size"] = size_token
                        elif len(win_labels) > 1:
                            st.session_state["window_size"] = win_labels[1]
                    if str(st.session_state.get("window_size") or "None") == "None" and len(win_labels) > 1:
                        _chat_add(
                            role="assistant",
                            content=f"How big should the windows be? Choose one: {', '.join(f'**{x}**' for x in win_labels if x != 'None')}.",
                        )
                        st.rerun()
                    if wants_add:
                        st.session_state["window_count"] = int(st.session_state.get("window_count") or 0) + int(window_count)
                    else:
                        st.session_state["window_count"] = int(window_count)

                # Garage
                if isinstance(garage_count, int):
                    if st.session_state.get("garage_door_type") in ("", "None"):
                        st.session_state["garage_door_type"] = "Roll-up"
                    if size_token and size_token in ROLL_UP_DOOR_OPTIONS:
                        st.session_state["garage_door_size"] = size_token
                    if wants_add:
                        st.session_state["garage_door_count"] = int(st.session_state.get("garage_door_count") or 0) + min(8, int(garage_count))
                    else:
                        st.session_state["garage_door_count"] = min(8, int(garage_count))

                parts = []
                if isinstance(door_count, int):
                    parts.append(
                        f"Doors **{st.session_state.get('walk_in_door_type') or 'Door'}** × **{int(st.session_state.get('walk_in_door_count') or 0)}**"
                    )
                if isinstance(window_count, int):
                    parts.append(
                        f"Windows **{st.session_state.get('window_size') or 'Window'}** × **{int(st.session_state.get('window_count') or 0)}**"
                    )
                if isinstance(garage_count, int):
                    parts.append(
                        f"Garage **{st.session_state.get('garage_door_type') or 'Roll-up'} {st.session_state.get('garage_door_size') or ''}** × **{int(st.session_state.get('garage_door_count') or 0)}**"
                    )
                updated_any = True
                handled_multi = True
                _chat_add(role="assistant", content=f"Awesome — got it. {', '.join(parts)}.")

        # Walk-in door phrasing without "add" (e.g. "standard x3", "standard walk-in door 3").
        if (not handled_multi) and (("standard" in tokens) or wants_door) and not wants_add and not wants_window and not wants_garage:
            door_labels = ["None"] + _available_accessory_labels(book, WALK_IN_DOOR_OPTIONS)
            picked = _pick_walk_in_label_from_text(raw, door_labels)
            if picked:
                st.session_state["walk_in_door_type"] = picked
                st.session_state["walk_in_door_count"] = int(add_count)
                updated_any = True
                _chat_add(
                    role="assistant",
                    tag="ack:openings_types_door_non_add",
                    content=f"OK — you chose **{picked}** × **{int(st.session_state.get('walk_in_door_count') or 0)}**.",
                )

        # "add X" for a single category.
        if (not handled_multi) and wants_add and (wants_door or wants_window or wants_garage):
            if wants_door:
                door_labels = ["None"] + _available_accessory_labels(book, WALK_IN_DOOR_OPTIONS)
                picked = _pick_walk_in_label_from_text(raw, door_labels)
                if picked:
                    st.session_state["walk_in_door_type"] = picked
                elif st.session_state.get("walk_in_door_type") in ("", "None") and len(door_labels) > 1:
                    st.session_state["walk_in_door_type"] = door_labels[1]
                st.session_state["walk_in_door_count"] = int(st.session_state.get("walk_in_door_count") or 0) + int(add_count)

            if wants_window:
                win_labels = ["None"] + _available_accessory_labels(book, WINDOW_OPTIONS)
                if st.session_state.get("window_size") in ("", "None") and len(win_labels) > 1:
                    st.session_state["window_size"] = win_labels[1]
                st.session_state["window_count"] = int(st.session_state.get("window_count") or 0) + int(add_count)

            if wants_garage:
                if st.session_state.get("garage_door_type") in ("", "None"):
                    st.session_state["garage_door_type"] = "Roll-up"
                st.session_state["garage_door_count"] = int(st.session_state.get("garage_door_count") or 0) + min(8, int(add_count))

            updated_any = True
            _chat_add(role="assistant", tag="ack:openings_types_add", content="OK — openings updated.")

        # If we updated anything, advance the micro-step.
        if updated_any:
            addressed_doors = bool(wants_door or ("standard" in tokens))
            addressed_windows = bool(wants_window)
            addressed_garage = bool(wants_garage)

            if stage == "doors":
                # If they already addressed windows in the same message, skip straight to garage.
                if addressed_garage and addressed_windows:
                    _set_openings_types_stage("doors")
                    next_idx = min(max_step_index, step_index + 1)
                    st.session_state["chat_last_auto_advance"] = {"from_step_index": int(step_index), "to_step_index": int(next_idx)}
                    st.session_state["wizard_step"] = next_idx
                    st.rerun()
                _set_openings_types_stage("garage" if addressed_windows else "windows")
                _chat_add(role="assistant", content=_chat_prompt_for_current_step("openings_types"))
                st.rerun()

            if stage == "windows":
                if addressed_garage:
                    _set_openings_types_stage("doors")
                    next_idx = min(max_step_index, step_index + 1)
                    st.session_state["chat_last_auto_advance"] = {"from_step_index": int(step_index), "to_step_index": int(next_idx)}
                    st.session_state["wizard_step"] = next_idx
                    st.rerun()
                _set_openings_types_stage("garage")
                _chat_add(role="assistant", content=_chat_prompt_for_current_step("openings_types"))
                st.rerun()

            # stage == "garage"
            _set_openings_types_stage("doors")
            next_idx = min(max_step_index, step_index + 1)
            st.session_state["chat_last_auto_advance"] = {"from_step_index": int(step_index), "to_step_index": int(next_idx)}
            st.session_state["wizard_step"] = next_idx
            st.rerun()

        # Coaching when the user mentions openings but we can't map it.
        if wants_door or wants_window or wants_garage:
            if wants_door and _first_int_in_text(raw) is None and not _contains_any(raw, ["standard", "six panel", "nine lite"]):
                top = _available_accessory_labels(book, WALK_IN_DOOR_OPTIONS)[:3]
                if top:
                    _chat_add(
                        role="assistant",
                        content=(
                            "Sure — let’s add doors.\n\n"
                            f"Pick one: {', '.join(f'**{x}**' for x in top)}\n\n"
                            "Then tell me how many (example: **standard x2**)."
                        ),
                    )
                    st.rerun()
            if _try_handle_with_ai_intent(step_key=step_key, raw=raw, step_index=step_index, max_step_index=max_step_index, book=book):
                return
            _chat_add(
                role="assistant",
                tag="help:openings_types",
                content="I can help add openings. Try: **standard door x2**, **add 2 windows 24x36**, or **2 roll-up 10x8** — or type **/hint**.",
            )
            st.rerun()

    if step_key == "openings_placement":
        if tokens & {"yes", "y", "yeah", "yep", "sure"}:
            _chat_add(
                role="assistant",
                content="OK — tell me **what** and **where** (example: **door left 3**, **window right 5**, **garage front 0**), or type **skip**.",
            )
            st.rerun()

        if tokens & {"skip"}:
            st.session_state["openings"] = []
            st.session_state["opening_seq"] = 1
            _chat_add(role="assistant", tag="ack:openings_placement_skip", content="OK — skipping placement.")
            next_idx = min(max_step_index, step_index + 1)
            st.session_state["chat_last_auto_advance"] = {"from_step_index": int(step_index), "to_step_index": int(next_idx)}
            st.session_state["wizard_step"] = next_idx
            st.rerun()

        if tokens & {"none", "no", "nope"}:
            st.session_state["openings"] = []
            st.session_state["opening_seq"] = 1
            _chat_add(role="assistant", tag="ack:openings_placement_none", content="OK — no explicit placements.")
            next_idx = min(max_step_index, step_index + 1)
            st.session_state["chat_last_auto_advance"] = {"from_step_index": int(step_index), "to_step_index": int(next_idx)}
            st.session_state["wizard_step"] = next_idx
            st.rerun()

        bulk = _parse_opening_bulk_placement_instruction(raw)
        if bulk:
            kind = str(bulk.get("kind") or "door")
            side = str(bulk.get("side") or "front")
            count = int(bulk.get("count") or 0)
            if count == -1:
                # "all doors left" → use current counts from the Types step.
                if kind == "door":
                    count = int(st.session_state.get("walk_in_door_count") or 0)
                elif kind == "window":
                    count = int(st.session_state.get("window_count") or 0)
                elif kind == "garage":
                    count = int(st.session_state.get("garage_door_count") or 0)
            count = max(0, min(12, count))
            if count <= 0:
                _chat_add(role="assistant", content="How many should I place? Example: **3 doors left**.")
                st.rerun()

            if "openings" not in st.session_state or not isinstance(st.session_state.get("openings"), list):
                st.session_state["openings"] = []
            if "opening_seq" not in st.session_state:
                st.session_state["opening_seq"] = 1

            w = int(st.session_state.get("width_ft") or 0)
            l = int(st.session_state.get("length_ft") or 0)
            offsets = _bulk_offsets_for_wall(side=side, count=count, width_ft=w, length_ft=l)

            for off in offsets:
                oid = int(st.session_state.get("opening_seq") or 1)
                openings_list = st.session_state.get("openings")
                if isinstance(openings_list, list):
                    openings_list.append({"id": oid, "kind": kind, "side": side, "offset_ft": int(off)})
                st.session_state["opening_seq"] = oid + 1

            _chat_add(
                role="assistant",
                tag="ack:openings_placement_bulk",
                content=f"Placed **{count}** {kind}(s) on **{side}**. Add more, or type **/next**.",
            )
            st.rerun()

        placement = _parse_opening_placement_instruction(raw)
        if placement:
            if "openings" not in st.session_state or not isinstance(st.session_state.get("openings"), list):
                st.session_state["openings"] = []
            if "opening_seq" not in st.session_state:
                st.session_state["opening_seq"] = 1

            oid = int(st.session_state.get("opening_seq") or 1)
            openings_list = st.session_state.get("openings")
            if isinstance(openings_list, list):
                openings_list.append(
                    {
                        "id": oid,
                        "kind": str(placement.get("kind") or "door"),
                        "side": str(placement.get("side") or "front"),
                        "offset_ft": int(placement.get("offset_ft") or 0),
                    }
                )
            st.session_state["opening_seq"] = oid + 1
            _chat_add(
                role="assistant",
                tag="ack:openings_placement_add",
                content=(
                    f"Placed **{placement['kind']}** on **{placement['side']}** at **{int(placement['offset_ft'])} ft**. "
                    "Add more, or type **/next**."
                ),
            )
            st.rerun()

        if tokens & {"door", "doors", "window", "windows", "garage"}:
            if _try_handle_with_ai_intent(step_key=step_key, raw=raw, step_index=step_index, max_step_index=max_step_index, book=book):
                return
            _chat_add(
                role="assistant",
                tag="help:openings_placement",
                content="For placement, try: **door left 3**, **window right 5**, or **garage front 0** — or type **skip**.",
            )
            st.rerun()

    if step_key == "options":
        pending_place = st.session_state.get("chat_pending_option_placement")
        if isinstance(pending_place, dict) and isinstance(pending_place.get("codes"), list):
            codes = [c for c in list(pending_place.get("codes") or []) if isinstance(c, str) and c]
            if tokens & {"skip", "none", "no"}:
                st.session_state.pop("chat_pending_option_placement", None)
                _chat_add(role="assistant", content="OK — skipping option placement. Next step.")
                next_idx = min(max_step_index, step_index + 1)
                st.session_state["chat_last_auto_advance"] = {"from_step_index": int(step_index), "to_step_index": int(next_idx)}
                st.session_state["wizard_step"] = next_idx
                st.rerun()
            placement = _parse_section_placement(raw)
            if placement is not None:
                for code in codes:
                    st.session_state[f"placement_{code}"] = placement
                st.session_state.pop("chat_pending_option_placement", None)
                _chat_add(
                    role="assistant",
                    content=f"Placed {', '.join(f'**{c}**' for c in codes)} on **{placement.value}**. Next step.",
                )
                next_idx = min(max_step_index, step_index + 1)
                st.session_state["chat_last_auto_advance"] = {"from_step_index": int(step_index), "to_step_index": int(next_idx)}
                st.session_state["wizard_step"] = next_idx
                st.rerun()
            _chat_add(role="assistant", content="Pick a placement: **front**, **rear**, **left**, **right** — or type **skip**.")
            st.rerun()

        stage = _options_stage()
        allowed_codes = set(_available_option_codes(book))

        def _clear_options_state() -> None:
            st.session_state["include_ground_certification"] = False
            st.session_state["selected_option_codes"] = []
            st.session_state["extra_panel_count"] = 0
            # Clear any remembered placement_* keys (dynamic UI).
            placement_keys = [k for k in st.session_state if isinstance(k, str) and k.startswith("placement_")]
            for k in placement_keys:
                try:
                    del st.session_state[k]
                except Exception:
                    pass

        # Micro-step 1: ground certification (yes/no).
        if stage == "ground_certification":
            no_options_overall = (
                (("none" in tokens) or (("option" in tokens or "options" in tokens) and (tokens & {"no", "none", "nope"})))
                and not ("ground" in tokens)
                and not ({"trim", "double", "leg"} & tokens)
            )
            if no_options_overall:
                _clear_options_state()
                _set_options_stage("ground_certification")
                _chat_add(role="assistant", tag="ack:options_none", content="OK — no options.")
                next_idx = min(max_step_index, step_index + 1)
                st.session_state["chat_last_auto_advance"] = {"from_step_index": int(step_index), "to_step_index": int(next_idx)}
                st.session_state["wizard_step"] = next_idx
                st.rerun()

            said_yes = bool(tokens & {"yes", "y", "yeah", "yep", "sure"})
            said_no = bool(tokens & {"no", "nope", "nah"})
            mentions_ground = bool("ground" in tokens and ("cert" in tokens or "certification" in tokens))
            if said_yes or said_no or mentions_ground:
                if said_no or (mentions_ground and (tokens & {"no", "none", "remove", "off"})):
                    st.session_state["include_ground_certification"] = False
                    _chat_add(role="assistant", content="OK — no ground certification.")
                else:
                    st.session_state["include_ground_certification"] = True
                    _chat_add(role="assistant", content="Added **ground certification**.")
                _set_options_stage("trim_or_double_leg")
                _chat_add(role="assistant", content=_chat_prompt_for_current_step("options"))
                st.rerun()

            # If they skip directly to codes ("j trim", "double leg"), assume no ground certification and proceed.
            selected_direct = _match_option_codes_from_text(raw, allowed_codes)
            if selected_direct:
                if "include_ground_certification" not in st.session_state:
                    st.session_state["include_ground_certification"] = False
                _set_options_stage("trim_or_double_leg")
                stage = "trim_or_double_leg"
            else:
                if _try_handle_with_ai_intent(step_key=step_key, raw=raw, step_index=step_index, max_step_index=max_step_index, book=book):
                    return
                _chat_add(role="assistant", content="Reply **yes** or **no** for ground certification (or type **/hint**).")
                st.rerun()

        # Micro-step 2: J-Trim / Double Leg.
        if stage == "trim_or_double_leg":
            if tokens & {"none", "no", "nope"} and not ("ground" in tokens):
                _set_options_stage("ground_certification")
                _chat_add(role="assistant", content="OK — no additional options.")
                next_idx = min(max_step_index, step_index + 1)
                st.session_state["chat_last_auto_advance"] = {"from_step_index": int(step_index), "to_step_index": int(next_idx)}
                st.session_state["wizard_step"] = next_idx
                st.rerun()

            # Ground certification toggle still allowed here.
            if "ground" in tokens and ("cert" in tokens or "certification" in tokens):
                if tokens & {"no", "none", "remove", "off"}:
                    st.session_state["include_ground_certification"] = False
                    _chat_add(role="assistant", content="OK — removed ground certification.")
                else:
                    st.session_state["include_ground_certification"] = True
                    _chat_add(role="assistant", content="Added **ground certification**.")

            # Allow typing option codes directly, including spaced variants like "j trim" and aliases like "double leg".
            selected = _match_option_codes_from_text(raw, allowed_codes)
            if selected:
                existing = st.session_state.get("selected_option_codes")
                existing_list = list(existing) if isinstance(existing, list) else []
                for c in selected:
                    if c not in existing_list:
                        existing_list.append(c)
                st.session_state["selected_option_codes"] = existing_list
                _chat_add(
                    role="assistant",
                    tag="ack:options_codes",
                    content=f"OK — added option(s): {', '.join(f'**{c}**' for c in selected)}.",
                )

                # Offer an optional placement prompt (since the UI supports placement per option).
                if len(selected) == 1 and st.session_state.get(f"placement_{selected[0]}") is None:
                    st.session_state["chat_pending_option_placement"] = {"codes": list(selected)}
                    _chat_add(
                        role="assistant",
                        content="Where should we place that? Reply **front/rear/left/right**, or **skip**.",
                    )
                st.rerun()

                _set_options_stage("ground_certification")
                next_idx = min(max_step_index, step_index + 1)
                st.session_state["chat_last_auto_advance"] = {"from_step_index": int(step_index), "to_step_index": int(next_idx)}
                st.session_state["wizard_step"] = next_idx
                st.rerun()

            if _try_handle_with_ai_intent(step_key=step_key, raw=raw, step_index=step_index, max_step_index=max_step_index, book=book):
                return
            _chat_add(role="assistant", content="Type **j trim**, **double leg**, or **none** — or **/hint** for details.")
            st.rerun()

    if step_key == "colors":
        if tokens & {"skip", "none"}:
            _chat_add(role="assistant", tag="ack:colors_skip", content="OK — keeping default colors.")
            next_idx = min(max_step_index, step_index + 1)
            st.session_state["chat_last_auto_advance"] = {"from_step_index": int(step_index), "to_step_index": int(next_idx)}
            st.session_state["wizard_step"] = next_idx
            st.rerun()
        allowed_colors = ["White", "Gray", "Black", "Tan", "Sandstone", "Brown", "Red", "Burgundy", "Blue", "Green"]
        assigns = _parse_color_assignments(raw, allowed_colors)
        if assigns:
            for k, v in assigns.items():
                st.session_state[k] = v
            missing = [k for k in ("roof_color", "trim_color", "side_color") if k not in assigns]
            if not missing:
                _chat_add(
                    role="assistant",
                    content=f"Nice — colors set (Roof **{st.session_state.get('roof_color')}**, Trim **{st.session_state.get('trim_color')}**, Sides **{st.session_state.get('side_color')}**).",
                )
                next_idx = min(max_step_index, step_index + 1)
                st.session_state["chat_last_auto_advance"] = {"from_step_index": int(step_index), "to_step_index": int(next_idx)}
                st.session_state["wizard_step"] = next_idx
                st.rerun()
            prompt_bits = []
            if "roof_color" in missing:
                prompt_bits.append("roof")
            if "trim_color" in missing:
                prompt_bits.append("trim")
            if "side_color" in missing:
                prompt_bits.append("sides")
            _chat_add(
                role="assistant",
                content=f"Got it. What {', '.join(prompt_bits)} color(s) do you want? Example: **trim black**.",
            )
            st.rerun()

    if step_key == "notes":
        if tokens & {"none", "no", "nope"}:
            st.session_state["internal_notes"] = ""
            _chat_add(role="assistant", tag="ack:notes_none", content="OK — no notes.")
            next_idx = min(max_step_index, step_index + 1)
            st.session_state["chat_last_auto_advance"] = {"from_step_index": int(step_index), "to_step_index": int(next_idx)}
            st.session_state["wizard_step"] = next_idx
            st.rerun()
        # Treat any other text as notes for this step.
        st.session_state["internal_notes"] = raw
        _chat_add(role="assistant", tag="ack:notes_saved", content="OK — notes saved.")
        next_idx = min(max_step_index, step_index + 1)
        st.session_state["chat_last_auto_advance"] = {"from_step_index": int(step_index), "to_step_index": int(next_idx)}
        st.session_state["wizard_step"] = next_idx
        st.rerun()

    # Optional: last-chance AI intent recognition for steps with the most freeform input.
    if step_key in {"built_size", "leg_height", "openings_types", "openings_placement", "options", "colors"}:
        _try_handle_with_ai_intent(step_key=step_key, raw=raw, step_index=step_index, max_step_index=max_step_index, book=book)

    _chat_add(
        role="assistant",
        tag="chat_help",
        content="Type **/next** to continue, or **/hint** to see the full menu for this step.",
    )
    st.rerun()


def _render_chat_panel(*, step_key: str, step_index: int, max_step_index: int, book: PriceBook) -> None:
    st.subheader("Conversation")

    # Seed greeting once.
    if not _chat_messages():
        _chat_add(
            role="assistant",
            tag="welcome",
            content=(
                "Hi — I’ll guide you through a quick quote.\n\n"
                "First, what’s your **name** and **email**?"
            ),
        )

    # Show a step prompt on step entry (and when returning to a step).
    if bool(st.session_state.get("lead_captured")):
        last_prompted = st.session_state.get("chat_last_prompted_step")
        if last_prompted != step_key:
            _chat_queue_step_prompt(step_key)

    left, right = st.columns([3, 2], gap="large")
    with left:
        with st.container(height=560, border=True):
            now_ms = int(time.time() * 1000)
            next_visible_at_ms: Optional[int] = None
            for msg in _chat_messages():
                role = msg.get("role")
                content = msg.get("content")
                visible_at_ms = int(msg.get("visible_at_ms") or msg.get("created_at_ms") or 0)
                if visible_at_ms > now_ms:
                    next_visible_at_ms = visible_at_ms if next_visible_at_ms is None else min(next_visible_at_ms, visible_at_ms)
                    continue
                if role in ("assistant", "user") and isinstance(content, str):
                    with st.chat_message(role):
                        st.markdown(content)
            st.markdown('<div id="chat-scroll-anchor"></div>', unsafe_allow_html=True)

        user_text = st.chat_input(_chat_input_placeholder(step_key), disabled=next_visible_at_ms is not None)
        if user_text is not None:
            _handle_chat_input(
                text=user_text,
                step_key=step_key,
                step_index=step_index,
                max_step_index=max_step_index,
                book=book,
            )

    with right:
        if bool(st.session_state.get("lead_captured")):
            _render_chat_action_card(step_key=step_key, step_index=step_index, max_step_index=max_step_index, book=book)
        else:
            st.info("After you enter name + email, I’ll show guided controls here.")

    _maybe_autoscroll_chat()
    if next_visible_at_ms is not None:
        wait_ms = max(0, int(next_visible_at_ms) - int(time.time() * 1000))
        time.sleep(min(0.35, wait_ms / 1000.0))
        st.rerun()


# endregion lead capture + chat

# region agent log
_AGENT_LOG_PATH = "/Users/cameron/STEVEN DEMO/.cursor/debug.log"
_AGENT_RUN_ID = "stacking-pre"


def _agent_log(*, hypothesis_id: str, location: str, message: str, data: dict) -> None:
    try:
        payload = {
            "sessionId": "debug-session",
            "runId": _AGENT_RUN_ID,
            "hypothesisId": hypothesis_id,
            "location": location,
            "message": message,
            "data": data,
            "timestamp": int(time.time() * 1000),
        }
        with open(_AGENT_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload) + "\n")
    except Exception:
        # Never let logging break the demo UI.
        pass


# endregion agent log


WALK_IN_DOOR_OPTIONS = {
    "Standard 36x80": "WALK_IN_DOOR_STANDARD_36X80",
    "Six Panel 36x80": "WALK_IN_DOOR_SIX_PANEL_36X80",
    "Six Panel w/ Window 36x80": "WALK_IN_DOOR_SIX_PANEL_WINDOW_36X80",
    "Nine Lite 36x80": "WALK_IN_DOOR_NINE_LITE_36X80",
}

WINDOW_OPTIONS = {
    "24x36": "WINDOW_24X36",
    "30x36": "WINDOW_30X36",
}

ROLL_UP_DOOR_OPTIONS = {
    "6x6": "ROLL_UP_DOOR_6X6",
    "6x7": "ROLL_UP_DOOR_6X7",
    "8x7": "ROLL_UP_DOOR_8X7",
    "9x8": "ROLL_UP_DOOR_9X8",
    "10x8": "ROLL_UP_DOOR_10X8",
    "10x10": "ROLL_UP_DOOR_10X10",
}


def _available_accessory_labels(book: PriceBook, options: dict[str, str]) -> list[str]:
    return [label for label, code in options.items() if code in book.option_prices_by_length_usd]


def _pick_default_title(titles: list[str], prefer: str) -> str:
    for title in titles:
        if prefer in title.upper():
            return title
    return titles[0]


def _find_r29_normalized_path() -> Path:
    repo_root = Path(__file__).resolve().parent
    candidate_out_dirs = [
        repo_root / "out",
        repo_root / "pricebooks" / "out",
    ]

    paths: list[Path] = []
    searched: list[str] = []
    for out_dir in candidate_out_dirs:
        searched.append(str(out_dir))
        if out_dir.exists():
            paths = find_normalized_pricebooks(out_dir)
            if paths:
                break

    if not paths:
        raise FileNotFoundError(
            "No normalized pricebooks found. Searched: "
            + ", ".join(searched)
            + ". Run extraction + normalize first."
        )

    for path in paths:
        candidate = load_normalized_pricebook(path)
        if candidate.status == "ok" and "R29" in candidate.source.upper():
            return path

    raise FileNotFoundError("Could not locate a usable R29 normalized pricebook under: " + ", ".join(searched))


def _load_pricebook_from_extracted() -> PriceBook:
    """
    Load the demo pricebook (R29 NW) and cache the resulting PriceBook across reruns.

    Caching here is for performance only (avoid re-reading/parsing the JSON on every rerun).
    Wizard progress and user inputs are persisted via `st.session_state`, not caching.
    """
    try:
        normalized_path = _find_r29_normalized_path()
        mtime = normalized_path.stat().st_mtime
        book = _load_pricebook_from_extracted_cached(str(normalized_path), mtime)
    except Exception as exc:
        st.error(str(exc))
        st.stop()

    # region agent log
    # Hypothesis B: accessory codes missing in loaded pricebook, causing the UI to force selections back to None.
    accessory_codes = [
        "WINDOW_24X36",
        "WINDOW_30X36",
        "WALK_IN_DOOR_STANDARD_36X80",
        "WALK_IN_DOOR_SIX_PANEL_36X80",
        "ROLL_UP_DOOR_10X8",
        "EXTRA_PANEL",
    ]
    _agent_log(
        hypothesis_id="B",
        location="local_demo_app.py:_load_pricebook_from_extracted",
        message="Loaded pricebook accessory code availability",
        data={
            "revision": book.revision,
            "available_accessory_codes": {
                code: (code in book.option_prices_by_length_usd) for code in accessory_codes
            },
            "option_code_count": len(book.option_prices_by_length_usd),
        },
    )
    # endregion agent log

    return book


@st.cache_resource
def _load_pricebook_from_extracted_cached(normalized_path_str: str, normalized_path_mtime: float) -> PriceBook:
    """
    Cached loader for the demo PriceBook.

    Important:
    - Do NOT call Streamlit UI functions (`st.*`) in cached code.
    - Cache invalidation is driven by `normalized_path_mtime`.
    """
    _ = normalized_path_mtime  # included only to invalidate cache when the file changes
    normalized_path = Path(normalized_path_str)
    normalized = load_normalized_pricebook(normalized_path)
    return build_demo_pricebook_r29(normalized)


def _restore_checkpoint(step_index: int, defaults: Mapping[str, object]) -> None:
    """
    Restore a previously saved wizard checkpoint for the given step index.

    This is used when navigating Back/Next so that widgets rehydrate to the user's last-known
    selections, even if Streamlit drops some widget-backed session_state keys between reruns.
    """
    checkpoints = st.session_state.get("wizard_checkpoints")
    if not isinstance(checkpoints, dict):
        return
    checkpoint = checkpoints.get(str(step_index))
    if not isinstance(checkpoint, dict):
        return

    for k, v in checkpoint.items():
        st.session_state[k] = v

    # Keep shadow state consistent with restored values, without clobbering unrelated keys.
    #
    # IMPORTANT: Do NOT call `_sync_shadow_state(defaults)` here, because that updates the shadow
    # snapshot from *all* persisted keys. If Streamlit has silently reset a non-active field
    # (e.g. leg_height_ft) right before navigation, a full sync would overwrite the last-known-good
    # value in shadow and cause quote totals/line-items to "disappear" on later steps.
    shadow = st.session_state.get("_shadow_state")
    if not isinstance(shadow, dict):
        shadow = {}
    new_shadow = dict(shadow)
    for k in checkpoint.keys():
        if k in _PERSIST_STATE_KEYS:
            new_shadow[k] = st.session_state.get(k, defaults.get(k))
    st.session_state["_shadow_state"] = {k: new_shadow.get(k, defaults.get(k)) for k in _PERSIST_STATE_KEYS}


def _default_state(book: PriceBook) -> dict[str, object]:
    return {
        "demo_style": "A-Frame (Horizontal)",
        "demo_style_prev": "A-Frame (Horizontal)",
        "width_ft": book.allowed_widths_ft[0] if book.allowed_widths_ft else 12,
        # Default to the horizontal grid so the first render doesn't crash when style is horizontal.
        "length_ft": 21,
        "leg_height_ft": book.allowed_leg_heights_ft[0] if book.allowed_leg_heights_ft else 6,
        "include_ground_certification": False,
        "selected_option_codes": [],
        "lean_to_enabled": False,
        "lean_to_placement": SectionPlacement.RIGHT,
        "lean_to_width_ft": book.allowed_widths_ft[0] if book.allowed_widths_ft else 12,
        "lean_to_length_ft": book.allowed_lengths_ft[0] if book.allowed_lengths_ft else 20,
        "closed_sides": [],
        "closed_ends": [],
        "walk_in_door_type": "None",
        "walk_in_door_count": 0,
        "window_size": "None",
        "window_count": 0,
        "garage_door_type": "None",
        "garage_door_size": "10x8",
        "garage_door_count": 0,
        # Explicit opening placement (v1): list of rows like {"id": 1, "kind": "door|window|garage", "side": "front|back|left|right", "offset_ft": 0}
        "openings": [],
        # Monotonic ID source for openings; persisted to avoid widget-key collisions across reruns.
        "opening_seq": 1,
        "extra_panel_count": 0,
        "roof_color": "White",
        "trim_color": "White",
        "side_color": "White",
        # Payment/discount terms (demo parity with vendor PDF)
        "manufacturer_discount_pct": 0.0,
        # Stored as a percent (0..100). Vendor screenshot uses 18%.
        "downpayment_pct": 18.0,
        "internal_notes": "",
        "wizard_step": 0,
    }


def _init_state(book: PriceBook) -> None:
    defaults = _default_state(book)
    created_keys: list[str] = []
    for key, value in defaults.items():
        if key not in st.session_state:
            # Prefer restoring from shadow state when Streamlit drops widget-backed keys.
            shadow = st.session_state.get("_shadow_state")
            if isinstance(shadow, dict) and key in shadow:
                st.session_state[key] = shadow[key]
            else:
                st.session_state[key] = value
            created_keys.append(key)

    # IMPORTANT: do NOT "sync all keys into shadow" on every rerun.
    #
    # When Streamlit resets a widget value (instead of dropping the key), a full sync here would
    # overwrite the last-known-good shadow snapshot with the reset/default value. That is exactly
    # the failure mode where totals suddenly drop when you toggle an unrelated option (e.g. J_TRIM)
    # and Streamlit silently resets non-active fields.
    shadow = st.session_state.get("_shadow_state")
    if not isinstance(shadow, dict):
        shadow = {}
    st.session_state["_shadow_state"] = {k: shadow.get(k, st.session_state.get(k, defaults.get(k))) for k in _PERSIST_STATE_KEYS}

    # region agent log
    _agent_log(
        hypothesis_id="F",
        location="local_demo_app.py:_init_state",
        message="State init pass",
        data={
            "created_keys": created_keys,
            "wizard_step": int(st.session_state.get("wizard_step") or 0),
        },
    )
    # endregion agent log


_PERSIST_STATE_KEYS: tuple[str, ...] = (
    "demo_style",
    "demo_style_prev",
    "width_ft",
    "length_ft",
    "leg_height_ft",
    "include_ground_certification",
    "selected_option_codes",
    "walk_in_door_type",
    "walk_in_door_count",
    "window_size",
    "window_count",
    "garage_door_type",
    "garage_door_size",
    "garage_door_count",
    "openings",
    "opening_seq",
    "extra_panel_count",
    "roof_color",
    "trim_color",
    "side_color",
    "manufacturer_discount_pct",
    "downpayment_pct",
    "internal_notes",
    "wizard_step",
)


def _placement_state_keys_from_codes(codes: list[object]) -> list[str]:
    """
    Placement keys are dynamic (derived from selected option codes), so we treat them as
    an "extended persisted key set" for shadow-state restore and effective-state rendering.
    """
    out: list[str] = []
    for code in codes:
        if isinstance(code, str) and code:
            out.append(f"placement_{code}")
    return out


def _extended_persist_keys(defaults: Mapping[str, object]) -> list[str]:
    """
    Persist the base keys plus dynamic per-option placement keys.

    We use a union of the current session value, shadow snapshot, and defaults so that if
    Streamlit drops a widget-backed list temporarily, we still know which placement keys
    to protect.
    """
    shadow = st.session_state.get("_shadow_state")
    if not isinstance(shadow, dict):
        shadow = {}

    codes_live = list(st.session_state.get("selected_option_codes", []) or [])
    codes_shadow = list(shadow.get("selected_option_codes", []) or [])
    codes_default = list(defaults.get("selected_option_codes", []) or [])
    placement_keys = _placement_state_keys_from_codes(list({*codes_live, *codes_shadow, *codes_default}))
    return list(_PERSIST_STATE_KEYS) + placement_keys


def _sync_shadow_state(defaults: Mapping[str, object], *, active_keys: Optional[set[str]] = None) -> None:
    """
    Streamlit can occasionally clear widget-backed keys across reruns when widgets are not rendered
    in a particular run. To ensure wizard progress never 'randomly resets', we keep a shadow copy
    of key inputs and restore any missing keys from it.

    Additionally, Streamlit can sometimes "reset" a widget value (instead of dropping the key)
    when the widget is not rendered. In those cases, we restore non-active keys from shadow
    if they differ from the shadow snapshot. We only update the shadow snapshot from keys that
    are active in the current step (or all keys when `active_keys` is None).
    """
    shadow = st.session_state.get("_shadow_state")
    if not isinstance(shadow, dict):
        shadow = {}

    persist_keys = _extended_persist_keys(defaults)
    restored: list[str] = []
    for k in persist_keys:
        if k not in st.session_state:
            if k in shadow:
                st.session_state[k] = shadow[k]
                restored.append(k)
            elif k in defaults:
                st.session_state[k] = defaults[k]
                restored.append(k)
            continue

        # If a non-active key differs from shadow, we need to decide if it's a legitimate
        # user edit (e.g. they opened a collapsed expander and changed it) or a Streamlit
        # "reset" (bug where value disappears).
        #
        # Heuristic: If the new value looks like a "default" (empty/zero/None) AND the old
        # value was NOT default, assume it's a reset bug and restore the old value.
        # If the new value is non-default, assume the user intentionally changed it.
        if active_keys is not None and k not in active_keys and k in shadow:
            new_val = st.session_state.get(k)
            old_val = shadow.get(k)
            
            if new_val != old_val:
                is_reset = False
                default_val = defaults.get(k)
                
                # Check for "reset-like" values
                if new_val == default_val:
                     is_reset = True
                elif isinstance(new_val, list) and len(new_val) == 0:
                     is_reset = True
                elif isinstance(new_val, (int, float)) and new_val == 0:
                     is_reset = True
                elif new_val is None:
                     is_reset = True
                     
                if is_reset:
                    st.session_state[k] = shadow[k]
                    restored.append(k)

    # Update shadow snapshot. If active_keys is provided, only update shadow from those keys;
    # otherwise (e.g. initialization), update from all persisted keys.
    new_shadow: dict[str, object] = dict(shadow)
    keys_to_update = set(persist_keys) if active_keys is None else set(active_keys)
    for k in keys_to_update:
        if k in st.session_state:
            new_shadow[k] = st.session_state.get(k)

    # Keep the shadow in the canonical base-key shape, but also store placement keys so
    # they can be restored on later reruns even when Options is collapsed.
    st.session_state["_shadow_state"] = {k: new_shadow.get(k, defaults.get(k)) for k in persist_keys}

    if restored:
        _agent_log(
            hypothesis_id="S",
            location="local_demo_app.py:_sync_shadow_state",
            message="Restored missing session_state keys from shadow/defaults",
            data={"restored_keys": restored},
        )


def _effective_state(defaults: Mapping[str, object], *, active_keys: set[str]) -> dict[str, object]:
    """
    Build a stable view of persisted wizard state for quote generation.

    Streamlit can reset (overwrite) widget-backed keys on reruns when widgets are not rendered.
    For quote preview, we treat the current step's active keys as authoritative, and for all
    other persisted keys we prefer the shadow snapshot.
    """
    shadow = st.session_state.get("_shadow_state")
    if not isinstance(shadow, dict):
        shadow = {}

    out: dict[str, object] = {}
    persist_keys = _extended_persist_keys(defaults)
    for k in persist_keys:
        if k in active_keys:
            out[k] = st.session_state.get(k, defaults.get(k))
        else:
            out[k] = shadow.get(k, st.session_state.get(k, defaults.get(k)))
    return out


def _reset_state(book: PriceBook) -> None:
    # Clear wizard-level persistence helpers so "Start over" is a true reset.
    st.session_state.pop("wizard_checkpoints", None)
    st.session_state.pop("_shadow_state", None)
    st.session_state.pop("_pending_restore_step", None)
    st.session_state.pop("chat_pending_suggestion", None)
    st.session_state.pop("chat_last_auto_advance", None)
    st.session_state.pop("chat_built_size_has_style", None)
    st.session_state.pop("chat_built_size_has_dims", None)
    # Clear any per-option placement keys.
    for k in list(st.session_state.keys()):
        if isinstance(k, str) and k.startswith("placement_"):
            st.session_state.pop(k, None)
    for key in _default_state(book).keys():
        st.session_state.pop(key, None)
    _init_state(book)
    st.session_state["wizard_step"] = 0
    st.rerun()


def _available_option_codes(book: PriceBook) -> list[str]:
    all_codes = sorted(set(book.option_prices_by_length_usd.keys()))
    excluded = {
        "LEG_HEIGHT",
        "HEIGHT",
        "GROUND_CERTIFICATION",
        "EXTRA_PANEL",
        "GARAGE_DOOR_FRAME_OUT",
        "WINDOW_FRAME_OUT",
        "WALK_IN_DOOR_FRAME_OUT",
    }
    excluded_prefixes = ("ROLL_UP_DOOR_", "WALK_IN_DOOR_", "WINDOW_")
    return [
        c
        for c in all_codes
        if c not in excluded and not any(c.startswith(prefix) for prefix in excluded_prefixes)
    ]


def _wizard_steps() -> list[tuple[str, str]]:
    return [
        ("Built & Size", "built_size"),
        ("Leg Height", "leg_height"),
        ("Openings (Types)", "openings_types"),
        ("Openings (Placement)", "openings_placement"),
        ("Options", "options"),
        ("Colors", "colors"),
        ("Notes", "notes"),
        ("Quote", "quote"),
        ("Done", "done"),
    ]


def _active_keys_for_step_key(step_key: str) -> set[str]:
    """
    Keys that should be treated as authoritative (and synced into shadow) for a given wizard step.
    """
    active_keys: set[str] = set()
    if step_key == "built_size":
        active_keys.update({"demo_style", "demo_style_prev", "width_ft", "length_ft"})
    elif step_key == "leg_height":
        active_keys.add("leg_height_ft")
    elif step_key == "openings_types":
        # Micro-step lives under a single wizard step; the full set should be kept stable.
        active_keys.update(
            {
                "walk_in_door_type",
                "walk_in_door_count",
                "window_size",
                "window_count",
                "garage_door_type",
                "garage_door_size",
                "garage_door_count",
                "openings_types_stage",
            }
        )
    elif step_key == "openings_placement":
        active_keys.update(
            {
                "walk_in_door_type",
                "walk_in_door_count",
                "window_size",
                "window_count",
                "garage_door_type",
                "garage_door_size",
                "garage_door_count",
                "openings",
                "opening_seq",
            }
        )
    elif step_key == "options":
        active_keys.update(
            {
                "include_ground_certification",
                "selected_option_codes",
                "extra_panel_count",
                "options_stage",
            }
        )
        for k in st.session_state:
            if isinstance(k, str) and k.startswith("placement_"):
                active_keys.add(k)
    elif step_key == "colors":
        active_keys.update({"roof_color", "trim_color", "side_color"})
    elif step_key == "notes":
        active_keys.add("internal_notes")
    return active_keys


def _placement_to_str(value: object) -> Optional[str]:
    if isinstance(value, SectionPlacement):
        return value.value
    return None


def _quote_export_payload(book: PriceBook, quote) -> dict[str, object]:
    selected_options = []
    for code in st.session_state.selected_option_codes:
        selected_options.append(
            {
                "code": code,
                "placement": _placement_to_str(st.session_state.get(f"placement_{code}")),
            }
        )

    lead_name = str(st.session_state.get("lead_name") or "").strip()
    lead_email = str(st.session_state.get("lead_email") or "").strip()

    # Human-readable line-item overview for internal notifications (SMS/email).
    line_items_overview = [f"{li.description}: {_format_usd(int(li.amount_usd))}" for li in quote.line_items]

    payload: dict[str, object] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "pricebook_revision": book.revision,
        "lead": {
            "name": lead_name,
            "email": lead_email,
        },
        "line_items_overview": line_items_overview,
        "input": {
            "width_ft": int(st.session_state.width_ft),
            "length_ft": int(st.session_state.length_ft),
            "leg_height_ft": int(st.session_state.leg_height_ft),
            "include_ground_certification": bool(st.session_state.include_ground_certification),
            "selected_options": selected_options,
            "closed_sides": list(st.session_state.closed_sides),
            "closed_ends": list(st.session_state.closed_ends),
            "lean_to": {
                "enabled": bool(st.session_state.lean_to_enabled),
                "placement": _placement_to_str(st.session_state.lean_to_placement),
                "width_ft": int(st.session_state.lean_to_width_ft),
                "length_ft": int(st.session_state.lean_to_length_ft),
            },
            "doors_windows": {
                "walk_in_door_type": st.session_state.walk_in_door_type,
                "walk_in_door_qty": int(st.session_state.walk_in_door_count or 0),
                "window_size": st.session_state.window_size,
                "window_qty": int(st.session_state.window_count or 0),
                "garage_door_type": st.session_state.garage_door_type,
                "garage_door_size": st.session_state.garage_door_size,
                "garage_door_qty": int(st.session_state.garage_door_count or 0),
            },
            "extra_panel_qty": int(st.session_state.extra_panel_count or 0),
            "colors": {
                "roof": st.session_state.roof_color,
                "trim": st.session_state.trim_color,
                "sides": st.session_state.side_color,
            },
            "internal_notes": st.session_state.internal_notes,
        },
        "quote": {
            "normalized_width_ft": quote.normalized_width_ft,
            "normalized_length_ft": quote.normalized_length_ft,
            "total_usd": quote.total_usd,
            "notes": list(quote.notes),
            "line_items": [
                {"code": li.code, "description": li.description, "amount_usd": li.amount_usd}
                for li in quote.line_items
            ],
        },
    }
    return payload


def _post_quote_export_payload(*, url: str, payload: dict[str, object]) -> tuple[int, str]:
    """
    Best-effort demo POST for "Export".

    Returns (status_code, response_text_snippet). On failure, returns (0, error_message).
    """
    try:
        body = json.dumps(payload).encode("utf-8")
        req = urllib_request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib_request.urlopen(req, timeout=3.0) as resp:
            status = int(getattr(resp, "status", 200) or 200)
            text = resp.read(1200).decode("utf-8", errors="replace")
            return status, text
    except Exception as exc:
        return 0, str(exc)


def _build_quote_pdf_bytes_for_current_state(book: PriceBook, quote) -> bytes:
    """
    Build PDF bytes for the current quote + current UI state.
    """
    defaults = _default_state(book)
    export_active_keys = _active_keys_for_step_key("quote")
    export_active_keys.add("wizard_step")
    export_active_keys.update({"manufacturer_discount_pct", "downpayment_pct"})
    export_state = _effective_state(defaults, active_keys=export_active_keys)

    quote_id = _quote_input_signature(book, quote)
    logo_bytes = _cached_logo_png_bytes()
    all_views = _cached_building_views_png(
        width_ft=int(export_state.get("width_ft") or 0),
        length_ft=int(export_state.get("length_ft") or 0),
        height_ft=int(export_state.get("leg_height_ft") or 0),
        roof_color=str(export_state.get("roof_color") or "White"),
        trim_color=str(export_state.get("trim_color") or "White"),
        side_color=str(export_state.get("side_color") or "White"),
        openings=_preview_openings_from_mapping(export_state),
    )

    building_amount_cents = int(quote.total_usd) * 100
    discount_pct = float(st.session_state.get("manufacturer_discount_pct") or 0.0) / 100.0
    discount_cents = int(round(discount_pct * building_amount_cents))
    subtotal_cents = building_amount_cents - discount_cents
    downpayment_pct = float(st.session_state.get("downpayment_pct") or 0.0) / 100.0
    downpayment_cents = int(round(downpayment_pct * subtotal_cents))
    balance_due_cents = subtotal_cents - downpayment_cents

    artifact = QuotePdfArtifact(
        quote_id=quote_id,
        quote_date=datetime.now(timezone.utc).date(),
        pricebook_revision=book.revision,
        customer_name=str(st.session_state.get("lead_name") or "").strip(),
        customer_email=str(st.session_state.get("lead_email") or "").strip(),
        building_label="Commercial Buildings",
        building_summary=(
            f"{int(st.session_state.get('width_ft') or 0)} x "
            f"{int(st.session_state.get('length_ft') or 0)} x "
            f"{int(st.session_state.get('leg_height_ft') or 0)}"
        ),
        line_items=tuple(
            QuotePdfLineItem(
                description=str(li.description),
                qty=1,
                amount_cents=int(li.amount_usd) * 100,
            )
            for li in quote.line_items
        ),
        totals=QuotePdfTotals(
            building_amount_cents=building_amount_cents,
            discount_cents=discount_cents,
            subtotal_cents=subtotal_cents,
            additional_charges_cents=0,
            grand_total_cents=subtotal_cents,
            downpayment_cents=downpayment_cents,
            balance_due_cents=balance_due_cents,
        ),
        notes=tuple(str(n) for n in (quote.notes or ())),
        logo_png_bytes=logo_bytes,
        building_preview_png_bytes=all_views.get("isometric"),
        building_views_png_bytes=all_views,
    )
    return make_quote_pdf_bytes(artifact)


def _quote_line_items_csv(quote) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=["code", "description", "amount_usd"])
    w.writeheader()
    for li in quote.line_items:
        w.writerow({"code": li.code, "description": li.description, "amount_usd": li.amount_usd})
    return buf.getvalue()


def _quote_text_summary(book: PriceBook, quote) -> str:
    lines = [
        "Coast to Coast - Quote",
        f"Pricebook: {book.revision}",
        f"Priced size: {quote.normalized_width_ft} ft x {quote.normalized_length_ft} ft",
        "",
        "Line items:",
    ]
    for li in quote.line_items:
        lines.append(f"- {li.description}: {_format_usd(li.amount_usd)}")
    lines.append("")
    lines.append(f"Total: {_format_usd(quote.total_usd)}")
    if quote.notes:
        lines.append("")
        lines.append("Notes:")
        for n in quote.notes:
            lines.append(f"- {n}")
    return "\n".join(lines) + "\n"


def _render_sidebar(book: PriceBook, step_index: int, step_labels: list[str], quote, quote_error: Optional[str]) -> None:
    _render_logo(where="sidebar")

    with st.sidebar.expander("AI (intent assist)", expanded=False):
        st.sidebar.caption("Optional: use GPT to interpret chat input per step (style/size, openings, placement, etc.).")
        has_key = bool(_read_secret_or_env_str("OPENAI_API_KEY"))
        st.checkbox("Enable GPT intent assist", key="ai_intent_enabled_ui", disabled=not has_key)
        st.text_input("Model", key="ai_intent_model_ui")
        if not has_key:
            st.sidebar.caption("Set `OPENAI_API_KEY` to enable.")
        if bool(st.session_state.get("ai_intent_enabled_ui", False)) and has_key and not ai_intent.ai_intent_enabled():
            st.sidebar.caption("Install the `openai` package (or check key/model) to activate.")

    lead_name = str(st.session_state.get("lead_name") or "").strip()
    lead_email = str(st.session_state.get("lead_email") or "").strip()
    lead_captured = bool(st.session_state.get("lead_captured"))
    if not lead_captured:
        st.sidebar.subheader("Lead capture")
        if lead_name or lead_email:
            st.sidebar.caption(f"Lead: **{lead_name or '-'}** / **{lead_email or '-'}**")
        st.sidebar.caption("Enter name + email in the main pane to start quoting.")

    with st.sidebar.expander("Wizard", expanded=True):
        for idx, label in enumerate(step_labels):
            marker = "➡️" if idx == step_index else "•"
            st.sidebar.write(f"{marker} {label}")
        st.sidebar.progress((step_index + 1) / len(step_labels))

    st.sidebar.caption("Quote preview")
    st.sidebar.write(f"Pricebook: **{book.revision}**")
    if quote_error:
        st.sidebar.error(quote_error)
    elif quote is None:
        st.sidebar.write("No quote yet.")
    else:
        st.sidebar.metric("Total", _format_usd(quote.total_usd))
        st.sidebar.write(f"Size: {quote.normalized_width_ft} x {quote.normalized_length_ft} ft")
        st.sidebar.write(f"Line items: {len(quote.line_items)}")
        with st.sidebar.expander("Line items (preview)", expanded=False):
            rows = [
                {"Code": li.code, "Description": li.description, "Amount": _format_usd(li.amount_usd)}
                for li in quote.line_items
            ]
            st.dataframe(rows, use_container_width=True, hide_index=True)

    with st.sidebar.expander("Actions", expanded=False):
        if st.sidebar.button("Reset quote", use_container_width=True):
            _reset_state(book)

        disabled = quote_error is not None or quote is None
        if not disabled:
            payload = _quote_export_payload(book, quote)
            st.sidebar.download_button(
                "Download quote (JSON)",
                data=json.dumps(payload, indent=2),
                file_name="quote.json",
                mime="application/json",
                use_container_width=True,
            )
            # PDF export (demo v1)
            try:
                pdf_bytes = _build_quote_pdf_bytes_for_current_state(book, quote)
                st.sidebar.download_button(
                    "Download quote (PDF)",
                    data=pdf_bytes,
                    file_name="quote.pdf",
                    mime="application/pdf",
                    use_container_width=True,
                )
            except Exception:
                # Keep the sidebar usable even if PDF export fails.
                pass
            st.sidebar.download_button(
                "Download line items (CSV)",
                data=_quote_line_items_csv(quote),
                file_name="quote_line_items.csv",
                mime="text/csv",
                use_container_width=True,
            )
            st.sidebar.download_button(
                "Download quote (TXT)",
                data=_quote_text_summary(book, quote),
                file_name="quote.txt",
                mime="text/plain",
                use_container_width=True,
            )


def _render_step_controls(step_index: int, max_index: int) -> None:
    # In the accordion layout, we just want a "Next" button that advances the step (expands the next section).
    # We can also have a "Back" button, but users can also just click the headers.
    col1, col2, _ = st.columns([1, 1, 6])
    
    # Only show Back if we are not at the start
    if step_index > 0:
        if col1.button("Back", key=f"wizard_back_{step_index}", use_container_width=True):
            st.session_state["wizard_step"] = step_index - 1
            st.rerun()

    # Only show Next if we are not at the end
    if step_index < max_index:
        if col2.button("Next", key=f"wizard_next_{step_index}", use_container_width=True):
            st.session_state["wizard_step"] = step_index + 1
            st.rerun()


def _build_selected_options(book: PriceBook) -> tuple[SelectedOption, ...]:
    return _build_selected_options_from_state(st.session_state, book)


def _build_selected_options_from_state(state: Mapping[str, object], book: PriceBook) -> tuple[SelectedOption, ...]:
    # region agent log
    _agent_log(
        hypothesis_id="A",
        location="local_demo_app.py:_build_selected_options:entry",
        message="Build selected options from session_state",
        data={
            "selected_option_codes": list(state.get("selected_option_codes", []) or []),
            "walk_in_door_type": state.get("walk_in_door_type"),
            "walk_in_door_count": int(state.get("walk_in_door_count") or 0),
            "window_size": state.get("window_size"),
            "window_count": int(state.get("window_count") or 0),
            "garage_door_type": state.get("garage_door_type"),
            "garage_door_size": state.get("garage_door_size"),
            "garage_door_count": int(state.get("garage_door_count") or 0),
            "extra_panel_count": int(state.get("extra_panel_count") or 0),
        },
    )
    # endregion agent log

    selected: list[SelectedOption] = []
    for code in list(state.get("selected_option_codes", []) or []):
        selected.append(
            SelectedOption(
                code=code,
                placement=state.get(f"placement_{code}"),
            )
        )

    def _add_counted(code: str, count: int) -> None:
        if count <= 0 or code not in book.option_prices_by_length_usd:
            # region agent log
            _agent_log(
                hypothesis_id="B",
                location="local_demo_app.py:_build_selected_options:_add_counted",
                message="Skipping counted add-on (count<=0 or missing pricing)",
                data={"code": code, "count": int(count), "has_pricing": code in book.option_prices_by_length_usd},
            )
            # endregion agent log
            return
        for _ in range(count):
            selected.append(SelectedOption(code=code, placement=None))

    # Prefer explicit openings (wall + offset) when available.
    openings_state = state.get("openings")
    if isinstance(openings_state, list) and openings_state:
        placement_map = {
            "front": SectionPlacement.FRONT,
            "back": SectionPlacement.BACK,
            "left": SectionPlacement.LEFT,
            "right": SectionPlacement.RIGHT,
        }

        def _add_opening_option(code: str, placement: Optional[SectionPlacement]) -> None:
            if code and code in book.option_prices_by_length_usd:
                selected.append(SelectedOption(code=code, placement=placement))

        # Walk-in doors
        door_code = WALK_IN_DOOR_OPTIONS.get(str(state.get("walk_in_door_type") or ""))
        # Windows
        window_code = WINDOW_OPTIONS.get(str(state.get("window_size") or ""))
        # Garage
        garage_kind = str(state.get("garage_door_type") or "None")
        garage_code = None
        if garage_kind == "Roll-up":
            garage_code = ROLL_UP_DOOR_OPTIONS.get(str(state.get("garage_door_size") or ""))
        elif garage_kind == "Frame-out":
            garage_code = "GARAGE_DOOR_FRAME_OUT"

        for row in openings_state:
            if not isinstance(row, dict):
                continue
            kind = str(row.get("kind") or "door").strip().lower()
            side = str(row.get("side") or "front").strip().lower()
            placement = placement_map.get(side)
            if kind == "window" and window_code:
                _add_opening_option(window_code, placement)
            elif kind == "garage" and garage_code:
                _add_opening_option(garage_code, placement)
            else:
                if door_code:
                    _add_opening_option(door_code, placement)

        return tuple(selected)

    if state.get("walk_in_door_type") in WALK_IN_DOOR_OPTIONS:
        _add_counted(
            WALK_IN_DOOR_OPTIONS[str(state.get("walk_in_door_type"))],
            int(state.get("walk_in_door_count") or 0),
        )

    if state.get("window_size") in WINDOW_OPTIONS:
        _add_counted(
            WINDOW_OPTIONS[str(state.get("window_size"))],
            int(state.get("window_count") or 0),
        )

    if state.get("garage_door_type") == "Roll-up":
        code = ROLL_UP_DOOR_OPTIONS.get(str(state.get("garage_door_size")))
        if code:
            _add_counted(code, int(state.get("garage_door_count") or 0))
    elif state.get("garage_door_type") == "Frame-out":
        _add_counted("GARAGE_DOOR_FRAME_OUT", int(state.get("garage_door_count") or 0))

    _add_counted("EXTRA_PANEL", int(state.get("extra_panel_count") or 0))

    # region agent log
    _agent_log(
        hypothesis_id="A",
        location="local_demo_app.py:_build_selected_options:exit",
        message="Built selected options",
        data={
            "selected_options_len": len(selected),
            "selected_option_codes_expanded": [s.code for s in selected],
        },
    )
    # endregion agent log

    return tuple(selected)


def _render_built_size_controls(book: PriceBook, disabled: bool) -> None:
    style_labels = ["Regular (Horizontal)", "A-Frame (Horizontal)", "A-Frame (Vertical)"]
    # Streamlit selectbox widgets will raise if the current session value is not in `options`.
    default_style = "A-Frame (Horizontal)" if "A-Frame (Horizontal)" in style_labels else style_labels[0]
    if str(st.session_state.get("demo_style") or "") not in style_labels:
        st.session_state["demo_style"] = default_style
    st.selectbox("Style", options=style_labels, key="demo_style", disabled=disabled)

    # Preserve the user's length intent when toggling horizontal <-> vertical.
    prev_style = st.session_state.get("demo_style_prev")
    new_style = st.session_state.get("demo_style")
    if (
        not disabled
        and isinstance(prev_style, str)
        and isinstance(new_style, str)
        and prev_style != new_style
    ):
        old_len = int(st.session_state.get("length_ft") or 0)
        # Map between the common demo grids: 21<->20, 26<->25, 31<->30, 36<->35
        if prev_style != "A-Frame (Vertical)" and new_style == "A-Frame (Vertical)":
            if old_len in (21, 26, 31, 36):
                st.session_state["length_ft"] = old_len - 1
        elif prev_style == "A-Frame (Vertical)" and new_style != "A-Frame (Vertical)":
            if old_len in (20, 25, 30, 35):
                st.session_state["length_ft"] = old_len + 1
    st.session_state["demo_style_prev"] = str(st.session_state.get("demo_style") or "")

    is_vertical = str(st.session_state.get("demo_style") or "") == "A-Frame (Vertical)"
    if is_vertical:
        st.caption("Per manufacturer rule: Vertical Buildings Are 1' Shorter Than Horizontal.")
        allowed_lengths = [20, 25, 30, 35]
    else:
        allowed_lengths = [21, 26, 31, 36]

    allowed_widths = list(book.allowed_widths_ft)
    if allowed_widths:
        raw_width = st.session_state.get("width_ft")
        coerced_width: Optional[int] = None
        try:
            if isinstance(raw_width, bool):
                coerced_width = None
            elif isinstance(raw_width, int):
                coerced_width = raw_width
            elif isinstance(raw_width, float) and raw_width.is_integer():
                coerced_width = int(raw_width)
            elif isinstance(raw_width, str):
                s = raw_width.strip()
                if s.isdigit():
                    coerced_width = int(s)
        except Exception:
            coerced_width = None

        if coerced_width in allowed_widths:
            st.session_state["width_ft"] = int(coerced_width)
        else:
            st.session_state["width_ft"] = allowed_widths[0]
    st.selectbox("Width (ft)", options=list(book.allowed_widths_ft), key="width_ft", disabled=disabled)
    # Streamlit requires the current session_state value to be one of the options.
    current_length = st.session_state.get("length_ft")
    if current_length not in allowed_lengths:
        st.session_state["length_ft"] = allowed_lengths[0]
    st.selectbox("Length (ft)", options=allowed_lengths, key="length_ft", disabled=disabled)
    st.caption("Gauge is fixed for the demo (14 ga).")

def _render_leg_height_controls(book: PriceBook, disabled: bool) -> None:
    leg_heights = list(book.allowed_leg_heights_ft) or [6]
    st.selectbox("Leg height (ft)", options=leg_heights, key="leg_height_ft", disabled=disabled)
    if int(st.session_state.get("leg_height_ft") or 0) >= 13:
        st.error("Requires Customer Lift (13' or taller).")

def _render_openings_types_controls(book: PriceBook, disabled: bool) -> None:
    def _clear_advanced_openings() -> None:
        # Count-based mode should override explicit openings; clear them whenever qty changes.
        st.session_state["openings"] = []

    def _render_qty_stepper(
        *,
        label: str,
        state_key: str,
        max_value: int,
        disabled: bool,
        help_text: Optional[str] = None,
    ) -> None:
        st.number_input(
            label,
            min_value=0,
            max_value=max_value,
            step=1,
            key=state_key,
            disabled=disabled,
            on_change=_clear_advanced_openings,
            help=help_text,
        )

    st.markdown("**Doors**")
    door_left, door_right = st.columns([3, 2], gap="medium")
    with door_left:
        walk_in_labels = ["None"] + _available_accessory_labels(book, WALK_IN_DOOR_OPTIONS)
        if str(st.session_state.get("walk_in_door_type") or "None") not in walk_in_labels:
            st.session_state["walk_in_door_type"] = "None"
        st.selectbox(
            "Walk-in door type",
            options=walk_in_labels,
            key="walk_in_door_type",
            disabled=disabled,
        )
    with door_right:
        if not disabled and str(st.session_state.get("walk_in_door_type") or "None") == "None":
            st.session_state["walk_in_door_count"] = 0
        _render_qty_stepper(
            label="Door qty",
            state_key="walk_in_door_count",
            max_value=12,
            disabled=disabled or str(st.session_state.get("walk_in_door_type") or "None") == "None",
        )

    st.markdown("**Windows**")
    win_left, win_right = st.columns([3, 2], gap="medium")
    with win_left:
        window_labels = ["None"] + _available_accessory_labels(book, WINDOW_OPTIONS)
        if str(st.session_state.get("window_size") or "None") not in window_labels:
            st.session_state["window_size"] = "None"
        st.selectbox("Window size", options=window_labels, key="window_size", disabled=disabled)
    with win_right:
        if not disabled and str(st.session_state.get("window_size") or "None") == "None":
            st.session_state["window_count"] = 0
        _render_qty_stepper(
            label="Window qty",
            state_key="window_count",
            max_value=24,
            disabled=disabled or str(st.session_state.get("window_size") or "None") == "None",
        )

    st.markdown("**Garage doors**")
    g1, g2, g3 = st.columns([2, 2, 1], gap="medium")
    with g1:
        st.selectbox(
            "Garage door type",
            options=["None", "Roll-up", "Frame-out"],
            key="garage_door_type",
            disabled=disabled,
        )
    with g2:
        if str(st.session_state.get("garage_door_type") or "None") == "Roll-up":
            roll_up_labels = _available_accessory_labels(book, ROLL_UP_DOOR_OPTIONS)
            if not roll_up_labels:
                st.warning("No roll-up door pricing found in this pricebook.")
            else:
                if str(st.session_state.get("garage_door_size") or "") not in roll_up_labels:
                    st.session_state["garage_door_size"] = roll_up_labels[0]
                st.selectbox(
                    "Roll-up door size",
                    options=roll_up_labels,
                    key="garage_door_size",
                    disabled=disabled,
                )
        elif str(st.session_state.get("garage_door_type") or "None") == "Frame-out":
            st.caption("Frame-out openings are priced per opening (when available).")
        else:
            st.caption("")
    with g3:
        if not disabled and str(st.session_state.get("garage_door_type") or "None") == "None":
            st.session_state["garage_door_count"] = 0
        _render_qty_stepper(
            label="Qty",
            state_key="garage_door_count",
            max_value=4,
            disabled=disabled or str(st.session_state.get("garage_door_type") or "None") == "None",
        )

    openings = st.session_state.get("openings")
    if isinstance(openings, list) and openings:
        st.caption("Note: you have advanced opening placement saved; this screen uses simple qty mode.")
        if st.button("Clear advanced openings", key="clear_advanced_openings", disabled=disabled, use_container_width=True):
            st.session_state["openings"] = []
            st.session_state["opening_seq"] = int(st.session_state.get("opening_seq") or 1)
            st.rerun()


def _render_openings_placement_controls(book: PriceBook, disabled: bool) -> None:
    """
    Optional placement editor for openings (wall + offset) that drives the drawing pages.

    Pricing for openings remains qty/type based; placement is visual only.
    """
    if "openings" not in st.session_state or not isinstance(st.session_state.get("openings"), list):
        st.session_state["openings"] = []
    if "opening_seq" not in st.session_state:
        st.session_state["opening_seq"] = 1

    st.caption("Optional: place openings by wall + offset for the drawing. If empty, we auto-place.")

    if st.button("Clear all placements", key="openings_clear_all", disabled=disabled, use_container_width=True):
        st.session_state["openings"] = []
        st.session_state["opening_seq"] = int(st.session_state.get("opening_seq") or 1)
        st.rerun()

    # IMPORTANT: This function is often rendered inside a step expander in the Configuration tab.
    # Streamlit does not allow nested expanders, so we avoid st.expander here entirely.
    with st.container(border=True):
        st.markdown("**Add opening**")
        c1, c2, c3 = st.columns([1, 1, 1])
        if c1.button("Door", key="openings_add_door", disabled=disabled, use_container_width=True):
            openings_list = st.session_state.get("openings")
            if isinstance(openings_list, list):
                openings_list.append(
                    {"id": int(st.session_state.get("opening_seq") or 1), "kind": "door", "side": "front", "offset_ft": 0}
                )
            st.session_state["opening_seq"] = int(st.session_state.get("opening_seq") or 1) + 1
            st.rerun()
        if c2.button("Window", key="openings_add_window", disabled=disabled, use_container_width=True):
            openings_list = st.session_state.get("openings")
            if isinstance(openings_list, list):
                openings_list.append(
                    {"id": int(st.session_state.get("opening_seq") or 1), "kind": "window", "side": "right", "offset_ft": 0}
                )
            st.session_state["opening_seq"] = int(st.session_state.get("opening_seq") or 1) + 1
            st.rerun()
        if c3.button("Garage", key="openings_add_garage", disabled=disabled, use_container_width=True):
            openings_list = st.session_state.get("openings")
            if isinstance(openings_list, list):
                openings_list.append(
                    {"id": int(st.session_state.get("opening_seq") or 1), "kind": "garage", "side": "front", "offset_ft": 0}
                )
            st.session_state["opening_seq"] = int(st.session_state.get("opening_seq") or 1) + 1
            st.rerun()

    openings_now = st.session_state.get("openings")
    if not isinstance(openings_now, list) or not openings_now:
        st.info("No explicit placements. Auto-placement will be used.")
        return

    st.caption(f"Placed openings: **{len(openings_now)}**")
    sides = ["front", "back", "left", "right"]
    for idx, row in enumerate(list(openings_now)):
        if not isinstance(row, dict):
            continue
        oid = int(row.get("id") or (idx + 1))
        with st.container(border=True):
            st.markdown(f"**Opening #{oid}**")
            r1, r2, r3, r4 = st.columns([1, 1, 1, 1])
            kind = r1.selectbox(
                "Type",
                options=["door", "window", "garage"],
                index=["door", "window", "garage"].index(str(row.get("kind") or "door")),
                key=f"opening_{oid}_kind",
                disabled=disabled,
            )
            side = r2.selectbox(
                "Wall",
                options=sides,
                index=sides.index(str(row.get("side") or "front")) if str(row.get("side") or "front") in sides else 0,
                key=f"opening_{oid}_side",
                disabled=disabled,
            )
            wall_ft = int(st.session_state.get("width_ft") or 0) if side in ("front", "back") else int(st.session_state.get("length_ft") or 0)
            max_offset = max(0, wall_ft)
            offset_default = min(int(row.get("offset_ft") or 0), max_offset)
            offset_ft = r3.number_input(
                "Offset (ft)",
                min_value=0,
                max_value=max_offset,
                step=1,
                value=offset_default,
                key=f"opening_{oid}_offset",
                disabled=disabled,
            )
            if r4.button("Remove", key=f"opening_{oid}_remove", disabled=disabled, use_container_width=True):
                st.session_state["openings"] = [
                    o
                    for o in list(st.session_state.get("openings") or [])
                    if not (isinstance(o, dict) and int(o.get("id") or -1) == oid)
                ]
                st.rerun()

            row["kind"] = str(kind)
            row["side"] = str(side)
            row["offset_ft"] = int(offset_ft)
        openings_after = st.session_state.get("openings")
        if isinstance(openings_after, list) and 0 <= idx < len(openings_after):
            openings_after[idx] = row

def _render_options_controls(book: PriceBook, disabled: bool) -> None:
    st.checkbox("Ground certification", key="include_ground_certification", disabled=disabled)
    option_codes = _available_option_codes(book)
    st.multiselect("Add options", options=option_codes, key="selected_option_codes", disabled=disabled)
    placements = [
        None,
        SectionPlacement.FRONT,
        SectionPlacement.BACK,
        SectionPlacement.LEFT,
        SectionPlacement.RIGHT,
    ]
    for code in st.session_state.selected_option_codes:
        st.selectbox(
            f"Placement for {code}",
            options=placements,
            format_func=lambda v: "(none)" if v is None else v.value,
            key=f"placement_{code}",
            disabled=disabled,
        )
    if "EXTRA_PANEL" in book.option_prices_by_length_usd:
        st.number_input(
            "Extra panels",
            min_value=0,
            max_value=12,
            step=1,
            key="extra_panel_count",
            disabled=disabled,
        )

def _render_colors_controls(book: PriceBook, disabled: bool) -> None:
    # Keep this list aligned with `building_views._NAMED_COLORS` so the PDF preview can match
    # vendor screenshots (e.g., Burgundy + Sandstone).
    colors = ["White", "Gray", "Black", "Tan", "Sandstone", "Brown", "Red", "Burgundy", "Blue", "Green"]
    st.selectbox("Roof color", options=colors, key="roof_color", disabled=disabled)
    st.selectbox("Trim color", options=colors, key="trim_color", disabled=disabled)
    st.selectbox("Side color", options=colors, key="side_color", disabled=disabled)

def _render_notes_controls(book: PriceBook, disabled: bool) -> None:
    st.text_area("Internal notes (demo only)", key="internal_notes", height=160, disabled=disabled)


def _render_builder_panel(book: PriceBook, steps: list[tuple[str, str]], current_step_index: int) -> None:
    st.subheader("Configuration")
    
    # Iterate through all steps and render them as expanders
    for idx, (label, key) in enumerate(steps):
        # We don't render the "Quote" step in the builder config list usually, it's the result.
        # But if the wizard treats it as a step, we can include it or handle it separately.
        # The original code had "Quote" as the last step.
        if key in {"quote", "done"}:
            continue

        is_active_step = idx == current_step_index
        is_expanded = is_active_step
        
        # Use a checkmark if the step is "past"
        prefix = "✅ " if idx < current_step_index else "🔷 " if idx == current_step_index else "⬜ "
        
        with st.expander(f"{prefix}{label}", expanded=is_expanded):
            # If the user opens a non-active step, offer an explicit "Edit" action that
            # makes it the active step. This keeps chat + shadow-state protections aligned.
            if not is_active_step:
                if st.button(f"Edit {label}", key=f"builder_edit_{key}", use_container_width=True):
                    st.session_state["wizard_step"] = idx
                    st.rerun()
                st.caption("This section is read-only until you make it the active step.")

            # Render the controls for this step
            if key == "built_size":
                _render_built_size_controls(book=book, disabled=not is_active_step)
            elif key == "leg_height":
                _render_leg_height_controls(book=book, disabled=not is_active_step)
            elif key == "openings_types":
                _render_openings_types_controls(book=book, disabled=not is_active_step)
            elif key == "openings_placement":
                _render_openings_placement_controls(book=book, disabled=not is_active_step)
            elif key == "options":
                _render_options_controls(book=book, disabled=not is_active_step)
            elif key == "colors":
                _render_colors_controls(book=book, disabled=not is_active_step)
            elif key == "notes":
                _render_notes_controls(book=book, disabled=not is_active_step)
            
            # Show navigation controls inside the active expander only?
            # Or always show them to allow jumping?
            # It's cleaner to show them in the active one.
            if is_active_step:
                st.divider()
                _render_step_controls(idx, len(steps) - 1)

    # If the current step is Quote, show it prominently at the bottom or top?
    # In the Accordion model, "Quote" isn't really a configuration step, it's the output.
    # We will handle Quote display in the main function or sidebar.


def main() -> None:
    st.set_page_config(page_title="Coast to Coast - Quote Demo (Local)", layout="wide")
    st.title("Coast to Coast - Quote Demo (Local)")

    _password_gate()
    _sync_openai_intent_env_from_secrets()

    _init_lead_state()
    _apply_ai_intent_env_from_ui_state()
    _sync_lead_shadow()

    book = _load_pricebook_from_extracted()
    if not book.allowed_widths_ft or not book.allowed_lengths_ft or not book.allowed_leg_heights_ft:
        st.error("Extracted pricebook is missing required size data.")
        st.stop()
    _init_state(book)

    if bool(st.session_state.pop("_chat_reset_requested", False)):
        _reset_state(book)

    # region agent log
    _agent_log(
        hypothesis_id="F",
        location="local_demo_app.py:main",
        message="Main rerun snapshot",
        data={
            "revision": book.revision,
            "wizard_step": int(st.session_state.get("wizard_step") or 0),
            "width_ft": int(st.session_state.get("width_ft") or 0),
            "length_ft": int(st.session_state.get("length_ft") or 0),
            "leg_height_ft": int(st.session_state.get("leg_height_ft") or 0),
            "selected_option_codes_len": len(st.session_state.get("selected_option_codes", [])),
            "window_size": st.session_state.get("window_size"),
            "window_count": int(st.session_state.get("window_count") or 0),
        },
    )
    # endregion agent log

    steps = _wizard_steps()
    step_labels = [s[0] for s in steps]
    step_keys = [s[1] for s in steps]
    step_index = int(st.session_state.get("wizard_step") or 0)
    step_index = max(0, min(step_index, len(steps) - 1))
    st.session_state["wizard_step"] = step_index

    defaults = _default_state(book)

    # Checkpoint restoration logic (same as before)
    pending_restore = st.session_state.pop("_pending_restore_step", None)
    if isinstance(pending_restore, int) and pending_restore == step_index:
        _restore_checkpoint(step_index, defaults)
    elif isinstance(pending_restore, str):
        try:
            if int(pending_restore) == step_index:
                _restore_checkpoint(step_index, defaults)
        except ValueError:
            pass

    step_key = step_keys[step_index]

    if not bool(st.session_state.get("lead_captured")):
        _lead_capture_form()
        return

    # Tabs layout (preferred for the demo vs a strict split-screen)
    tab_config, tab_chat = st.tabs(["Configuration", "Conversation"])

    # Calculate quote on every rerun so the sidebar stays correct regardless of which tab is open.
    quote = None
    quote_error = None
    try:
        # Determine active keys based on the current step.
        # This is CRITICAL: it prevents Streamlit from "resetting" values in collapsed sections.
        active_keys = _active_keys_for_step_key(step_key)

        # Chat can set step values and immediately auto-advance without ever rendering the prior step's
        # widgets. In that case, include the prior step's active keys in this rerun so shadow-state
        # captures the just-updated values before Streamlit can reset them.
        last_auto = st.session_state.get("chat_last_auto_advance")
        if isinstance(last_auto, dict):
            try:
                from_idx = int(last_auto.get("from_step_index"))
                to_idx = int(last_auto.get("to_step_index"))
            except Exception:
                from_idx = -1
                to_idx = -1
            should_commit_prev = not bool(last_auto.get("shadow_committed"))
            if should_commit_prev and to_idx == int(step_index) and 0 <= from_idx < len(step_keys):
                active_keys.update(_active_keys_for_step_key(str(step_keys[from_idx])))
                # Mark committed so later reruns on the same step don't keep treating prior-step keys as active
                # (which could overwrite shadow with reset/default values).
                st.session_state["chat_last_auto_advance"] = {
                    **last_auto,
                    "shadow_committed": True,
                }

        # Always treat wizard navigation as authoritative.
        active_keys.add("wizard_step")
        # Sidebar Terms are always-visible widgets; treat as active so edits never get shadow-overridden.
        active_keys.update({"manufacturer_discount_pct", "downpayment_pct"})

        # Sync shadow state, protecting non-active keys from accidental resets.
        _sync_shadow_state(defaults, active_keys=active_keys)
        state = _effective_state(defaults, active_keys=active_keys)

        demo_style = str(state.get("demo_style"))
        if demo_style == "Regular (Horizontal)":
            style = CarportStyle.REGULAR
            roof_style = RoofStyle.HORIZONTAL
        elif demo_style == "A-Frame (Vertical)":
            style = CarportStyle.A_FRAME
            roof_style = RoofStyle.VERTICAL
        else:
            style = CarportStyle.A_FRAME
            roof_style = RoofStyle.HORIZONTAL

        inp = QuoteInput(
            style=style,
            roof_style=roof_style,
            gauge=14,
            width_ft=int(state.get("width_ft") or 0),
            length_ft=int(state.get("length_ft") or 0),
            leg_height_ft=int(state.get("leg_height_ft") or 0),
            include_ground_certification=bool(state.get("include_ground_certification")),
            selected_options=_build_selected_options_from_state(state, book),
            closed_end_count=0,
            closed_side_count=0,
            lean_to_enabled=False,
            lean_to_width_ft=0,
            lean_to_length_ft=0,
            lean_to_placement=None,
        )
        quote = generate_quote(inp, book)
    except PriceBookError as exc:
        quote_error = str(exc)

    with tab_chat:
        _render_chat_panel(step_key=step_key, step_index=step_index, max_step_index=len(steps) - 1, book=book)

    with tab_config:
        _render_builder_panel(book=book, steps=steps, current_step_index=step_index)

        # Show quote details if we are on the Quote step.
        if step_key == "quote":
            st.markdown("## Quote")
            if quote_error:
                st.error(quote_error)
            elif quote:
                # Auto-save lead snapshot
                _append_lead_snapshot(book=book, quote=quote)
                left, right = st.columns([2, 1], gap="large")
                with left:
                    st.metric("Total", _format_usd(quote.total_usd))
                    if quote.notes:
                        for note in quote.notes:
                            st.info(note)
                with right:
                    try:
                        preview_png = _cached_building_isometric_png(
                            width_ft=int(state.get("width_ft") or 0),
                            length_ft=int(state.get("length_ft") or 0),
                            height_ft=int(state.get("leg_height_ft") or 0),
                            roof_color=str(state.get("roof_color") or "White"),
                            trim_color=str(state.get("trim_color") or "White"),
                            side_color=str(state.get("side_color") or "White"),
                            openings=_preview_openings_from_mapping(state),
                        )
                        st.image(preview_png, caption="Building view", use_container_width=True)
                    except Exception:
                        # The quote should still render even if the preview fails.
                        pass
                rows = [
                    {"Code": li.code, "Description": li.description, "Amount": _format_usd(li.amount_usd)}
                    for li in quote.line_items
                ]
                st.dataframe(rows, use_container_width=True, hide_index=True)

                export_url = str(os.environ.get("QUOTE_EXPORT_URL") or "").strip()
                if st.button("Export", key="export_quote", use_container_width=True):
                    payload = _quote_export_payload(book, quote)
                    st.session_state["export_payload"] = payload
                    # Generate PDF so the next page can offer download immediately.
                    try:
                        st.session_state["export_pdf_bytes"] = _build_quote_pdf_bytes_for_current_state(book, quote)
                        st.session_state["export_pdf_error"] = None
                    except Exception as exc:
                        st.session_state["export_pdf_bytes"] = None
                        st.session_state["export_pdf_error"] = str(exc)

                    if export_url:
                        status, resp_text = _post_quote_export_payload(url=export_url, payload=payload)
                        st.session_state["export_post_status"] = int(status)
                        st.session_state["export_post_response"] = str(resp_text or "")
                    else:
                        st.session_state["export_post_status"] = 0
                        st.session_state["export_post_response"] = "QUOTE_EXPORT_URL not set; skipped POST."

                    st.session_state["wizard_step"] = min(len(steps) - 1, step_index + 1)
                    st.rerun()

        if step_key == "done":
            _render_logo(where="main")
            st.markdown("## Thanks")
            lead_name = str(st.session_state.get("lead_name") or "").strip()
            lead_email = str(st.session_state.get("lead_email") or "").strip()
            if lead_name or lead_email:
                st.success(
                    f"A member of our team will be in contact with **{lead_name or 'you'}** "
                    f"at **{lead_email or 'the email you provided'}**."
                )
            else:
                st.success("A member of our team will be in contact.")
            st.caption("If you need to adjust anything, click **Back** or restart a new quote.")

            # Export results (demo): POST status + PDF download.
            post_status = int(st.session_state.get("export_post_status") or 0)
            post_resp = str(st.session_state.get("export_post_response") or "")
            if post_status >= 200 and post_status < 300:
                st.success(f"Export POST succeeded (HTTP {post_status}).")
            elif post_status == 0 and post_resp:
                st.info(post_resp)
            elif post_status:
                st.warning(f"Export POST returned HTTP {post_status}.")

            pdf_bytes = st.session_state.get("export_pdf_bytes")
            pdf_err = str(st.session_state.get("export_pdf_error") or "")
            if isinstance(pdf_bytes, (bytes, bytearray)):
                st.download_button(
                    "Download quote (PDF)",
                    data=bytes(pdf_bytes),
                    file_name="quote.pdf",
                    mime="application/pdf",
                    use_container_width=True,
                )
            elif pdf_err:
                st.error(f"Could not generate PDF: {pdf_err}")

    _render_sidebar(book, step_index, step_labels, quote, quote_error)

if __name__ == "__main__":
    main()
