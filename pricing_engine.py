from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


class CarportStyle(str, Enum):
    A_FRAME = "A-FRAME"
    REGULAR = "REGULAR"


class RoofStyle(str, Enum):
    HORIZONTAL = "HORIZONTAL"
    VERTICAL = "VERTICAL"


class SectionPlacement(str, Enum):
    FRONT = "FRONT"
    BACK = "BACK"
    LEFT = "LEFT"
    RIGHT = "RIGHT"


class PriceBookError(ValueError):
    pass


@dataclass(frozen=True)
class SelectedOption:
    code: str
    placement: Optional[SectionPlacement]


@dataclass(frozen=True)
class QuoteInput:
    style: CarportStyle
    roof_style: RoofStyle
    gauge: int
    width_ft: int
    length_ft: int
    leg_height_ft: int
    include_ground_certification: bool
    selected_options: Tuple[SelectedOption, ...] = ()
    closed_end_count: int = 0
    closed_side_count: int = 0
    lean_to_enabled: bool = False
    lean_to_width_ft: int = 0
    lean_to_length_ft: int = 0
    lean_to_placement: Optional[SectionPlacement] = None


@dataclass(frozen=True)
class LineItem:
    code: str
    description: str
    amount_usd: int


@dataclass(frozen=True)
class QuoteResult:
    pricebook_revision: str
    normalized_width_ft: int
    normalized_length_ft: int
    line_items: Tuple[LineItem, ...]
    total_usd: int
    notes: Tuple[str, ...]


@dataclass(frozen=True)
class PriceBook:
    revision: str
    allowed_widths_ft: Tuple[int, ...]
    allowed_lengths_ft: Tuple[int, ...]
    allowed_leg_heights_ft: Tuple[int, ...]
    # key: (style, roof_style, gauge, width_ft, length_ft) -> base price
    base_prices_usd: Mapping[Tuple[CarportStyle, RoofStyle, int, int, int], int]
    # key: option code -> (length_ft -> price)
    option_prices_by_length_usd: Mapping[str, Mapping[int, int]]
    # key: leg_height_ft -> (length_ft -> price)
    leg_height_addon_by_length_usd: Mapping[int, Mapping[int, int]]
    # key: leg_height_ft -> (width_ft -> price) for closed end (per end)
    closed_end_prices_by_leg_height_width_usd: Mapping[int, Mapping[int, int]] = field(
        default_factory=dict
    )
    # key: width_ft -> price (per side)
    vertical_end_add_by_width_usd: Mapping[int, int] = field(default_factory=dict)

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
        pass


# endregion agent log


def _next_size_up(value: int, allowed: Sequence[int]) -> int:
    if not allowed:
        raise PriceBookError("price book has no allowed sizes")
    for a in allowed:
        if value <= a:
            return a
    return allowed[-1]


def _validate_positive_int(name: str, value: int) -> None:
    if not isinstance(value, int) or value <= 0:
        raise PriceBookError(f"{name} must be a positive integer (got {value!r})")


def generate_quote(inp: QuoteInput, book: PriceBook) -> QuoteResult:
    """
    Generate a single itemized quote for the demo.

    This is intentionally small-scope: base price + leg height add-on + optional ground certification.
    """
    # region agent log
    _agent_log(
        hypothesis_id="C",
        location="pricing_engine.py:generate_quote:entry",
        message="generate_quote called",
        data={
            "width_ft": inp.width_ft,
            "length_ft": inp.length_ft,
            "leg_height_ft": inp.leg_height_ft,
            "include_ground_certification": inp.include_ground_certification,
            "closed_end_count": inp.closed_end_count,
            "closed_side_count": inp.closed_side_count,
            "lean_to_enabled": inp.lean_to_enabled,
            "selected_options_len": len(inp.selected_options),
            "selected_option_codes": [s.code for s in inp.selected_options],
        },
    )
    # endregion agent log

    _validate_positive_int("width_ft", inp.width_ft)
    _validate_positive_int("length_ft", inp.length_ft)
    _validate_positive_int("leg_height_ft", inp.leg_height_ft)
    _validate_positive_int("gauge", inp.gauge)
    if inp.closed_end_count < 0 or inp.closed_side_count < 0:
        raise PriceBookError("Closed end/side counts must be zero or positive.")

    if inp.roof_style == RoofStyle.VERTICAL and inp.style != CarportStyle.A_FRAME:
        raise PriceBookError("Vertical roof is only available on A-FRAME style carports (per option list note).")

    base_cells: Dict[Tuple[int, int], int] = {}
    widths_set = set()
    lengths_set = set()
    for (style, roof_style, gauge, w_ft, l_ft), price in book.base_prices_usd.items():
        if style == inp.style and roof_style == inp.roof_style and gauge == inp.gauge:
            base_cells[(w_ft, l_ft)] = int(price)
            widths_set.add(int(w_ft))
            lengths_set.add(int(l_ft))

    # Fall back to the broader "allowed sizes" only if the base matrix is missing for this style/roof/gauge.
    widths = tuple(sorted(widths_set)) if widths_set else tuple(book.allowed_widths_ft)
    lengths = tuple(sorted(lengths_set)) if lengths_set else tuple(book.allowed_lengths_ft)
    if not widths or not lengths or not base_cells:
        raise PriceBookError(
            f"No base pricing matrix available for {inp.style.value} / {inp.roof_style.value} / {inp.gauge} ga."
        )

    # Choose the "max available cell" deterministically for commercial extrapolation anchoring.
    max_w_ft, max_l_ft = max(base_cells.keys(), key=lambda wl: (wl[0] * wl[1], wl[0], wl[1]))
    max_width = int(max_w_ft)
    max_length = int(max_l_ft)

    notes: List[str] = []

    # Commercial coverage (demo): if the requested size exceeds the available matrix,
    # we extrapolate beyond the largest available size instead of silently pricing as the max.
    wants_commercial_extrap = inp.width_ft > max_width or inp.length_ft > max_length

    # Find a valid (width, length) that exists in the base matrix.
    # For in-matrix sizing we use "next size up" on each axis; for commercial we clamp to the max cell.
    if wants_commercial_extrap:
        pricing_width = max_width
        pricing_length = max_length
    else:
        width_candidates = [w for w in widths if inp.width_ft <= w] or [widths[-1]]
        length_candidates = [l for l in lengths if inp.length_ft <= l] or [lengths[-1]]
        pricing_width = width_candidates[-1]
        pricing_length = length_candidates[-1]
        found = False
        # Prefer minimal overage: smallest width >= requested, then smallest length >= requested.
        for w in width_candidates:
            for l in length_candidates:
                if (w, l) in base_cells:
                    pricing_width, pricing_length = w, l
                    found = True
                    break
            if found:
                break
        if not found:
            # Fallback: find any cell that covers requested dims (min overage area).
            covering = [
                (w, l)
                for (w, l) in base_cells.keys()
                if w >= inp.width_ft and l >= inp.length_ft
            ]
            if covering:
                pricing_width, pricing_length = min(
                    covering,
                    key=lambda wl: ((wl[0] - inp.width_ft) * (wl[1] - inp.length_ft), wl[0] - inp.width_ft, wl[1] - inp.length_ft),
                )
            else:
                pricing_width, pricing_length = max_width, max_length

    normalized_width = inp.width_ft if wants_commercial_extrap else _next_size_up(inp.width_ft, widths)
    normalized_length = inp.length_ft if wants_commercial_extrap else _next_size_up(inp.length_ft, lengths)

    if not wants_commercial_extrap and (normalized_width != inp.width_ft or normalized_length != inp.length_ft):
        notes.append("Per manufacturer pricing rules, sizes not in the matrix are priced at the next size up.")
    if wants_commercial_extrap:
        notes.append(
            "Commercial sizing: requested size exceeds the extracted matrix; base pricing is extrapolated "
            f"beyond the max available {max_width}x{max_length}."
        )

    if inp.leg_height_ft >= 13:
        notes.append("Requires customer-provided lift for installation (13' or taller).")

    base_price = base_cells.get((int(pricing_width), int(pricing_length)))
    if base_price is None:
        raise PriceBookError(
            f"No base price found for: ({inp.style.value}, {inp.roof_style.value}, {inp.gauge}, {pricing_width}, {pricing_length})"
        )

    line_items: List[LineItem] = [
        LineItem(
            code="BASE",
            description=f"Base price ({inp.style.value}, {inp.roof_style.value} roof, {inp.gauge} ga, {pricing_width}x{pricing_length})",
            amount_usd=base_price,
        )
    ]

    if wants_commercial_extrap:
        extra = _commercial_extrapolated_base_delta_usd(
            book=book,
            style=inp.style,
            roof_style=inp.roof_style,
            gauge=inp.gauge,
            requested_width_ft=inp.width_ft,
            requested_length_ft=inp.length_ft,
            max_width_ft=max_width,
            max_length_ft=max_length,
        )
        if extra > 0:
            line_items.append(
                LineItem(
                    code="COMMERCIAL_SIZE_EXTRAP",
                    description=f"Commercial size extrapolation ({inp.width_ft}x{inp.length_ft})",
                    amount_usd=extra,
                )
            )
            notes.append(
                "Commercial sizing note: option/leg-height tables are still priced using the closest available "
                f"length column ({pricing_length} ft)."
            )

    if inp.lean_to_enabled:
        _validate_positive_int("lean_to_width_ft", inp.lean_to_width_ft)
        _validate_positive_int("lean_to_length_ft", inp.lean_to_length_ft)
        lean_width = _next_size_up(inp.lean_to_width_ft, book.allowed_widths_ft)
        lean_length = _next_size_up(inp.lean_to_length_ft, book.allowed_lengths_ft)
        lean_key = (inp.style, inp.roof_style, inp.gauge, lean_width, lean_length)
        lean_price = book.base_prices_usd.get(lean_key)
        if lean_price is None:
            raise PriceBookError(f"No base price found for lean-to: {lean_key}")
        if lean_width != inp.lean_to_width_ft or lean_length != inp.lean_to_length_ft:
            notes.append(
                "Per manufacturer pricing rules, lean-to sizes not in the matrix are priced at the next size up."
            )
        placement_txt = (
            f" ({inp.lean_to_placement.value})"
            if isinstance(inp.lean_to_placement, SectionPlacement)
            else ""
        )
        line_items.append(
            LineItem(
                code="LEAN_TO",
                description=f"Lean-to add-on{placement_txt} ({lean_width}x{lean_length})",
                amount_usd=lean_price,
            )
        )

    leg_height_prices = book.leg_height_addon_by_length_usd.get(inp.leg_height_ft)
    if leg_height_prices is None:
        raise PriceBookError(f"Unsupported leg height: {inp.leg_height_ft} ft")
    option_pricing_length = _option_pricing_length_for_vertical_short_rule(
        roof_style=inp.roof_style,
        requested_length_ft=pricing_length,
        available_lengths=tuple(sorted(leg_height_prices.keys())),
        notes=notes,
    )
    leg_addon, leg_note = _lookup_by_length_next_size_up(
        value_by_length=leg_height_prices,
        requested_length_ft=option_pricing_length,
        label=f"leg height add-on ({inp.leg_height_ft} ft)",
    )
    if leg_note is not None:
        notes.append(leg_note)
    if leg_addon > 0:
        line_items.append(
            LineItem(
                code="LEG_HEIGHT",
                description=f"Leg height add-on ({inp.leg_height_ft} ft)",
                amount_usd=leg_addon,
            )
        )

    if inp.include_ground_certification:
        gc_map = book.option_prices_by_length_usd.get("GROUND_CERTIFICATION", {})
        gc, gc_note = _lookup_by_length_next_size_up(
            value_by_length=gc_map,
            requested_length_ft=option_pricing_length,
            label="ground certification",
        )
        if gc_note is not None:
            notes.append(gc_note)
        line_items.append(
            LineItem(
                code="GROUND_CERTIFICATION",
                description="Ground certification",
                amount_usd=gc,
            )
        )

    if inp.closed_end_count:
        price, note = _lookup_by_height_width_next_size_up(
            value_by_height_width=book.closed_end_prices_by_leg_height_width_usd,
            requested_height_ft=inp.leg_height_ft,
            requested_width_ft=normalized_width,
            label="closed end",
        )
        if note is not None:
            notes.append(note)
        line_items.append(
            LineItem(
                code="CLOSED_END",
                description=f"Closed end x{inp.closed_end_count}",
                amount_usd=price * inp.closed_end_count,
            )
        )

    if inp.closed_side_count:
        side_price, side_note = _lookup_by_width_next_size_up(
            value_by_width=book.vertical_end_add_by_width_usd,
            requested_width_ft=normalized_width,
            label="closed side",
        )
        if side_note is not None:
            notes.append(side_note)
        line_items.append(
            LineItem(
                code="CLOSED_SIDE",
                description=f"Closed side x{inp.closed_side_count}",
                amount_usd=side_price * inp.closed_side_count,
            )
        )

    for sel in inp.selected_options:
        code = sel.code.strip().upper()
        if not code:
            continue
        # Avoid double-charging the special-case toggle.
        if inp.include_ground_certification and code == "GROUND_CERTIFICATION":
            continue
        m = book.option_prices_by_length_usd.get(code, {})
        price, opt_note = _lookup_by_length_next_size_up(
            value_by_length=m,
            requested_length_ft=option_pricing_length,
            label=code,
        )
        if opt_note is not None:
            notes.append(opt_note)
        placement_txt = f" ({sel.placement.value})" if isinstance(sel.placement, SectionPlacement) else ""
        line_items.append(
            LineItem(
                code=code,
                description=f"{code.replace('_', ' ').title()}{placement_txt}",
                amount_usd=price,
            )
        )

    # region agent log
    _agent_log(
        hypothesis_id="C",
        location="pricing_engine.py:generate_quote:pre_group",
        message="Line items before grouping",
        data={
            "line_items_len": len(line_items),
            "line_items": [{"code": li.code, "desc": li.description, "amt": li.amount_usd} for li in line_items],
        },
    )
    # endregion agent log

    line_items_grouped = _group_line_items(line_items)

    # region agent log
    _agent_log(
        hypothesis_id="C",
        location="pricing_engine.py:generate_quote:post_group",
        message="Line items after grouping",
        data={
            "line_items_grouped_len": len(line_items_grouped),
            "line_items_grouped": [
                {"code": li.code, "desc": li.description, "amt": li.amount_usd} for li in line_items_grouped
            ],
        },
    )
    # endregion agent log

    total = sum(li.amount_usd for li in line_items_grouped)
    return QuoteResult(
        pricebook_revision=book.revision,
        normalized_width_ft=normalized_width,
        normalized_length_ft=normalized_length,
        line_items=line_items_grouped,
        total_usd=total,
        notes=tuple(notes),
    )


def _option_pricing_length_for_vertical_short_rule(
    *,
    roof_style: RoofStyle,
    requested_length_ft: int,
    available_lengths: Sequence[int],
    notes: List[str],
) -> int:
    """
    Coast-to-Coast rule: \"Vertical Buildings Are 1' Shorter Than Horizontal.\"

    In the source price book, some base matrices use 20/25/30/35 for vertical roofs, while
    option tables (leg-height, certification, etc.) are often keyed by 21/26/31/36.

    For demo clarity, if the roof is vertical and there is no exact match for the requested
    length in the option table, but (length+1) exists, we price options using (length+1).
    """
    if roof_style != RoofStyle.VERTICAL:
        return requested_length_ft
    if requested_length_ft in available_lengths:
        return requested_length_ft
    if (requested_length_ft + 1) in available_lengths:
        notes.append(
            "Per manufacturer rules, vertical-roof option pricing uses the corresponding horizontal length "
            f"column: {requested_length_ft + 1} ft."
        )
        return requested_length_ft + 1
    return requested_length_ft


def _lookup_by_length_next_size_up(
    *,
    value_by_length: Mapping[int, int],
    requested_length_ft: int,
    label: str,
) -> Tuple[int, Optional[str]]:
    """
    Look up a price by length. If the exact length isn't present, price at next available length up.
    Returns (price, note_if_adjusted).
    """
    if not value_by_length:
        raise PriceBookError(f"No pricing table available for {label}")
    if requested_length_ft in value_by_length:
        return value_by_length[requested_length_ft], None

    keys = sorted(value_by_length.keys())
    next_len = _next_size_up(requested_length_ft, keys)
    price = value_by_length.get(next_len)
    if price is None:
        raise PriceBookError(f"No {label} price for length {requested_length_ft} ft (or next size up)")
    return (
        price,
        f"Per manufacturer rules, {label} was priced at the next length up: {next_len} ft.",
    )


def _lookup_by_width_next_size_up(
    *,
    value_by_width: Mapping[int, int],
    requested_width_ft: int,
    label: str,
) -> Tuple[int, Optional[str]]:
    if not value_by_width:
        raise PriceBookError(f"No pricing table available for {label}")
    if requested_width_ft in value_by_width:
        return value_by_width[requested_width_ft], None
    widths = sorted(value_by_width.keys())
    next_width = _next_size_up(requested_width_ft, widths)
    price = value_by_width.get(next_width)
    if price is None:
        raise PriceBookError(f"No {label} price for width {requested_width_ft} ft (or next size up)")
    return (
        price,
        f"Per manufacturer rules, {label} was priced at the next width up: {next_width} ft.",
    )


def _lookup_by_height_width_next_size_up(
    *,
    value_by_height_width: Mapping[int, Mapping[int, int]],
    requested_height_ft: int,
    requested_width_ft: int,
    label: str,
) -> Tuple[int, Optional[str]]:
    if not value_by_height_width:
        raise PriceBookError(f"No pricing table available for {label}")
    heights = sorted(value_by_height_width.keys())
    height_ft = _next_size_up(requested_height_ft, heights)
    width_map = value_by_height_width.get(height_ft, {})
    if not width_map:
        raise PriceBookError(f"No {label} pricing available for height {requested_height_ft} ft")
    if requested_width_ft in width_map:
        return width_map[requested_width_ft], None
    widths = sorted(width_map.keys())
    next_width = _next_size_up(requested_width_ft, widths)
    price = width_map.get(next_width)
    if price is None:
        raise PriceBookError(
            f"No {label} price for {requested_width_ft} ft width at {height_ft} ft height"
        )
    note = None
    if height_ft != requested_height_ft or next_width != requested_width_ft:
        note = (
            f"Per manufacturer rules, {label} was priced at height {height_ft} ft "
            f"and width {next_width} ft."
        )
    return price, note


def _group_line_items(line_items: Sequence[LineItem]) -> Tuple[LineItem, ...]:
    aggregated: Dict[Tuple[str, str], Tuple[int, int, int]] = {}
    # key -> (first_idx, total_amount, count)
    for idx, li in enumerate(line_items):
        key = (li.code, li.description)
        prev = aggregated.get(key)
        if prev is None:
            aggregated[key] = (idx, li.amount_usd, 1)
        else:
            first_idx, amount, count = prev
            aggregated[key] = (first_idx, amount + li.amount_usd, count + 1)

    out: List[LineItem] = []
    for (code, desc), (first_idx, amount, count) in sorted(aggregated.items(), key=lambda kv: kv[1][0]):
        description = f"{desc} x{count}" if count > 1 else desc
        out.append(LineItem(code=code, description=description, amount_usd=amount))
    return tuple(out)


def _commercial_extrapolated_base_delta_usd(
    *,
    book: PriceBook,
    style: CarportStyle,
    roof_style: RoofStyle,
    gauge: int,
    requested_width_ft: int,
    requested_length_ft: int,
    max_width_ft: int,
    max_length_ft: int,
) -> int:
    """
    Best-effort extrapolation beyond the available base matrix.

    We compute a delta over the max available (max_width_ft x max_length_ft) using:
    - per-foot width increment from the last two widths at max length, if available
    - per-foot length increment from the last two lengths at max width, if available
    - fallback: per-sqft rate derived from the max cell
    """
    if requested_width_ft <= max_width_ft and requested_length_ft <= max_length_ft:
        return 0

    # Build a base matrix view for the specific style/roof/gauge.
    base_cells: Dict[Tuple[int, int], int] = {}
    for (s, r, g, w_ft, l_ft), price in book.base_prices_usd.items():
        if s == style and r == roof_style and g == gauge:
            base_cells[(int(w_ft), int(l_ft))] = int(price)

    base_max = base_cells.get((int(max_width_ft), int(max_length_ft)))
    if base_max is None:
        # If the caller passed a "max" that isn't a real cell, we can't extrapolate safely.
        return 0

    widths_at_max_len = sorted({w for (w, l) in base_cells.keys() if l == int(max_length_ft)})
    lengths_at_max_w = sorted({l for (w, l) in base_cells.keys() if w == int(max_width_ft)})

    inc_w = None
    if len(widths_at_max_len) >= 2:
        w_prev = widths_at_max_len[-2]
        base_prev = base_cells.get((int(w_prev), int(max_length_ft)))
        if base_prev is not None and (max_width_ft - w_prev) > 0:
            inc_w = (base_max - base_prev) / float(max_width_ft - w_prev)

    inc_l = None
    if len(lengths_at_max_w) >= 2:
        l_prev = lengths_at_max_w[-2]
        base_prev = base_cells.get((int(max_width_ft), int(l_prev)))
        if base_prev is not None and (max_length_ft - l_prev) > 0:
            inc_l = (base_max - base_prev) / float(max_length_ft - l_prev)

    extra = 0.0
    if requested_width_ft > max_width_ft and inc_w is not None:
        extra += (requested_width_ft - max_width_ft) * inc_w
    if requested_length_ft > max_length_ft and inc_l is not None:
        extra += (requested_length_ft - max_length_ft) * inc_l

    if extra <= 0.0:
        # Area-based fallback
        max_area = max_width_ft * max_length_ft
        req_area = requested_width_ft * requested_length_ft
        if max_area > 0 and req_area > max_area:
            per_sqft = base_max / float(max_area)
            extra = (req_area - max_area) * per_sqft

    return max(0, int(round(extra)))
