from __future__ import annotations

"""
Smoke test for the demo (local, offline).

This script simulates a small number of "wizard button presses" by mutating an in-memory
state dict one step at a time, then:
- generates a quote (pricing_engine)
- renders building views (building_views)
- generates a multi-page PDF (quote_pdf)

It writes PDFs to `out/smoke_test_demo/` and exits non-zero if anything breaks.

Usage:
  python3 scripts/smoke_test_demo.py
  python3 scripts/smoke_test_demo.py --out-dir out/smoke_test_demo
"""

import argparse
import sys
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping, MutableMapping, Optional

# Allow running as `python3 scripts/smoke_test_demo.py` (module imports live at repo root).
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import local_demo_app
from building_views import (
    BuildingColorScheme,
    BuildingOpening,
    BuildingOpeningKind,
    BuildingSide,
    render_building_views_png,
)
from normalized_pricebooks import build_demo_pricebook_r29, load_normalized_pricebook
from pricing_engine import CarportStyle, PriceBook, PriceBookError, QuoteInput, RoofStyle, generate_quote
from quote_pdf import QuotePdfArtifact, QuotePdfLineItem, QuotePdfTotals, logo_png_bytes_from_svg, make_quote_pdf_bytes


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _find_r29_normalized_path() -> Path:
    root = _repo_root()
    candidates = [
        root / "out" / "Coast_To_Coast_Carports___Price_Book___R29_1" / "normalized_pricebook.json",
        root / "pricebooks" / "out" / "Coast_To_Coast_Carports___Price_Book___R29_1" / "normalized_pricebook.json",
    ]
    for p in candidates:
        if p.exists():
            return p
    raise FileNotFoundError("Could not find R29 normalized pricebook. Looked in: " + ", ".join(str(p) for p in candidates))


def _load_demo_book() -> PriceBook:
    normalized = load_normalized_pricebook(_find_r29_normalized_path())
    return build_demo_pricebook_r29(normalized)


def _style_and_roof_from_label(label: str) -> tuple[CarportStyle, RoofStyle]:
    t = (label or "").strip()
    if t == "Regular (Horizontal)":
        return (CarportStyle.REGULAR, RoofStyle.HORIZONTAL)
    if t == "A-Frame (Vertical)":
        return (CarportStyle.A_FRAME, RoofStyle.VERTICAL)
    # default
    return (CarportStyle.A_FRAME, RoofStyle.HORIZONTAL)


def _openings_to_building_openings(state: Mapping[str, object]) -> tuple[BuildingOpening, ...]:
    openings_state = state.get("openings")
    if not isinstance(openings_state, list) or not openings_state:
        return ()

    win_label = str(state.get("window_size") or "")
    ww_ft, wh_ft = local_demo_app._parse_window_size_ft(win_label)

    garage_kind = str(state.get("garage_door_type") or "None")
    garage_size = str(state.get("garage_door_size") or "")
    g_w_ft, g_h_ft = local_demo_app._parse_garage_size_ft(garage_size)

    out: list[BuildingOpening] = []
    for row in openings_state:
        if not isinstance(row, dict):
            continue
        kind = str(row.get("kind") or "door").strip().lower()
        side = str(row.get("side") or "front").strip().lower()
        offset_obj = row.get("offset_ft")
        offset_ft = int(offset_obj) if isinstance(offset_obj, (int, float)) else None

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
            if garage_kind == "None":
                continue
            out.append(
                BuildingOpening(
                    side=side_enum,
                    kind=BuildingOpeningKind.GARAGE_DOOR if garage_kind == "Roll-up" else BuildingOpeningKind.DOOR,
                    width_ft=g_w_ft,
                    height_ft=g_h_ft,
                    offset_ft=offset_ft,
                )
            )
        else:
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


def _build_quote_input(book: PriceBook, state: Mapping[str, object]) -> QuoteInput:
    style_label = str(state.get("demo_style") or "A-Frame (Horizontal)")
    style, roof_style = _style_and_roof_from_label(style_label)
    selected = local_demo_app._build_selected_options_from_state(state, book)
    return QuoteInput(
        style=style,
        roof_style=roof_style,
        gauge=14,
        width_ft=int(state.get("width_ft") or 0),
        length_ft=int(state.get("length_ft") or 0),
        leg_height_ft=int(state.get("leg_height_ft") or 0),
        include_ground_certification=bool(state.get("include_ground_certification")),
        selected_options=selected,
        closed_end_count=0,
        closed_side_count=0,
        lean_to_enabled=False,
        lean_to_width_ft=0,
        lean_to_length_ft=0,
        lean_to_placement=None,
    )


def _compute_pdf_totals(*, building_amount_cents: int, discount_pct: float, downpayment_pct: float) -> QuotePdfTotals:
    discount_cents = int(round((max(0.0, discount_pct) / 100.0) * building_amount_cents))
    subtotal_cents = building_amount_cents - discount_cents
    grand_total_cents = subtotal_cents
    downpayment_cents = int(round((max(0.0, downpayment_pct) / 100.0) * grand_total_cents))
    balance_due_cents = grand_total_cents - downpayment_cents
    return QuotePdfTotals(
        building_amount_cents=building_amount_cents,
        discount_cents=discount_cents,
        subtotal_cents=subtotal_cents,
        additional_charges_cents=0,
        grand_total_cents=grand_total_cents,
        downpayment_cents=downpayment_cents,
        balance_due_cents=balance_due_cents,
    )


def _make_pdf_bytes(*, book: PriceBook, state: Mapping[str, object], quote, out_dir: Path, label: str) -> bytes:
    logo_svg_path = _repo_root() / "assets" / "coast to coast image.svg"
    logo_bytes = logo_png_bytes_from_svg(logo_svg_path) if logo_svg_path.exists() else None

    openings = _openings_to_building_openings(state)
    views = render_building_views_png(
        width_ft=int(state.get("width_ft") or 0),
        length_ft=int(state.get("length_ft") or 0),
        height_ft=int(state.get("leg_height_ft") or 0),
        colors=BuildingColorScheme(
            roof=str(state.get("roof_color") or "White"),
            trim=str(state.get("trim_color") or "White"),
            sides=str(state.get("side_color") or "White"),
        ),
        openings=openings,
        view_names=("isometric", "front", "back", "left", "right"),
        canvas_px=(900, 520),
    )

    quote_id = f"smoke-{label}"
    today = datetime.now(timezone.utc).date()
    artifact = QuotePdfArtifact(
        quote_id=quote_id,
        quote_date=today,
        pricebook_revision=book.revision,
        customer_name=str(state.get("lead_name") or "Demo Customer").strip(),
        customer_email=str(state.get("lead_email") or "demo@example.com").strip(),
        building_label="Commercial Buildings",
        building_summary=f"{int(state.get('width_ft') or 0)} x {int(state.get('length_ft') or 0)} x {int(state.get('leg_height_ft') or 0)}",
        line_items=tuple(
            QuotePdfLineItem(description=str(li.description), qty=1, amount_cents=int(li.amount_usd) * 100)
            for li in quote.line_items
        ),
        totals=_compute_pdf_totals(
            building_amount_cents=int(quote.total_usd) * 100,
            discount_pct=float(state.get("manufacturer_discount_pct") or 0.0),
            downpayment_pct=float(state.get("downpayment_pct") or 0.0),
        ),
        notes=tuple(str(n) for n in (quote.notes or ())),
        logo_png_bytes=logo_bytes,
        building_preview_png_bytes=views.get("isometric"),
        building_views_png_bytes=views,
    )

    pdf_bytes = make_quote_pdf_bytes(artifact)
    if not pdf_bytes.startswith(b"%PDF"):
        raise RuntimeError("Generated PDF does not start with %PDF header.")
    # Shallow text markers to catch obvious template/render failures.
    for marker in (b"Downpayment", b"Balance Due", b"Pricebook"):
        if marker not in pdf_bytes:
            raise RuntimeError(f"Generated PDF missing expected marker: {marker!r}")

    out_path = out_dir / f"{label}.pdf"
    out_path.write_bytes(pdf_bytes)
    return pdf_bytes


@dataclass(frozen=True)
class Step:
    label: str
    apply: Callable[[MutableMapping[str, object]], None]


def _run_scenario(*, name: str, book: PriceBook, base_state: dict[str, object], steps: list[Step], out_dir: Path) -> None:
    state: dict[str, object] = dict(base_state)

    print("")
    print("=" * 72)
    print(f"SCENARIO: {name}")
    print("=" * 72)

    for i, step in enumerate(steps, start=1):
        step.apply(state)

        inp = _build_quote_input(book, state)
        quote = generate_quote(inp, book)

        label = f"{name.replace(' ', '_').lower()}_{i:02d}_{step.label.replace(' ', '_').lower()}"
        _make_pdf_bytes(book=book, state=state, quote=quote, out_dir=out_dir, label=label)

        print(f"[{i}/{len(steps)}] {step.label}")
        print(f"  - size: {int(state.get('width_ft') or 0)}x{int(state.get('length_ft') or 0)}  leg: {int(state.get('leg_height_ft') or 0)}")
        print(f"  - total: ${quote.total_usd:,.0f}")
        print(f"  - pdf: {label}.pdf")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--out-dir",
        default=str(_repo_root() / "out" / "smoke_test_demo"),
        help="Directory to write PDFs into (default: out/smoke_test_demo).",
    )
    args = parser.parse_args(argv)

    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    book = _load_demo_book()

    base_state = local_demo_app._default_state(book)
    # Add minimal lead + colors so PDFs are non-empty and visually varied.
    base_state.update(
        {
            "lead_name": "Demo Customer",
            "lead_email": "demo@example.com",
            "roof_color": "Blue",
            "trim_color": "Black",
            "side_color": "Tan",
            "manufacturer_discount_pct": 0.0,
            "downpayment_pct": 18.0,
        }
    )

    # Scenario 1: baseline quote (no add-ons)
    s1_steps = [
        Step(
            label="set_base",
            apply=lambda s: s.update(
                {
                    "demo_style": "A-Frame (Horizontal)",
                    "width_ft": 12,
                    "length_ft": 21,
                    "leg_height_ft": 6,
                    "include_ground_certification": False,
                    "selected_option_codes": [],
                    "openings": [],
                }
            ),
        ),
        Step(
            label="apply_terms",
            apply=lambda s: s.update({"manufacturer_discount_pct": 5.0, "downpayment_pct": 18.0}),
        ),
    ]

    # Scenario 2: openings + one option (simulate "add door/window" presses)
    def _add_opening(kind: str, side: str, offset_ft: int) -> Callable[[MutableMapping[str, object]], None]:
        def _apply(s: MutableMapping[str, object]) -> None:
            openings = s.get("openings")
            if not isinstance(openings, list):
                openings = []
                s["openings"] = openings
            seq = int(s.get("opening_seq") or 1)
            openings.append({"id": seq, "kind": kind, "side": side, "offset_ft": offset_ft})
            s["opening_seq"] = seq + 1

        return _apply

    s2_steps = [
        Step(
            label="set_base",
            apply=lambda s: s.update(
                {
                    "demo_style": "A-Frame (Horizontal)",
                    "width_ft": 18,
                    "length_ft": 26,
                    "leg_height_ft": 10,
                    "include_ground_certification": False,
                    "selected_option_codes": [],
                    "walk_in_door_type": "Standard 36x80",
                    "window_size": "24x36",
                    "garage_door_type": "Roll-up",
                    "garage_door_size": "10x8",
                    "openings": [],
                    "opening_seq": 1,
                }
            ),
        ),
        Step(label="press_add_door", apply=_add_opening("door", "front", 4)),
        Step(label="press_add_window", apply=_add_opening("window", "right", 6)),
        Step(label="press_add_garage", apply=_add_opening("garage", "front", 8)),
        Step(
            label="press_toggle_ground_cert",
            apply=lambda s: s.update({"include_ground_certification": True}),
        ),
        Step(
            label="press_add_option_j_trim",
            apply=lambda s: s.update({"selected_option_codes": ["J_TRIM"]}),
        ),
    ]

    # Scenario 3: commercial extrapolation sizing
    s3_steps = [
        Step(
            label="set_base",
            apply=lambda s: s.update(
                {
                    "demo_style": "A-Frame (Vertical)",
                    "width_ft": 40,
                    "length_ft": 60,
                    "leg_height_ft": 12,
                    "include_ground_certification": False,
                    "selected_option_codes": [],
                    "walk_in_door_type": "Standard 36x80",
                    "window_size": "30x36",
                    "garage_door_type": "Roll-up",
                    "garage_door_size": "10x10",
                    "openings": [],
                    "opening_seq": 1,
                }
            ),
        ),
        Step(label="press_add_garage", apply=_add_opening("garage", "front", 10)),
        Step(label="press_add_door", apply=_add_opening("door", "front", 3)),
        Step(label="press_add_window", apply=_add_opening("window", "left", 12)),
        Step(label="apply_terms", apply=lambda s: s.update({"manufacturer_discount_pct": 10.0, "downpayment_pct": 18.0})),
    ]

    _run_scenario(name="baseline", book=book, base_state=base_state, steps=s1_steps, out_dir=out_dir)
    _run_scenario(name="openings_and_options", book=book, base_state=base_state, steps=s2_steps, out_dir=out_dir)
    _run_scenario(name="commercial_extrap", book=book, base_state=base_state, steps=s3_steps, out_dir=out_dir)

    print("")
    print(f"OK: wrote PDFs to {out_dir}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PriceBookError as exc:
        print(f"FAIL: PriceBookError: {exc}", file=sys.stderr)
        raise SystemExit(2)
    except Exception:
        traceback.print_exc()
        raise SystemExit(1)

