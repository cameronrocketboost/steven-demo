from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from extracted_pricebooks import ExtractedPricebook, ExtractedTable, markdown_table_to_rows
from pricing_engine import CarportStyle, PriceBook, RoofStyle


_MONEY_RE = re.compile(r"(-?\d[\d,]*(?:\.\d+)?)")
_DIM_RE = re.compile(r"^\*?\s*(\d+)\s*[xX]\s*(\d+)\s*$")


def build_pricebook_from_extracted(
    extracted: ExtractedPricebook,
    *,
    base_table_title: str,
    option_table_title: str,
    assume_style: CarportStyle = CarportStyle.A_FRAME,
    assume_roof: RoofStyle = RoofStyle.VERTICAL,
) -> PriceBook:
    base_table = _find_table_by_title(extracted, base_table_title)
    option_table = _find_table_by_title(extracted, option_table_title)

    base_prices, base_lengths, base_widths, gauge = _parse_base_matrix_table(base_table.table_markdown)
    option_prices_by_length, leg_height_addon_by_length = _parse_option_list_table(option_table.table_markdown)

    base_prices_keyed: Dict[Tuple[CarportStyle, RoofStyle, int, int, int], int] = {}
    for (w, l), price in base_prices.items():
        base_prices_keyed[(assume_style, assume_roof, gauge, w, l)] = price

    # Allowed leg heights comes from parsed leg height table
    allowed_leg_heights = tuple(sorted(leg_height_addon_by_length.keys()))

    return PriceBook(
        revision=f"{extracted.source} | base={base_table.title} | options={option_table.title}",
        allowed_widths_ft=tuple(sorted(base_widths)),
        allowed_lengths_ft=tuple(sorted(base_lengths)),
        allowed_leg_heights_ft=allowed_leg_heights,
        base_prices_usd=base_prices_keyed,
        option_prices_by_length_usd=option_prices_by_length,
        leg_height_addon_by_length_usd=leg_height_addon_by_length,
    )


@dataclass(frozen=True)
class ParsedBaseMatrix:
    title: str
    gauge: int
    entries: Tuple[Tuple[int, int, int], ...]  # (width_ft, length_ft, price_usd)
    widths_ft: Tuple[int, ...]
    lengths_ft: Tuple[int, ...]


@dataclass(frozen=True)
class ParsedOptionTable:
    title: str
    lengths_ft: Tuple[int, ...]
    option_prices_by_code: Dict[str, Dict[int, int]]
    leg_height_addons: Dict[int, Dict[int, int]]


@dataclass(frozen=True)
class ParsedAccessoryOptions:
    title: str
    flat_options: Dict[str, int]
    length_options: Dict[str, Dict[int, int]]


@dataclass(frozen=True)
class ParsedClosedEndOptions:
    title: str
    closed_end_by_leg_height_width: Dict[int, Dict[int, int]]
    vertical_end_add_by_width: Dict[int, int]


def parse_base_matrix_table(*, title: str, table_markdown: str) -> ParsedBaseMatrix:
    base_prices, base_lengths, base_widths, gauge = _parse_base_matrix_table(table_markdown)
    entries = tuple((w, l, p) for (w, l), p in sorted(base_prices.items()))
    return ParsedBaseMatrix(
        title=title,
        gauge=gauge,
        entries=entries,
        widths_ft=tuple(base_widths),
        lengths_ft=tuple(base_lengths),
    )


def parse_option_list_table(*, title: str, table_markdown: str) -> ParsedOptionTable:
    option_prices_by_length, leg_height_addon_by_length = _parse_option_list_table(table_markdown)
    # Normalize lengths as union of whatever we saw (mostly header lengths).
    lengths: List[int] = []
    for v in option_prices_by_length.values():
        lengths.extend(v.keys())
    for v in leg_height_addon_by_length.values():
        lengths.extend(v.keys())
    return ParsedOptionTable(
        title=title,
        lengths_ft=tuple(sorted(set(lengths))),
        option_prices_by_code=dict(option_prices_by_length),
        leg_height_addons=dict(leg_height_addon_by_length),
    )


def parse_specifications_and_accessories_table(*, title: str, table_markdown: str) -> ParsedAccessoryOptions:
    flat_options, length_options = _parse_specifications_and_accessories_table(table_markdown)
    return ParsedAccessoryOptions(
        title=title,
        flat_options=dict(flat_options),
        length_options={k: dict(v) for k, v in length_options.items()},
    )


def parse_vertical_sides_included_table(*, title: str, table_markdown: str) -> ParsedClosedEndOptions:
    closed_end_by_leg_height_width, vertical_end_add_by_width = _parse_vertical_sides_included_table(
        table_markdown
    )
    return ParsedClosedEndOptions(
        title=title,
        closed_end_by_leg_height_width={k: dict(v) for k, v in closed_end_by_leg_height_width.items()},
        vertical_end_add_by_width=dict(vertical_end_add_by_width),
    )


def _find_table_by_title(extracted: ExtractedPricebook, title: str) -> ExtractedTable:
    wanted = title.strip().lower()
    for t in extracted.tables:
        if t.title.strip().lower() == wanted:
            return t
    raise ValueError(f"Table not found: {title!r}")


def _parse_money_to_int(cell: str) -> Optional[int]:
    # Handle weird OCR currency (€, etc). We only care about the numeric dollars for demo purposes.
    m = _MONEY_RE.search(cell)
    if not m:
        return None
    try:
        return int(round(float(m.group(1).replace(",", ""))))
    except ValueError:
        return None


def _parse_dim(cell: str) -> Optional[Tuple[int, int]]:
    m = _DIM_RE.match(cell.strip())
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)))


_DIM_IN_TEXT_RE = re.compile(r"(\d+)\s*\"?\s*[xX]\s*(\d+)\s*\"?")


def _parse_dim_in_text(cell: str) -> Optional[Tuple[int, int]]:
    m = _DIM_IN_TEXT_RE.search(cell)
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)))


def _parse_gauge_from_cells(cells: Sequence[str]) -> Optional[int]:
    for c in cells:
        c2 = c.strip().upper()
        if "GAUGE" in c2:
            nums = re.findall(r"\d+", c2)
            if nums:
                try:
                    return int(nums[0])
                except ValueError:
                    return None
    return None


def _parse_base_matrix_table(table_markdown: str) -> Tuple[Dict[Tuple[int, int], int], List[int], List[int], int]:
    """
    Parse base price tables that are encoded as pairs of (W x L, $price) repeated across a row.

    Example row structure from extraction:
    | 14 GAUGE | 12 x 20 | $2,895.00 | *18 x 20 | $3,395.00 | ...
    """
    rows = markdown_table_to_rows(table_markdown)
    if not rows:
        raise ValueError("Empty base matrix table")

    gauge = _parse_gauge_from_cells(rows[0]) or 14

    out: Dict[Tuple[int, int], int] = {}
    widths: List[int] = []
    lengths: List[int] = []

    for r in rows:
        # Walk through cells, collecting dim/price pairs.
        i = 0
        while i < len(r) - 1:
            dim = _parse_dim(r[i])
            if dim is None:
                i += 1
                continue
            price = _parse_money_to_int(r[i + 1])
            if price is None:
                i += 1
                continue
            out[dim] = price
            widths.append(dim[0])
            lengths.append(dim[1])
            i += 2

    if not out:
        raise ValueError("No (W x L, price) pairs found in base matrix table")

    return out, sorted(set(lengths)), sorted(set(widths)), gauge


def _parse_length_header_row(cells: Sequence[str]) -> List[int]:
    lengths: List[int] = []
    for c in cells:
        c2 = c.strip()
        # Common: "21' Long", "30' LONG"
        m = re.search(r"(\d+)\s*'?\s*(?:Long|LONG)", c2)
        if m:
            lengths.append(int(m.group(1)))
    return lengths


def _parse_option_list_table(
    table_markdown: str,
) -> Tuple[Dict[str, Dict[int, int]], Dict[int, Dict[int, int]]]:
    """
    Parse the 'OPTION LIST' style table into:
    - option_prices_by_length: option_code -> {length_ft: price}
    - leg_height_addon_by_length: leg_height_ft -> {length_ft: add_on_price} (STD treated as 0)
    """
    rows = markdown_table_to_rows(table_markdown)
    if not rows:
        raise ValueError("Empty option list table")

    # Find the first header row with lengths.
    header_lengths: List[int] = []
    header_row_idx: Optional[int] = None
    for idx, r in enumerate(rows):
        lengths = _parse_length_header_row(r)
        if len(lengths) >= 3:
            header_lengths = lengths
            header_row_idx = idx
            break
    if not header_lengths or header_row_idx is None:
        raise ValueError("Could not locate a length header row in option list table")

    option_prices: Dict[str, Dict[int, int]] = {}
    leg_height_addons: Dict[int, Dict[int, int]] = {}

    # Locate the leg-height add-on block (the one that contains STD).
    leg_height_header_idx: Optional[int] = None
    for idx, r in enumerate(rows[header_row_idx + 1 :], start=header_row_idx + 1):
        row_text = " ".join(c.strip() for c in r if c.strip()).upper()
        if "LEG HEIGHT" not in row_text:
            continue
        lookahead = rows[idx + 1 : idx + 6]
        if any(any(cell.strip().upper() == "STD" for cell in rr) for rr in lookahead):
            leg_height_header_idx = idx
            break

    if leg_height_header_idx is None:
        raise ValueError("Could not locate the leg height add-on section in option list table")

    # Parse normal option price rows that appear before the leg-height section.
    for r in rows[header_row_idx + 1 : leg_height_header_idx]:
        label = r[0].strip() if r else ""
        if not label:
            continue
        option_code = _normalize_option_code(label)
        values = r[-len(header_lengths) :] if len(r) >= len(header_lengths) else []
        if len(values) != len(header_lengths):
            continue
        by_len2: Dict[int, int] = {}
        for length_ft, cell in zip(header_lengths, values):
            price = _parse_money_to_int(cell)
            if price is None:
                continue
            by_len2[length_ft] = price
        if by_len2:
            option_prices[option_code] = by_len2

    # Parse leg height add-ons until we leave the block.
    for r in rows[leg_height_header_idx + 1 :]:
        first_cell = r[0].strip().upper() if r else ""
        if first_cell not in {"", "HEIGHT"}:
            if leg_height_addons:
                break
            continue

        height_cell = r[1].strip() if len(r) > 1 else ""
        m = re.search(r"(\d+)\s*F[Tt]", height_cell)
        if not m:
            if leg_height_addons:
                break
            continue

        height_ft = int(m.group(1))
        values = r[-len(header_lengths) :] if len(r) >= len(header_lengths) else []
        if len(values) != len(header_lengths):
            continue
        by_len: Dict[int, int] = {}
        for length_ft, cell in zip(header_lengths, values):
            if cell.strip().upper() == "STD":
                by_len[length_ft] = 0
                continue
            price = _parse_money_to_int(cell)
            if price is None:
                continue
            by_len[length_ft] = price
        if by_len:
            leg_height_addons[height_ft] = by_len

    if not leg_height_addons:
        # Some option tables include leg heights elsewhere; for our demo we require them.
        raise ValueError("Could not parse leg height add-ons from option list table")

    return option_prices, leg_height_addons


def _normalize_option_code(label: str) -> str:
    s = label.strip().upper()
    s = re.sub(r"[^A-Z0-9]+", "_", s).strip("_")
    return s or "OPTION"


def _parse_specifications_and_accessories_table(
    table_markdown: str,
) -> Tuple[Dict[str, int], Dict[str, Dict[int, int]]]:
    rows = markdown_table_to_rows(table_markdown)
    if not rows:
        raise ValueError("Empty specifications/accessories table")

    flat_options: Dict[str, int] = {}
    length_options: Dict[str, Dict[int, int]] = {}

    def _parse_accessory_price(cell: str) -> Optional[int]:
        prices = re.findall(r"\$\s*([\d,]+)", cell)
        if prices:
            try:
                return int(prices[-1].replace(",", ""))
            except ValueError:
                return None
        return _parse_money_to_int(cell)

    for r in rows:
        if not r:
            continue

        # Extra panels by length.
        if len(r) >= 2:
            m_len = re.search(r"(\d+)\s*'?\s*Long", r[0], re.IGNORECASE)
            if m_len:
                length_ft = int(m_len.group(1))
                price = _parse_money_to_int(r[1])
                if price is not None:
                    length_options.setdefault("EXTRA_PANEL", {})[length_ft] = price

        # Frame out options with per-opening pricing.
        if len(r) >= 4:
            label = r[2].strip()
            price = _parse_accessory_price(r[3]) if len(r) > 3 else None
            if label and price is not None:
                label_upper = label.upper()
                if "WINDOW FRAME OUT" in label_upper:
                    flat_options["WINDOW_FRAME_OUT"] = price
                elif "WALK-IN DOOR FRAME OUT" in label_upper or "WALK IN DOOR FRAME OUT" in label_upper:
                    flat_options["WALK_IN_DOOR_FRAME_OUT"] = price
                elif "GARAGE DOOR FRAME OUT" in label_upper:
                    flat_options["GARAGE_DOOR_FRAME_OUT"] = price

        # Roll-up garage door sizes.
        if len(r) >= 2:
            dim = _parse_dim(r[0])
            price = _parse_accessory_price(r[1])
            if dim and price is not None:
                code = f"ROLL_UP_DOOR_{dim[0]}X{dim[1]}"
                flat_options[code] = price

        # Windows and door sizes embedded in text.
        for cell in r:
            cell_upper = cell.upper()
            if "$" not in cell:
                continue
            price = _parse_accessory_price(cell)
            if price is None:
                continue

            if "WINDOW" in cell_upper and "PANEL" not in cell_upper and "DOOR" not in cell_upper:
                dim = _parse_dim_in_text(cell)
                if dim:
                    flat_options[f"WINDOW_{dim[0]}X{dim[1]}"] = price
                continue

            if "PANEL" in cell_upper or "STANDARD" in cell_upper or "NINE LITE" in cell_upper:
                if '36"' in cell and '80"' in cell:
                    if "SIX PANEL W/ WINDOW" in cell_upper:
                        flat_options["WALK_IN_DOOR_SIX_PANEL_WINDOW_36X80"] = price
                    elif "SIX PANEL" in cell_upper:
                        flat_options["WALK_IN_DOOR_SIX_PANEL_36X80"] = price
                    elif "NINE LITE" in cell_upper:
                        flat_options["WALK_IN_DOOR_NINE_LITE_36X80"] = price
                    elif "STANDARD" in cell_upper:
                        flat_options["WALK_IN_DOOR_STANDARD_36X80"] = price

            if "X" in cell_upper and "EACH" in cell_upper and "WINDOW" not in cell_upper:
                dim = _parse_dim_in_text(cell)
                if dim:
                    flat_options.setdefault(f"WINDOW_{dim[0]}X{dim[1]}", price)

    if not flat_options and not length_options:
        raise ValueError("No accessory options parsed from table")

    return flat_options, length_options


def _parse_vertical_sides_included_table(
    table_markdown: str,
) -> Tuple[Dict[int, Dict[int, int]], Dict[int, int]]:
    rows = markdown_table_to_rows(table_markdown)
    if not rows:
        raise ValueError("Empty vertical sides table")

    header = rows[0]
    widths: List[int] = []
    for cell in header:
        m = re.search(r"(\d+)", cell)
        if m:
            widths.append(int(m.group(1)))
    if len(widths) < 2:
        raise ValueError("Could not parse width headers from vertical sides table")

    closed_end_by_height: Dict[int, Dict[int, int]] = {}
    vertical_end_add_by_width: Dict[int, int] = {}

    for r in rows[1:]:
        if not r:
            continue
        label = r[0].strip().upper()
        if "CLOSED END" in label:
            height_cell = r[1] if len(r) > 1 else ""
            m_height = re.search(r"(\d+)", height_cell)
            if not m_height:
                continue
            height_ft = int(m_height.group(1))
            values = r[-len(widths) :] if len(r) >= len(widths) else []
            if len(values) != len(widths):
                continue
            by_width: Dict[int, int] = {}
            for width_ft, cell in zip(widths, values):
                price = _parse_money_to_int(cell)
                if price is not None:
                    by_width[width_ft] = price
            if by_width:
                closed_end_by_height[height_ft] = by_width
            continue

        if label == "" and len(r) > 1 and "FT" in r[1].upper():
            m_height = re.search(r"(\d+)", r[1])
            if not m_height:
                continue
            height_ft = int(m_height.group(1))
            values = r[-len(widths) :] if len(r) >= len(widths) else []
            if len(values) != len(widths):
                continue
            by_width = {}
            for width_ft, cell in zip(widths, values):
                price = _parse_money_to_int(cell)
                if price is not None:
                    by_width[width_ft] = price
            if by_width:
                closed_end_by_height[height_ft] = by_width
            continue

        if "VERTICAL ENDS OPTION" in label:
            values = r[-len(widths) :] if len(r) >= len(widths) else []
            if len(values) != len(widths):
                continue
            for width_ft, cell in zip(widths, values):
                price = _parse_money_to_int(cell)
                if price is not None:
                    vertical_end_add_by_width[width_ft] = price

    if not closed_end_by_height:
        raise ValueError("No closed end prices parsed from vertical sides table")

    return closed_end_by_height, vertical_end_add_by_width
