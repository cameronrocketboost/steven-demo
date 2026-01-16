from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from building_views import (
    BuildingColorScheme,
    BuildingOpening,
    BuildingOpeningKind,
    BuildingSide,
    render_building_views_png,
)
from normalized_pricebooks import (
    build_pricebook_from_normalized,
    find_normalized_pricebooks,
    load_normalized_pricebook,
)
from pricing_engine import (
    CarportStyle,
    PriceBookError,
    QuoteInput,
    RoofStyle,
    SectionPlacement,
    SelectedOption,
    generate_quote,
)
from quote_pdf import QuotePdfArtifact, QuotePdfLineItem, QuotePdfTotals, make_quote_pdf_bytes


@dataclass(frozen=True)
class VendorScreenshotFixture:
    """
    Minimal vendor quote fixture reconstructed from the provided screenshots (page 1).

    Notes:
    - The Coast-to-Coast R29/R30 normalized pricebooks in this repo do NOT include a 40x60 vertical base
      matrix. Running this simulation will therefore price at the nearest available sizes.
    - Many vendor-specific line items (e.g. commercial chain-hoist door) are not modeled in our demo engine.
    """

    width_ft: int
    length_ft: int
    height_ft: int
    roof_style: str
    gauge: int
    roof_color: str
    trim_color: str
    side_color: str
    wind_snow_label: str
    on_center_label: str
    # Totals from vendor PDF (cents) for comparison
    vendor_grand_total_cents: int


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _find_r29_normalized_path() -> Path:
    root = _repo_root()
    candidate_out_dirs = [
        root / "out",
        root / "pricebooks" / "out",
    ]
    for out_dir in candidate_out_dirs:
        if not out_dir.exists():
            continue
        for p in find_normalized_pricebooks(out_dir):
            n = load_normalized_pricebook(p)
            if n.status == "ok" and "R29" in n.source.upper():
                return p
    raise FileNotFoundError("Could not locate an R29 normalized_pricebook.json under ./out or ./pricebooks/out")


def _safe_selected_options(*, codes: Sequence[SelectedOption], available_codes: set[str]) -> tuple[SelectedOption, ...]:
    out: list[SelectedOption] = []
    for s in codes:
        code = s.code.strip().upper()
        if code and code in available_codes:
            out.append(SelectedOption(code=code, placement=s.placement))
    return tuple(out)


def main() -> None:
    fixture = VendorScreenshotFixture(
        width_ft=40,
        length_ft=60,
        height_ft=14,
        roof_style="Vertical",
        gauge=14,
        # Different colors to visually confirm the renderer is respecting them.
        roof_color="Blue",
        trim_color="Black",
        side_color="Tan",
        wind_snow_label="140 MPH + 35 PSF Certified",
        on_center_label="5 Feet",
        vendor_grand_total_cents=3_379_950,  # $33,799.50 (from screenshot)
    )

    normalized_path = _find_r29_normalized_path()
    normalized = load_normalized_pricebook(normalized_path)

    # Use the full vertical-roof base matrix + option list (not the demo-composite book)
    # so the simulator represents the best available extracted data.
    book = build_pricebook_from_normalized(
        normalized,
        base_matrix_title="VERTICAL ROOF STYLE",
        option_table_title="OPTION LIST",
        assume_style=CarportStyle.A_FRAME,
        assume_roof=RoofStyle.VERTICAL,
    )

    available_codes = set(book.option_prices_by_length_usd.keys())
    requested_options = [
        # From the screenshot: 2x walk-in doors (front + right) and 2x windows (right).
        SelectedOption(code="WALK_IN_DOOR_STANDARD_36X80", placement=SectionPlacement.FRONT),
        SelectedOption(code="WALK_IN_DOOR_STANDARD_36X80", placement=SectionPlacement.RIGHT),
        SelectedOption(code="WINDOW_24X36", placement=SectionPlacement.RIGHT),
        SelectedOption(code="WINDOW_24X36", placement=SectionPlacement.RIGHT),
        # Note: vendor's "Front - 12x12' Garage Door (Commercial) Chain Hoist" is not modeled.
    ]
    selected_options = _safe_selected_options(codes=requested_options, available_codes=available_codes)
    missing_option_codes = sorted({s.code for s in requested_options} - {s.code for s in selected_options})

    inp = QuoteInput(
        style=CarportStyle.A_FRAME,
        roof_style=RoofStyle.VERTICAL,
        gauge=fixture.gauge,
        width_ft=fixture.width_ft,
        length_ft=fixture.length_ft,
        leg_height_ft=fixture.height_ft,
        include_ground_certification=False,
        selected_options=selected_options,
        closed_end_count=2,   # Ends - Closed | Vertical (qty 2)
        closed_side_count=2,  # Sides - Closed | Vertical (qty 2)
        lean_to_enabled=False,
        lean_to_width_ft=0,
        lean_to_length_ft=0,
        lean_to_placement=None,
    )

    try:
        quote = generate_quote(inp, book)
    except PriceBookError as exc:
        raise SystemExit(f"Simulation failed while generating quote: {exc}") from exc

    # Openings for drawing: reconstructing the screenshot's doors/windows (including unmodeled garage door).
    openings = (
        # Front: 12x12 garage door + 3x7 walk-in
        BuildingOpening(side=BuildingSide.FRONT, kind=BuildingOpeningKind.GARAGE_DOOR, width_ft=12, height_ft=12),
        BuildingOpening(side=BuildingSide.FRONT, kind=BuildingOpeningKind.DOOR, width_ft=3, height_ft=7, offset_ft=30),
        # Right: 2x windows + walk-in
        BuildingOpening(side=BuildingSide.RIGHT, kind=BuildingOpeningKind.WINDOW, width_ft=2, height_ft=3, offset_ft=20),
        BuildingOpening(side=BuildingSide.RIGHT, kind=BuildingOpeningKind.WINDOW, width_ft=2, height_ft=3, offset_ft=35),
        BuildingOpening(side=BuildingSide.RIGHT, kind=BuildingOpeningKind.DOOR, width_ft=3, height_ft=7, offset_ft=52),
    )

    # Render all view images (same renderer used by Streamlit + PDF export).
    views = render_building_views_png(
        width_ft=fixture.width_ft,
        length_ft=fixture.length_ft,
        height_ft=fixture.height_ft,
        colors=BuildingColorScheme(roof=fixture.roof_color, trim=fixture.trim_color, sides=fixture.side_color),
        openings=openings,
        view_names=("isometric", "front", "back", "left", "right"),
        canvas_px=(900, 520),
    )
    preview_png = views["isometric"]

    # Build PDF artifact from the computed quote (amounts in cents).
    quote_id = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    # Vendor screenshot uses 18% downpayment
    downpayment_pct = 0.18
    artifact = QuotePdfArtifact(
        quote_id=quote_id,
        quote_date=datetime.now(timezone.utc).date(),
        pricebook_revision=book.revision,
        customer_name="Demo Quote (from screenshot)",
        customer_email="",
        building_label="Commercial Buildings (sim)",
        building_summary=f"{fixture.width_ft} x {fixture.length_ft} x {fixture.height_ft}",
        line_items=tuple(
            QuotePdfLineItem(description=li.description, qty=1, amount_cents=int(li.amount_usd) * 100)
            for li in quote.line_items
        ),
        totals=QuotePdfTotals(
            building_amount_cents=int(quote.total_usd) * 100,
            discount_cents=0,
            subtotal_cents=int(quote.total_usd) * 100,
            additional_charges_cents=0,
            grand_total_cents=int(quote.total_usd) * 100,
            downpayment_cents=int(round((int(quote.total_usd) * 100) * downpayment_pct)),
            balance_due_cents=(int(quote.total_usd) * 100) - int(round((int(quote.total_usd) * 100) * downpayment_pct)),
        ),
        notes=tuple(
            [
                *list(quote.notes),
                f"Colors: roof={fixture.roof_color}, trim={fixture.trim_color}, sides={fixture.side_color}",
                f"Vendor wind/snow: {fixture.wind_snow_label}",
                f"Vendor on-center: {fixture.on_center_label}",
            ]
        ),
        logo_png_bytes=None,
        building_preview_png_bytes=preview_png,
        building_views_png_bytes=views,
    )
    pdf_bytes = make_quote_pdf_bytes(artifact)

    out_dir = _repo_root() / "out" / "vendor_sim"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "demo_quote_sim.pdf").write_bytes(pdf_bytes)
    (out_dir / "demo_quote_sim_preview.png").write_bytes(preview_png)
    for k, png in views.items():
        (out_dir / f"demo_quote_sim_view_{k}.png").write_bytes(png)

    report = {
        "fixture": {
            "width_ft": fixture.width_ft,
            "length_ft": fixture.length_ft,
            "height_ft": fixture.height_ft,
            "roof_style": fixture.roof_style,
            "gauge": fixture.gauge,
            "colors": {"roof": fixture.roof_color, "trim": fixture.trim_color, "sides": fixture.side_color},
            "vendor_grand_total_cents": fixture.vendor_grand_total_cents,
        },
        "pricebook": {"revision": book.revision, "normalized_path": str(normalized_path)},
        "unmodeled_vendor_items": [
            "Front - 12x12' Garage Door (Commercial) Chain Hoist",
            "Manufacturer Discount (cents)",
            "Wind/Snow rating certification (as a priced/validated selector)",
            "Distance on center (as a priced/validated selector)",
            "Closed sides/ends pricing may not correspond to vendor's commercial building rules",
        ],
        "engine_result": {
            "normalized_width_ft": quote.normalized_width_ft,
            "normalized_length_ft": quote.normalized_length_ft,
            "total_cents": int(quote.total_usd) * 100,
            "notes": list(quote.notes),
            "line_items": [{"code": li.code, "description": li.description, "amount_cents": li.amount_usd * 100} for li in quote.line_items],
        },
        "diff": {
            "vendor_grand_total_cents": fixture.vendor_grand_total_cents,
            "our_total_cents": int(quote.total_usd) * 100,
            "delta_cents": int(quote.total_usd) * 100 - fixture.vendor_grand_total_cents,
            "missing_option_codes": missing_option_codes,
            "size_limitations": {
                "requested": {"width_ft": fixture.width_ft, "length_ft": fixture.length_ft},
                "priced_as": {"width_ft": quote.normalized_width_ft, "length_ft": quote.normalized_length_ft},
                "reason": "Current R29/R30 normalized VERTICAL ROOF STYLE matrix only supports widths 12–24 and lengths 20–50.",
            },
        },
        "artifacts": {
            "pdf_path": str(out_dir / "demo_quote_sim.pdf"),
            "preview_png_path": str(out_dir / "demo_quote_sim_preview.png"),
            "view_png_paths": {k: str(out_dir / f"demo_quote_sim_view_{k}.png") for k in sorted(views.keys())},
        },
    }
    (out_dir / "demo_quote_sim_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")

    print("Wrote:")
    print(f"- {out_dir / 'demo_quote_sim.pdf'}")
    print(f"- {out_dir / 'demo_quote_sim_preview.png'}")
    print(f"- {out_dir / 'demo_quote_sim_report.json'}")
    print("")
    print("Key results:")
    print(f"- Vendor grand total: {fixture.vendor_grand_total_cents/100.0:,.2f}")
    print(f"- Our total (from extracted R29): {quote.total_usd:,.0f}.00 (priced as {quote.normalized_width_ft}x{quote.normalized_length_ft})")
    if missing_option_codes:
        print(f"- Missing option codes (not priced in this book): {', '.join(missing_option_codes)}")


if __name__ == '__main__':
    main()

