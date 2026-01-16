from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple


@dataclass(frozen=True)
class ExtractedRule:
    text: str
    page_hint: Optional[str]


@dataclass(frozen=True)
class ExtractedTable:
    title: str
    table_markdown: str
    page_hint: Optional[str]


@dataclass(frozen=True)
class ExtractedPricebook:
    source: str
    rules: Tuple[ExtractedRule, ...]
    notes: Tuple[ExtractedRule, ...]
    tables: Tuple[ExtractedTable, ...]
    path: Path


def find_extracted_pricebooks(out_dir: Path) -> List[Path]:
    return sorted(out_dir.glob("**/pricebook_extracted.json"))


def load_extracted_pricebook(path: Path) -> ExtractedPricebook:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Expected JSON object in {path}")

    source = data.get("source")
    if not isinstance(source, str) or not source.strip():
        raise ValueError(f"Missing/invalid 'source' in {path}")

    rules = _load_rules(data.get("rules"))
    notes = _load_rules(data.get("notes"))
    tables = _load_tables(data.get("tables"))
    return ExtractedPricebook(
        source=source.strip(),
        rules=tuple(rules),
        notes=tuple(notes),
        tables=tuple(tables),
        path=path,
    )


def _load_rules(value: object) -> List[ExtractedRule]:
    if not isinstance(value, list):
        return []
    out: List[ExtractedRule] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        text = item.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        page_hint = item.get("page_hint")
        page_hint_str = page_hint.strip() if isinstance(page_hint, str) and page_hint.strip() else None
        out.append(ExtractedRule(text=text.strip(), page_hint=page_hint_str))
    return out


def _load_tables(value: object) -> List[ExtractedTable]:
    if not isinstance(value, list):
        return []
    out: List[ExtractedTable] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        title = item.get("title")
        md = item.get("table_markdown")
        if not isinstance(title, str) or not title.strip():
            continue
        if not isinstance(md, str) or not md.strip():
            continue
        page_hint = item.get("page_hint")
        page_hint_str = page_hint.strip() if isinstance(page_hint, str) and page_hint.strip() else None
        out.append(ExtractedTable(title=title.strip(), table_markdown=md.strip(), page_hint=page_hint_str))
    return out


def markdown_table_to_rows(table_markdown: str) -> List[List[str]]:
    """
    Parse a markdown table string into rows of cell strings.

    This is a tolerant parser for OCR-generated markdown:
    - Skips separator rows like | --- | --- |
    - Trims whitespace
    - Keeps empty cells as empty strings
    """
    raw_lines = [ln.strip() for ln in table_markdown.splitlines() if ln.strip()]
    rows: List[List[str]] = []
    for ln in raw_lines:
        if not ln.startswith("|"):
            continue
        if _is_separator_row(ln):
            continue
        cells = [c.strip() for c in ln.strip("|").split("|")]
        rows.append(cells)
    return rows


_SEP_CELL_RE = re.compile(r"^:?-{3,}:?$")


def _is_separator_row(line: str) -> bool:
    cells = [c.strip() for c in line.strip("|").split("|")]
    if not cells:
        return False
    return all(bool(_SEP_CELL_RE.match(c)) for c in cells if c)


