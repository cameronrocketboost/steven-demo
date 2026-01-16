from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Tuple

from pricing_engine import CarportStyle, PriceBook, RoofStyle


@dataclass(frozen=True)
class NormalizedBaseMatrix:
    title: str
    gauge: int
    widths_ft: Tuple[int, ...]
    lengths_ft: Tuple[int, ...]
    entries: Tuple[Tuple[int, int, int], ...]  # (width_ft, length_ft, price_usd)


@dataclass(frozen=True)
class NormalizedOptionTable:
    title: str
    lengths_ft: Tuple[int, ...]
    option_prices_by_code: Mapping[str, Mapping[int, int]]
    leg_height_addons: Mapping[int, Mapping[int, int]]


@dataclass(frozen=True)
class NormalizedPricebook:
    source: str
    status: str
    reason: Optional[str]
    rules: Tuple[str, ...]
    notes: Tuple[str, ...]
    base_matrices: Tuple[NormalizedBaseMatrix, ...]
    option_tables: Tuple[NormalizedOptionTable, ...]
    accessory_prices: Mapping[str, int]
    accessory_prices_by_length: Mapping[str, Mapping[int, int]]
    closed_end_prices_by_leg_height_width: Mapping[int, Mapping[int, int]]
    vertical_end_add_by_width: Mapping[int, int]
    path: Path


def find_normalized_pricebooks(out_dir: Path) -> List[Path]:
    return sorted(out_dir.glob("**/normalized_pricebook.json"))


def load_normalized_pricebook(path: Path) -> NormalizedPricebook:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Expected JSON object in {path}")

    source = data.get("source")
    if not isinstance(source, str) or not source.strip():
        raise ValueError(f"Missing/invalid 'source' in {path}")

    status = data.get("status")
    if not isinstance(status, str) or not status.strip():
        raise ValueError(f"Missing/invalid 'status' in {path}")

    reason_obj = data.get("reason")
    reason = reason_obj.strip() if isinstance(reason_obj, str) and reason_obj.strip() else None

    rules = tuple(r for r in data.get("rules", []) if isinstance(r, str) and r.strip())
    notes = tuple(n for n in data.get("notes", []) if isinstance(n, str) and n.strip())

    base_matrices: List[NormalizedBaseMatrix] = []
    bm_raw = data.get("base_matrices", [])
    if isinstance(bm_raw, list):
        for bm in bm_raw:
            if not isinstance(bm, dict):
                continue
            title = bm.get("title")
            gauge = bm.get("gauge")
            widths = bm.get("widths_ft")
            lengths = bm.get("lengths_ft")
            entries = bm.get("entries")
            if not isinstance(title, str) or not title.strip():
                continue
            if not isinstance(gauge, int):
                continue
            widths_ft = tuple(int(x) for x in widths) if isinstance(widths, list) else tuple()
            lengths_ft = tuple(int(x) for x in lengths) if isinstance(lengths, list) else tuple()
            entries_out: List[Tuple[int, int, int]] = []
            if isinstance(entries, list):
                for e in entries:
                    if not isinstance(e, dict):
                        continue
                    w = e.get("width_ft")
                    l = e.get("length_ft")
                    p = e.get("price_usd")
                    if isinstance(w, int) and isinstance(l, int) and isinstance(p, int):
                        entries_out.append((w, l, p))
            base_matrices.append(
                NormalizedBaseMatrix(
                    title=title.strip(),
                    gauge=gauge,
                    widths_ft=tuple(widths_ft),
                    lengths_ft=tuple(lengths_ft),
                    entries=tuple(entries_out),
                )
            )

    option_tables: List[NormalizedOptionTable] = []
    ot_raw = data.get("option_tables", [])
    if isinstance(ot_raw, list):
        for ot in ot_raw:
            if not isinstance(ot, dict):
                continue
            title = ot.get("title")
            lengths = ot.get("lengths_ft")
            option_prices_by_code = ot.get("option_prices_by_code")
            leg_height_addons = ot.get("leg_height_addons")
            if not isinstance(title, str) or not title.strip():
                continue
            lengths_ft = tuple(int(x) for x in lengths) if isinstance(lengths, list) else tuple()
            option_prices: Dict[str, Dict[int, int]] = {}
            if isinstance(option_prices_by_code, dict):
                for code, v in option_prices_by_code.items():
                    if not isinstance(code, str) or not code.strip() or not isinstance(v, dict):
                        continue
                    d2: Dict[int, int] = {}
                    for k, val in v.items():
                        try:
                            k_int = int(k)
                        except Exception:
                            continue
                        if isinstance(val, int):
                            d2[k_int] = val
                    if d2:
                        option_prices[code.strip()] = d2
            leg_addons: Dict[int, Dict[int, int]] = {}
            if isinstance(leg_height_addons, dict):
                for height_key, v in leg_height_addons.items():
                    try:
                        height_int = int(height_key)
                    except Exception:
                        continue
                    if not isinstance(v, dict):
                        continue
                    d3: Dict[int, int] = {}
                    for k, val in v.items():
                        try:
                            k_int = int(k)
                        except Exception:
                            continue
                        if isinstance(val, int):
                            d3[k_int] = val
                    if d3:
                        leg_addons[height_int] = d3
            option_tables.append(
                NormalizedOptionTable(
                    title=title.strip(),
                    lengths_ft=tuple(lengths_ft),
                    option_prices_by_code=option_prices,
                    leg_height_addons=leg_addons,
                )
            )

    accessory_prices: Dict[str, int] = {}
    raw_accessory_prices = data.get("accessory_prices", {})
    if isinstance(raw_accessory_prices, dict):
        for code, val in raw_accessory_prices.items():
            if isinstance(code, str) and code.strip() and isinstance(val, int):
                accessory_prices[code.strip()] = val

    accessory_prices_by_length: Dict[str, Dict[int, int]] = {}
    raw_accessory_by_length = data.get("accessory_prices_by_length", {})
    if isinstance(raw_accessory_by_length, dict):
        for code, v in raw_accessory_by_length.items():
            if not isinstance(code, str) or not code.strip() or not isinstance(v, dict):
                continue
            d: Dict[int, int] = {}
            for k, val in v.items():
                try:
                    k_int = int(k)
                except Exception:
                    continue
                if isinstance(val, int):
                    d[k_int] = val
            if d:
                accessory_prices_by_length[code.strip()] = d

    closed_end_prices_by_leg_height_width: Dict[int, Dict[int, int]] = {}
    raw_closed = data.get("closed_end_prices_by_leg_height_width", {})
    if isinstance(raw_closed, dict):
        for height_key, v in raw_closed.items():
            try:
                height_int = int(height_key)
            except Exception:
                continue
            if not isinstance(v, dict):
                continue
            d: Dict[int, int] = {}
            for k, val in v.items():
                try:
                    k_int = int(k)
                except Exception:
                    continue
                if isinstance(val, int):
                    d[k_int] = val
            if d:
                closed_end_prices_by_leg_height_width[height_int] = d

    vertical_end_add_by_width: Dict[int, int] = {}
    raw_vertical_end = data.get("vertical_end_add_by_width", {})
    if isinstance(raw_vertical_end, dict):
        for k, val in raw_vertical_end.items():
            try:
                k_int = int(k)
            except Exception:
                continue
            if isinstance(val, int):
                vertical_end_add_by_width[k_int] = val

    return NormalizedPricebook(
        source=source.strip(),
        status=status.strip(),
        reason=reason,
        rules=rules,
        notes=notes,
        base_matrices=tuple(base_matrices),
        option_tables=tuple(option_tables),
        accessory_prices=accessory_prices,
        accessory_prices_by_length=accessory_prices_by_length,
        closed_end_prices_by_leg_height_width=closed_end_prices_by_leg_height_width,
        vertical_end_add_by_width=vertical_end_add_by_width,
        path=path,
    )


def build_pricebook_from_normalized(
    normalized: NormalizedPricebook,
    *,
    base_matrix_title: str,
    option_table_title: str,
    assume_style: CarportStyle = CarportStyle.A_FRAME,
    assume_roof: RoofStyle = RoofStyle.VERTICAL,
) -> PriceBook:
    base = _find_base_matrix(normalized, base_matrix_title)
    opt = _find_option_table(normalized, option_table_title)

    base_prices_keyed: Dict[Tuple[CarportStyle, RoofStyle, int, int, int], int] = {}
    for (w, l, p) in base.entries:
        base_prices_keyed[(assume_style, assume_roof, base.gauge, w, l)] = p

    # Use base matrix lengths for allowed lengths (this matches the UI behaviour in the screenshot)
    allowed_lengths = tuple(sorted(set(base.lengths_ft)))
    allowed_widths = tuple(sorted(set(base.widths_ft)))
    allowed_leg_heights = tuple(sorted(opt.leg_height_addons.keys()))

    option_prices: Dict[str, Dict[int, int]] = {k: dict(v) for k, v in opt.option_prices_by_code.items()}
    for code, by_len in normalized.accessory_prices_by_length.items():
        if code in option_prices:
            continue
        option_prices[code] = dict(by_len)
    for code, price in normalized.accessory_prices.items():
        if code in option_prices:
            continue
        option_prices[code] = {length: price for length in allowed_lengths}

    return PriceBook(
        revision=f"{normalized.source} | base={base.title} | options={opt.title}",
        allowed_widths_ft=allowed_widths,
        allowed_lengths_ft=allowed_lengths,
        allowed_leg_heights_ft=allowed_leg_heights,
        base_prices_usd=base_prices_keyed,
        option_prices_by_length_usd=option_prices,
        leg_height_addon_by_length_usd=opt.leg_height_addons,
        closed_end_prices_by_leg_height_width_usd=normalized.closed_end_prices_by_leg_height_width,
        vertical_end_add_by_width_usd=normalized.vertical_end_add_by_width,
    )


def _find_base_matrix(normalized: NormalizedPricebook, title: str) -> NormalizedBaseMatrix:
    wanted = title.strip().lower()
    for bm in normalized.base_matrices:
        if bm.title.strip().lower() == wanted:
            return bm
    raise ValueError(f"Base matrix not found: {title!r}")


def _find_option_table(normalized: NormalizedPricebook, title: str) -> NormalizedOptionTable:
    wanted = title.strip().lower()
    for ot in normalized.option_tables:
        if ot.title.strip().lower() == wanted:
            return ot
    raise ValueError(f"Option table not found: {title!r}")


def build_demo_pricebook_r29(normalized: NormalizedPricebook) -> PriceBook:
    """
    Build a demo-focused PriceBook from the R29 (NW) normalized JSON.

    This intentionally supports only the "standard" subset needed for the demo:
    - Regular (horizontal), A-Frame (horizontal), A-Frame (vertical roof)
    - Standard widths: 12/18/20/22/24
    - Horizontal lengths: 21/26/31/36
    - Vertical lengths: 20/25/30/35 (manufacturer note: vertical buildings are 1' shorter)
    - Leg height pricing from OPTION LIST (bounded to 6-13 for demo)
    - Accessories from Specifications and Accessories, where available
    """
    if normalized.status != "ok":
        raise ValueError(f"Normalized pricebook is not usable (status={normalized.status!r})")

    demo_widths = (12, 18, 20, 22, 24)
    horiz_lengths = (21, 26, 31, 36)
    vert_lengths = (20, 25, 30, 35)
    allowed_lengths = tuple(sorted(set(horiz_lengths + vert_lengths)))

    regular = _find_base_matrix_for_demo(
        normalized, title="REGULAR STYLE", required_widths=demo_widths, required_lengths=horiz_lengths
    )
    a_frame_h = _find_base_matrix_for_demo(
        normalized, title="A-FRAME STYLE", required_widths=demo_widths, required_lengths=horiz_lengths
    )
    a_frame_v = _find_base_matrix_for_demo(
        normalized, title="VERTICAL ROOF STYLE", required_widths=demo_widths, required_lengths=vert_lengths
    )
    opt = _find_option_table(normalized, "OPTION LIST")

    base_prices_keyed: Dict[Tuple[CarportStyle, RoofStyle, int, int, int], int] = {}
    _add_base_entries(
        base_prices_keyed,
        matrix=regular,
        style=CarportStyle.REGULAR,
        roof=RoofStyle.HORIZONTAL,
        allowed_widths=demo_widths,
        allowed_lengths=horiz_lengths,
    )
    _add_base_entries(
        base_prices_keyed,
        matrix=a_frame_h,
        style=CarportStyle.A_FRAME,
        roof=RoofStyle.HORIZONTAL,
        allowed_widths=demo_widths,
        allowed_lengths=horiz_lengths,
    )
    _add_base_entries(
        base_prices_keyed,
        matrix=a_frame_v,
        style=CarportStyle.A_FRAME,
        roof=RoofStyle.VERTICAL,
        allowed_widths=demo_widths,
        allowed_lengths=vert_lengths,
    )

    # Demo-bounded leg heights: 6-13 (still enough to show the "lift" rule at 13)
    allowed_leg_heights = tuple(h for h in sorted(opt.leg_height_addons.keys()) if 6 <= h <= 13)
    leg_height_addons = {h: dict(v) for (h, v) in opt.leg_height_addons.items() if h in allowed_leg_heights}

    option_prices: Dict[str, Dict[int, int]] = {k: dict(v) for k, v in opt.option_prices_by_code.items()}
    for code, by_len in normalized.accessory_prices_by_length.items():
        option_prices.setdefault(code, {}).update(dict(by_len))
    for code, price in normalized.accessory_prices.items():
        if code in option_prices:
            continue
        option_prices[code] = {length: price for length in allowed_lengths}

    return PriceBook(
        revision=(
            f"R29 (NW) | {normalized.source} | "
            f"base=[{regular.title} + {a_frame_h.title} + {a_frame_v.title}] | options={opt.title}"
        ),
        allowed_widths_ft=demo_widths,
        allowed_lengths_ft=allowed_lengths,
        allowed_leg_heights_ft=allowed_leg_heights,
        base_prices_usd=base_prices_keyed,
        option_prices_by_length_usd=option_prices,
        leg_height_addon_by_length_usd=leg_height_addons,
        closed_end_prices_by_leg_height_width_usd={},  # Phase 2
        vertical_end_add_by_width_usd={},  # Phase 2
    )


def _find_base_matrix_for_demo(
    normalized: NormalizedPricebook,
    *,
    title: str,
    required_widths: Tuple[int, ...],
    required_lengths: Tuple[int, ...],
) -> NormalizedBaseMatrix:
    wanted = title.strip().lower()
    req_w = set(required_widths)
    req_l = set(required_lengths)
    for bm in normalized.base_matrices:
        if bm.title.strip().lower() != wanted:
            continue
        bm_w = set(bm.widths_ft)
        bm_l = set(bm.lengths_ft)
        if not req_w.issubset(bm_w):
            continue
        if not req_l.issubset(bm_l):
            continue
        return bm
    raise ValueError(
        f"Demo base matrix not found: {title!r} (needed widths={sorted(req_w)}, lengths={sorted(req_l)})"
    )


def _add_base_entries(
    out: Dict[Tuple[CarportStyle, RoofStyle, int, int, int], int],
    *,
    matrix: NormalizedBaseMatrix,
    style: CarportStyle,
    roof: RoofStyle,
    allowed_widths: Tuple[int, ...],
    allowed_lengths: Tuple[int, ...],
) -> None:
    allow_w = set(allowed_widths)
    allow_l = set(allowed_lengths)
    for (w, l, p) in matrix.entries:
        if w not in allow_w or l not in allow_l:
            continue
        out[(style, roof, matrix.gauge, w, l)] = p
