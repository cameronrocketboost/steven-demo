from __future__ import annotations

import unittest
from pathlib import Path

from normalized_pricebooks import build_demo_pricebook_r29, load_normalized_pricebook
from pricing_engine import CarportStyle, PriceBookError, QuoteInput, RoofStyle, generate_quote


def _print_quote(label: str, inp: QuoteInput, quote) -> None:
    print("\n" + "=" * 72)
    print(f"{label}")
    print(
        f"INPUT  style={inp.style.value} roof={inp.roof_style.value} gauge={inp.gauge} "
        f"size={inp.width_ft}x{inp.length_ft} leg_height={inp.leg_height_ft} "
        f"ground_cert={inp.include_ground_certification}"
    )
    print(f"NORM   size={quote.normalized_width_ft}x{quote.normalized_length_ft}")
    print("ITEMS")
    for li in quote.line_items:
        print(f"  - {li.code}: ${li.amount_usd} | {li.description}")
    print(f"TOTAL  ${quote.total_usd}")
    if quote.notes:
        print("NOTES")
        for n in quote.notes:
            print(f"  - {n}")
    print("=" * 72)


class TestPricingEngine(unittest.TestCase):
    def test_regular_horizontal_base_lookup(self) -> None:
        book = _load_demo_book()
        inp = QuoteInput(
            style=CarportStyle.REGULAR,
            roof_style=RoofStyle.HORIZONTAL,
            gauge=14,
            width_ft=12,
            length_ft=21,
            leg_height_ft=6,
            include_ground_certification=False,
        )

        quote = generate_quote(inp, book)
        _print_quote("test_regular_horizontal_base_lookup", inp, quote)
        self.assertEqual(quote.normalized_width_ft, 12)
        self.assertEqual(quote.normalized_length_ft, 21)
        self.assertIn("R29 (NW)", quote.pricebook_revision)
        self.assertGreater(quote.total_usd, 0)

    def test_a_frame_vertical_base_lookup(self) -> None:
        book = _load_demo_book()
        inp = QuoteInput(
            style=CarportStyle.A_FRAME,
            roof_style=RoofStyle.VERTICAL,
            gauge=14,
            width_ft=12,
            length_ft=20,
            leg_height_ft=6,
            include_ground_certification=False,
        )
        quote = generate_quote(inp, book)
        _print_quote("test_a_frame_vertical_base_lookup", inp, quote)
        self.assertEqual(quote.normalized_length_ft, 20)
        self.assertGreater(quote.total_usd, 0)

    def test_vertical_roof_rejected_for_regular(self) -> None:
        book = _load_demo_book()
        inp = QuoteInput(
            style=CarportStyle.REGULAR,
            roof_style=RoofStyle.VERTICAL,
            gauge=14,
            width_ft=12,
            length_ft=20,
            leg_height_ft=6,
            include_ground_certification=False,
        )
        with self.assertRaises(PriceBookError):
            generate_quote(inp, book)

    def test_leg_height_addon_depends_on_length(self) -> None:
        book = _load_demo_book()
        short = QuoteInput(
            style=CarportStyle.A_FRAME,
            roof_style=RoofStyle.HORIZONTAL,
            gauge=14,
            width_ft=12,
            length_ft=21,
            leg_height_ft=10,
            include_ground_certification=False,
        )
        long = QuoteInput(
            style=CarportStyle.A_FRAME,
            roof_style=RoofStyle.HORIZONTAL,
            gauge=14,
            width_ft=12,
            length_ft=36,
            leg_height_ft=10,
            include_ground_certification=False,
        )
        q1 = generate_quote(short, book)
        q2 = generate_quote(long, book)
        _print_quote("test_leg_height_addon_depends_on_length (short)", short, q1)
        _print_quote("test_leg_height_addon_depends_on_length (long)", long, q2)
        self.assertNotEqual(q1.total_usd, q2.total_usd)

    def test_lift_note_at_13ft(self) -> None:
        book = _load_demo_book()
        inp = QuoteInput(
            style=CarportStyle.A_FRAME,
            roof_style=RoofStyle.HORIZONTAL,
            gauge=14,
            width_ft=12,
            length_ft=21,
            leg_height_ft=13,
            include_ground_certification=False,
        )
        quote = generate_quote(inp, book)
        _print_quote("test_lift_note_at_13ft", inp, quote)
        self.assertTrue(any("lift" in n.lower() for n in quote.notes))


def _load_demo_book():
    root = Path(__file__).resolve().parents[1]
    candidates = [
        root / "out" / "Coast_To_Coast_Carports___Price_Book___R29_1" / "normalized_pricebook.json",
        root / "pricebooks" / "out" / "Coast_To_Coast_Carports___Price_Book___R29_1" / "normalized_pricebook.json",
    ]
    normalized_path = next((p for p in candidates if p.exists()), candidates[0])
    normalized = load_normalized_pricebook(normalized_path)
    return build_demo_pricebook_r29(normalized)


if __name__ == "__main__":
    unittest.main()


