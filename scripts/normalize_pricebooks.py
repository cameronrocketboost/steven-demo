from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from extracted_pricebooks import find_extracted_pricebooks, load_extracted_pricebook
from pricebook_from_extracted import (
    parse_base_matrix_table,
    parse_option_list_table,
    parse_specifications_and_accessories_table,
    parse_vertical_sides_included_table,
)


def is_effectively_empty_ocr_text(text: str) -> bool:
    """
    Detect OCR output that is effectively empty (e.g. '.' repeated) so we don't
    generate misleading structured tables.
    """
    stripped = "".join(ch for ch in text if ch not in {" ", "\n", "\r", "\t"})
    if not stripped:
        return True
    # If all remaining chars are dots, it's useless.
    return all(ch == "." for ch in stripped)


def normalize_one(out_dir: Path, extracted_path: Path) -> Path:
    extracted = load_extracted_pricebook(extracted_path)
    pb_dir = extracted_path.parent
    ocr_text_path = pb_dir / "ocr_text.md"

    status: Dict[str, Any] = {"source": extracted.source, "path": str(extracted_path)}
    if ocr_text_path.exists():
        ocr_text = ocr_text_path.read_text(encoding="utf-8", errors="replace")
        if is_effectively_empty_ocr_text(ocr_text):
            normalized = {
                "source": extracted.source,
                "status": "invalid_ocr_text",
                "reason": "OCR text contained no usable content (dots/empty).",
                "rules": [r.text for r in extracted.rules],
                "notes": [n.text for n in extracted.notes],
                "base_matrices": [],
                "option_tables": [],
                "accessory_prices": {},
                "accessory_prices_by_length": {},
                "closed_end_prices_by_leg_height_width": {},
                "vertical_end_add_by_width": {},
            }
            out_path = pb_dir / "normalized_pricebook.json"
            out_path.write_text(json.dumps(normalized, indent=2), encoding="utf-8")
            return out_path

    base_matrices: List[Dict[str, Any]] = []
    option_tables: List[Dict[str, Any]] = []
    accessory_prices: Dict[str, int] = {}
    accessory_prices_by_length: Dict[str, Dict[int, int]] = {}
    closed_end_prices_by_leg_height_width: Dict[int, Dict[int, int]] = {}
    vertical_end_add_by_width: Dict[int, int] = {}

    for t in extracted.tables:
        title_norm = t.title.strip().lower()
        if title_norm == "specifications and accessories":
            try:
                parsed_accessories = parse_specifications_and_accessories_table(
                    title=t.title, table_markdown=t.table_markdown
                )
                accessory_prices.update(parsed_accessories.flat_options)
                for code, by_len in parsed_accessories.length_options.items():
                    accessory_prices_by_length.setdefault(code, {}).update(by_len)
            except Exception:
                pass
            continue

        if title_norm == "vertical sides included rv covers":
            try:
                parsed_closed = parse_vertical_sides_included_table(
                    title=t.title, table_markdown=t.table_markdown
                )
                closed_end_prices_by_leg_height_width.update(parsed_closed.closed_end_by_leg_height_width)
                vertical_end_add_by_width.update(parsed_closed.vertical_end_add_by_width)
            except Exception:
                pass
            continue

        # Try parse as base matrix
        try:
            parsed_base = parse_base_matrix_table(title=t.title, table_markdown=t.table_markdown)
            base_matrices.append(
                {
                    "title": parsed_base.title,
                    "gauge": parsed_base.gauge,
                    "widths_ft": list(parsed_base.widths_ft),
                    "lengths_ft": list(parsed_base.lengths_ft),
                    "entries": [
                        {"width_ft": w, "length_ft": l, "price_usd": p} for (w, l, p) in parsed_base.entries
                    ],
                }
            )
            continue
        except Exception:
            pass

        # Try parse as option table
        try:
            parsed_opt = parse_option_list_table(title=t.title, table_markdown=t.table_markdown)
            option_tables.append(
                {
                    "title": parsed_opt.title,
                    "lengths_ft": list(parsed_opt.lengths_ft),
                    "option_prices_by_code": parsed_opt.option_prices_by_code,
                    "leg_height_addons": parsed_opt.leg_height_addons,
                }
            )
            continue
        except Exception:
            pass

    normalized = {
        "source": extracted.source,
        "status": "ok",
        "rules": [r.text for r in extracted.rules],
        "notes": [n.text for n in extracted.notes],
        "base_matrices": base_matrices,
        "option_tables": option_tables,
        "accessory_prices": accessory_prices,
        "accessory_prices_by_length": accessory_prices_by_length,
        "closed_end_prices_by_leg_height_width": closed_end_prices_by_leg_height_width,
        "vertical_end_add_by_width": vertical_end_add_by_width,
    }

    out_path = extracted_path.parent / "normalized_pricebook.json"
    out_path.write_text(json.dumps(normalized, indent=2), encoding="utf-8")
    return out_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Normalize extracted pricebooks into a clean JSON schema.")
    parser.add_argument("--out-dir", required=True, help="Path to the extractor output directory (contains */pricebook_extracted.json).")
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    paths = find_extracted_pricebooks(out_dir)
    if not paths:
        raise SystemExit(f"No extracted pricebooks found under: {out_dir}")

    written: List[Path] = []
    for p in paths:
        written.append(normalize_one(out_dir, p))

    print(f"Normalized {len(written)} pricebooks:")
    for w in written:
        print(f"- {w}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
