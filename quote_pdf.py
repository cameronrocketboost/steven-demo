from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from datetime import date
from io import BytesIO
from pathlib import Path
from typing import Mapping, Optional, Tuple

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas


@dataclass(frozen=True)
class QuotePdfLineItem:
    description: str
    qty: int
    amount_cents: int


@dataclass(frozen=True)
class QuotePdfTotals:
    building_amount_cents: int
    discount_cents: int
    subtotal_cents: int
    additional_charges_cents: int
    grand_total_cents: int
    downpayment_cents: int = 0
    balance_due_cents: int = 0


@dataclass(frozen=True)
class QuotePdfArtifact:
    quote_id: str
    quote_date: date
    pricebook_revision: str
    customer_name: str
    customer_email: str
    building_label: str
    building_summary: str
    line_items: Tuple[QuotePdfLineItem, ...]
    totals: QuotePdfTotals
    notes: Tuple[str, ...] = ()
    logo_png_bytes: Optional[bytes] = None
    building_preview_png_bytes: Optional[bytes] = None
    # Optional additional view pages (keys like: "front", "back", "left", "right", "isometric")
    building_views_png_bytes: Optional[Mapping[str, bytes]] = None


def format_usd(amount: int) -> str:
    """
    Format a USD currency amount from integer cents.

    The demo pricing engine uses whole dollars, but real vendor quotes often include cents
    (discount calculations, payment schedules). The PDF artifact uses cents so we can
    represent those values precisely.
    """
    if not isinstance(amount, int):
        raise TypeError(f"amount must be int cents (got {type(amount).__name__})")
    sign = "-" if amount < 0 else ""
    cents = abs(amount)
    dollars = cents / 100.0
    return f"{sign}${dollars:,.2f}"


def make_quote_pdf_bytes(artifact: QuotePdfArtifact) -> bytes:
    """
    Render a quote PDF suitable for the demo.

    Layout goal:
    - Page 1: similar shape to the vendor screenshot page 1 (header + customer/details + line items + totals).
    - Additional pages: "BUILDING VIEW" pages for each provided view (front/back/left/right/isometric).
    """
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    # Disable page compression so output is easier to diff/debug and tests can
    # reliably find markers in the bytes.
    c.setPageCompression(0)
    w, h = letter

    margin = 0.6 * inch
    x0 = margin
    y_top = h - margin
    pad = 0.15 * inch

    # Header band
    header_h = 1.35 * inch
    _rect(c, x0, y_top - header_h, w - 2 * margin, header_h, stroke=1, fill=0)

    # Logo (optional)
    if artifact.logo_png_bytes:
        try:
            img = ImageReader(BytesIO(artifact.logo_png_bytes))
            c.drawImage(
                img,
                x0 + pad,
                y_top - header_h + pad,
                width=1.35 * inch,
                height=0.95 * inch,
                mask="auto",
                preserveAspectRatio=True,
            )
        except Exception:
            pass

    # Company / Sales blocks (demo placeholders)
    c.setFont("Helvetica-Bold", 11)
    c.drawString(x0 + 1.65 * inch, y_top - 0.40 * inch, "Coast to Coast (Demo)")
    c.setFont("Helvetica", 9)
    c.drawString(x0 + 1.65 * inch, y_top - 0.62 * inch, "Local-first quoting demo")

    # Quote summary (right)
    box_w = 2.2 * inch
    box_x = w - margin - box_w
    box_y = y_top - header_h + pad
    box_h = header_h - 2 * pad
    _rect(c, box_x, box_y, box_w, box_h, stroke=1, fill=0)
    line_h = 0.22 * inch
    t_y = box_y + box_h - 0.28 * inch
    c.setFont("Helvetica-Bold", 10)
    c.drawString(box_x + pad, t_y, "Building Quote")
    c.setFont("Helvetica-Bold", 10)
    t_y -= line_h
    c.drawString(box_x + pad, t_y, f"QTE-{artifact.quote_id}")
    c.setFont("Helvetica", 9)
    t_y -= line_h
    c.drawString(box_x + pad, t_y, f"Date: {artifact.quote_date.isoformat()}")
    c.setFont("Helvetica-Bold", 11)
    t_y -= (line_h + 0.03 * inch)
    c.drawString(box_x + pad, t_y, f"Total: {format_usd(artifact.totals.grand_total_cents)}")

    y = y_top - header_h - 0.25 * inch

    # Customer + Building blocks
    left_w = 3.2 * inch
    right_w = (w - 2 * margin) - left_w - 0.15 * inch
    block_h = 1.55 * inch

    _rect(c, x0, y - block_h, left_w, block_h, stroke=1, fill=0)
    _rect(c, x0 + left_w + 0.15 * inch, y - block_h, right_w, block_h, stroke=1, fill=0)

    c.setFont("Helvetica-Bold", 9)
    c.drawString(x0 + pad, y - 0.25 * inch, "CUSTOMER DETAILS")
    # Customer name/email: keep inside the box with padding + truncation.
    cust_max_w = left_w - 2 * pad
    raw_name = (artifact.customer_name or "").strip()
    raw_email = (artifact.customer_email or "").strip()
    # NOTE: ReportLab's built-in Type1 fonts (Helvetica) can be flaky with some Unicode
    # punctuation (e.g. em dash). Use ASCII placeholders so the text always renders.
    name = raw_name or "-"
    email = raw_email or "-"
    c.setFont("Helvetica-Bold", 9)
    _draw_truncated(c, x0 + pad, y - 0.55 * inch, name, max_width=cust_max_w)
    c.setFont("Helvetica", 8)
    _draw_truncated(c, x0 + pad, y - 0.75 * inch, email, max_width=cust_max_w)

    right_x = x0 + left_w + 0.15 * inch
    # Reserve a left text column so the preview image never overlaps the "Commercial Buildings" label.
    text_col_w = 1.10 * inch
    c.setFont("Helvetica-Bold", 10)
    _draw_truncated(c, right_x + pad, y - 0.45 * inch, artifact.building_label, max_width=text_col_w - pad)
    c.setFont("Helvetica", 9)
    _draw_truncated(c, right_x + pad, y - 0.70 * inch, artifact.building_summary, max_width=text_col_w - pad)

    # Building preview image (optional) inside right block
    if artifact.building_preview_png_bytes:
        try:
            img = ImageReader(BytesIO(artifact.building_preview_png_bytes))
            # Place the preview in the RIGHT portion of the box and right-align the image inside that area.
            img_area_x = right_x + text_col_w
            img_area_w = max(0.0, right_w - text_col_w - pad)
            img_target_w = img_area_w
            img_target_h = 1.05 * inch
            img_x = img_area_x
            img_y = y - block_h + pad
            c.drawImage(
                img,
                img_x,
                img_y,
                width=img_target_w,
                height=img_target_h,
                preserveAspectRatio=True,
                anchor="e",
                mask="auto",
            )
        except Exception:
            pass

    y = y - block_h - 0.25 * inch
    # Line items table (auto-grow + paginate so totals never overlap line items)
    footer_base_y = margin + 0.35 * inch
    reserved_bottom_y = footer_base_y
    if artifact.notes:
        # Notes render above the footer; reserve space so the table never collides.
        # We show at most 3 notes, each ~0.12", plus a header gap.
        reserved_bottom_y = footer_base_y + 0.15 * inch + (min(3, len(artifact.notes)) * 0.12 * inch)

    remaining = list(artifact.line_items)
    first_page = True
    while True:
        if first_page:
            table_top_y = y
        else:
            # Continuation pages: a small title, then the table.
            table_top_y = (h - margin) - 0.55 * inch
            c.setFont("Helvetica-Bold", 10)
            c.drawString(x0, (h - margin) - 0.25 * inch, "LINE ITEMS (CONTINUED)")

        max_table_h = _max_table_height(table_top_y, reserved_bottom_y)
        if max_table_h <= 1.0 * inch:
            # Extremely defensive: ensure we always draw something valid.
            max_table_h = 1.0 * inch

        # First, see if the remaining items can fit on a single page WITH totals.
        needed_with_totals = _needed_table_height_with_totals(
            item_count=len(remaining),
            row_h=0.27 * inch,
            first_row_offset=0.55 * inch,
            totals_box_h=2.05 * inch,
            totals_bottom_pad=0.15 * inch,
            clearance_rows=2,
        )
        if needed_with_totals > max_table_h:
            can_finish_here = False
        else:
            # Double-check capacity using the SAME cutoff logic as the renderer so we never
            # end up with overlap or silently dropped items.
            table_h_candidate = max(3.6 * inch, needed_with_totals)
            row_h = 0.27 * inch
            row_start_y = table_top_y - 0.55 * inch
            table_bottom_y = table_top_y - table_h_candidate
            bottom_content_pad = max(0.45 * inch, 2.0 * row_h)
            totals_box_h = 2.05 * inch
            totals_bottom_pad = 0.15 * inch
            totals_box_top_y = table_bottom_y + totals_bottom_pad + totals_box_h
            totals_clearance_y = totals_box_top_y + (2.0 * row_h)
            min_row_y = max(table_bottom_y + bottom_content_pad, totals_clearance_y)
            if row_start_y <= min_row_y:
                capacity = 0
            else:
                capacity = int((row_start_y - min_row_y) // row_h) + 1
            can_finish_here = capacity >= len(remaining)

        if can_finish_here:
            table_h = max(3.6 * inch, needed_with_totals)
            remaining = _render_line_items_table_page(
                c,
                artifact=artifact,
                x0=x0,
                margin=margin,
                pad=pad,
                page_w=w,
                table_top_y=table_top_y,
                table_h=table_h,
                include_totals=True,
                line_items=tuple(remaining),
                reserved_bottom_y=reserved_bottom_y,
            )
            break

        # Not enough room to finish: render a continuation page WITHOUT totals,
        # using the maximum available table height to pack rows.
        table_h = max_table_h
        remaining = _render_line_items_table_page(
            c,
            artifact=artifact,
            x0=x0,
            margin=margin,
            pad=pad,
            page_w=w,
            table_top_y=table_top_y,
            table_h=table_h,
            include_totals=False,
            line_items=tuple(remaining),
            reserved_bottom_y=reserved_bottom_y,
        )
        c.showPage()
        first_page = False

    # Notes + traceability footer
    c.setFont("Helvetica", 8)
    c.setFillColor(colors.grey)
    c.drawString(x0, footer_base_y, f"Pricebook revision: {artifact.pricebook_revision}")
    c.setFillColor(colors.black)

    if artifact.notes:
        note_y = footer_base_y + 0.15 * inch
        c.setFont("Helvetica", 8)
        for n in artifact.notes[:3]:
            c.drawString(x0, note_y, f"Note: {n}")
            note_y += 0.12 * inch

    c.showPage()

    # Additional "BUILDING VIEW" pages, similar to the vendor PDF.
    views = artifact.building_views_png_bytes or {}
    view_order = ("front", "back", "left", "right", "isometric")
    view_labels = {
        "front": "FRONT",
        "back": "BACK",
        "left": "LEFT",
        "right": "RIGHT",
        "isometric": "ISOMETRIC",
    }
    for key in view_order:
        png = views.get(key)
        if not png:
            continue
        _render_building_view_page(
            c,
            png_bytes=png,
            title="BUILDING VIEW",
            label=view_labels.get(key, key.upper()),
        )
        c.showPage()

    c.save()
    return buf.getvalue()


def logo_png_bytes_from_svg(svg_path: Path) -> Optional[bytes]:
    """
    Extract embedded PNG bytes from our SVG logo file (it contains a data:image/png;base64,... payload).
    """
    try:
        svg = svg_path.read_text(encoding="utf-8")
    except Exception:
        return None

    m = re.search(r"data:image/png;base64,([A-Za-z0-9+/=]+)", svg)
    if not m:
        return None
    try:
        return base64.b64decode(m.group(1))
    except Exception:
        return None


def _rect(c: canvas.Canvas, x: float, y: float, w: float, h: float, *, stroke: int, fill: int) -> None:
    c.rect(x, y, w, h, stroke=stroke, fill=fill)


def _hline(c: canvas.Canvas, x1: float, x2: float, y: float) -> None:
    c.line(x1, y, x2, y)


def _totals_row(c: canvas.Canvas, x: float, y: float, label: str, amount_cents: int, box_w: float) -> None:
    """
    Draw one label/value row inside the totals box.

    Important: We reserve space for the right-aligned amount and truncate the label so
    long labels never collide with the amount text.
    """
    left_pad = 0.12 * inch
    right_pad = 0.12 * inch
    gap = 0.10 * inch
    amount_txt = format_usd(amount_cents)
    amount_w = c.stringWidth(amount_txt)

    label_max = box_w - left_pad - right_pad - amount_w - gap
    _draw_truncated(c, x + left_pad, y, (label or "").strip(), max_width=max(0.0, label_max))
    c.drawRightString(x + box_w - right_pad, y, amount_txt)


def _draw_truncated(c: canvas.Canvas, x: float, y: float, text: str, *, max_width: float) -> None:
    """
    Draw text truncated with ellipsis so it stays inside a box.
    """
    t = (text or "").strip()
    if not t:
        return
    if max_width <= 0:
        return
    if c.stringWidth(t) <= max_width:
        c.drawString(x, y, t)
        return
    # ASCII ellipsis for compatibility with ReportLab's built-in fonts.
    ell = "..."
    # Walk back until it fits.
    lo = 0
    hi = len(t)
    best = ""
    while lo <= hi:
        mid = (lo + hi) // 2
        cand = (t[:mid].rstrip() + ell) if mid < len(t) else t
        if c.stringWidth(cand) <= max_width:
            best = cand
            lo = mid + 1
        else:
            hi = mid - 1
    if best:
        c.drawString(x, y, best)


def _max_table_height(table_top_y: float, reserved_bottom_y: float) -> float:
    """
    Compute the maximum table height that fits between `table_top_y` and the reserved footer area.
    """
    if not isinstance(table_top_y, (int, float)) or not isinstance(reserved_bottom_y, (int, float)):
        raise TypeError("table_top_y and reserved_bottom_y must be numeric")
    if table_top_y <= reserved_bottom_y:
        return 0.0
    gap = 0.25 * inch  # visual breathing room above the footer/notes
    return max(0.0, float(table_top_y - reserved_bottom_y - gap))


def _needed_table_height_with_totals(
    *,
    item_count: int,
    row_h: float,
    first_row_offset: float,
    totals_box_h: float,
    totals_bottom_pad: float,
    clearance_rows: int,
) -> float:
    """
    Compute a table height large enough to render `item_count` rows with a bottom-right totals box.

    The totals box is bottom-aligned with `totals_bottom_pad`. We also reserve `clearance_rows`
    empty row-heights above the TOP of the totals box to guarantee no overlap risk.
    """
    if item_count < 0:
        raise ValueError("item_count must be >= 0")
    rows = max(0, item_count - 1)
    return (
        float(first_row_offset)
        + float(rows) * float(row_h)
        + float(totals_box_h)
        + float(totals_bottom_pad)
        + float(clearance_rows) * float(row_h)
    )


def _render_line_items_table_page(
    c: canvas.Canvas,
    *,
    artifact: QuotePdfArtifact,
    x0: float,
    margin: float,
    pad: float,
    page_w: float,
    table_top_y: float,
    table_h: float,
    include_totals: bool,
    line_items: Tuple[QuotePdfLineItem, ...],
    reserved_bottom_y: float,
) -> list[QuotePdfLineItem]:
    """
    Render one page of the line-items table.

    Returns the remaining (not rendered) line items.
    """
    if table_top_y - table_h <= reserved_bottom_y:
        # Don't hard-error; drawing will still clip visually but we keep output valid.
        pass

    table_w = page_w - 2 * margin
    table_bottom_y = table_top_y - table_h
    _rect(c, x0, table_bottom_y, table_w, table_h, stroke=1, fill=0)

    c.setFont("Helvetica-Bold", 9)
    c.drawString(x0 + pad, table_top_y - 0.25 * inch, "DESCRIPTION")
    c.drawRightString(page_w - margin - 1.4 * inch, table_top_y - 0.25 * inch, "QTY")
    c.drawRightString(page_w - margin - 0.15 * inch, table_top_y - 0.25 * inch, "AMOUNT")
    _hline(c, x0, page_w - margin, table_top_y - 0.35 * inch)

    row_h = 0.27 * inch
    row_y = table_top_y - 0.55 * inch
    bottom_content_pad = max(0.45 * inch, 2.0 * row_h)

    c.setFont("Helvetica", 9)
    desc_max_w = (page_w - 2 * margin) - (1.55 * inch + 0.20 * inch)  # qty+amount columns

    # Optional totals box geometry (bottom-right inside table)
    totals_box_w = 2.35 * inch
    totals_box_h = 2.05 * inch
    totals_bottom_pad = 0.15 * inch
    totals_box_bottom_y = table_bottom_y + totals_bottom_pad
    totals_box_top_y = totals_box_bottom_y + totals_box_h
    totals_clearance_y = totals_box_top_y + (2.0 * row_h)  # 1–2 line heights above the box

    rendered = 0
    for li in line_items:
        if row_y < (table_bottom_y + bottom_content_pad):
            break
        if include_totals and row_y <= totals_clearance_y:
            break
        _draw_truncated(c, x0 + pad, row_y, li.description, max_width=desc_max_w)
        c.drawRightString(page_w - margin - 1.4 * inch, row_y, str(max(1, int(li.qty))))
        c.drawRightString(page_w - margin - 0.15 * inch, row_y, format_usd(li.amount_cents))
        row_y -= row_h
        rendered += 1

    if include_totals:
        tx = page_w - margin - totals_box_w
        _rect(c, tx, totals_box_bottom_y, totals_box_w, totals_box_h, stroke=1, fill=0)

        # Totals layout inside the box (keep generous padding so labels never collide with amounts).
        left_pad = 0.12 * inch
        row_step = 0.19 * inch
        y_cursor = totals_box_top_y - 0.30 * inch

        c.setFont("Helvetica", 9)
        _totals_row(c, tx, y_cursor, "Building Amount", artifact.totals.building_amount_cents, totals_box_w)
        y_cursor -= row_step
        _totals_row(c, tx, y_cursor, "Manufacturer Discount", -abs(artifact.totals.discount_cents), totals_box_w)
        y_cursor -= row_step
        _totals_row(c, tx, y_cursor, "Sub Total", artifact.totals.subtotal_cents, totals_box_w)
        y_cursor -= row_step
        _totals_row(c, tx, y_cursor, "Additional Charges", artifact.totals.additional_charges_cents, totals_box_w)
        y_cursor -= (row_step + 0.03 * inch)

        c.setFont("Helvetica-Bold", 10)
        _totals_row(c, tx, y_cursor, "Grand Total", artifact.totals.grand_total_cents, totals_box_w)
        y_cursor -= (row_step + 0.05 * inch)

        # Pay now band + payment breakdown
        band_h = 0.20 * inch
        c.setFillColor(colors.black)
        band_y = y_cursor - band_h + 0.02 * inch
        c.rect(tx, band_y, totals_box_w, band_h, stroke=0, fill=1)
        c.setFillColor(colors.white)
        c.setFont("Helvetica-Bold", 9)
        c.drawString(tx + left_pad, y_cursor - 0.13 * inch, "Pay Now")
        c.setFillColor(colors.black)
        # Extra spacing so Downpayment text never overlaps the black band.
        y_cursor -= (band_h + 0.18 * inch)

        c.setFont("Helvetica", 9)
        _totals_row(c, tx, y_cursor, "Downpayment", artifact.totals.downpayment_cents, totals_box_w)
        y_cursor -= row_step
        _totals_row(c, tx, y_cursor, "Balance Due", artifact.totals.balance_due_cents, totals_box_w)

    return list(line_items[rendered:])


def _render_building_view_page(
    c: canvas.Canvas,
    *,
    png_bytes: bytes,
    title: str,
    label: str,
) -> None:
    """
    Render a single "BUILDING VIEW" page with a large image box and a centered label.
    """
    w, h = letter
    margin = 0.6 * inch
    pad = 0.18 * inch

    # Title
    c.setFont("Helvetica-Bold", 11)
    c.drawString(margin, h - margin, title)

    # Main framed area
    frame_x = margin
    frame_y = margin + 0.85 * inch
    frame_w = w - 2 * margin
    frame_h = h - 2 * margin - 1.35 * inch
    _rect(c, frame_x, frame_y, frame_w, frame_h, stroke=1, fill=0)

    # Image area inside frame
    img_x = frame_x + pad
    img_y = frame_y + pad + 0.20 * inch
    img_w = frame_w - 2 * pad
    img_h = frame_h - 2 * pad - 0.20 * inch

    try:
        img = ImageReader(BytesIO(png_bytes))
        c.drawImage(
            img,
            img_x,
            img_y,
            width=img_w,
            height=img_h,
            preserveAspectRatio=True,
            anchor="c",
            mask="auto",
        )
    except Exception:
        pass

    # Bottom label band
    c.setFont("Helvetica-Bold", 10)
    c.drawCentredString(w / 2.0, margin + 0.45 * inch, (label or "").strip().upper())

