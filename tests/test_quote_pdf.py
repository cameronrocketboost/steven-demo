from __future__ import annotations

import unittest
from datetime import date

from quote_pdf import QuotePdfArtifact, QuotePdfLineItem, QuotePdfTotals, make_quote_pdf_bytes


class TestQuotePdf(unittest.TestCase):
    def _count_pdf_pages(self, pdf: bytes) -> int:
        """
        Best-effort page count without extra dependencies.

        In ReportLab output, each page object typically includes "/Type /Page",
        while the page tree includes "/Type /Pages". We count Page occurrences
        and subtract the Pages tree occurrence.
        """
        if not isinstance(pdf, (bytes, bytearray)):
            raise TypeError("pdf must be bytes")
        page = pdf.count(b"/Type /Page")
        pages_tree = pdf.count(b"/Type /Pages")
        return max(0, page - pages_tree)

    def test_make_quote_pdf_bytes_returns_pdf(self) -> None:
        artifact = QuotePdfArtifact(
            quote_id="TEST123",
            quote_date=date(2026, 1, 15),
            pricebook_revision="R29 (NW) demo",
            customer_name="Demo Customer",
            customer_email="demo@example.com",
            building_label="Commercial Buildings",
            building_summary="40 x 60 x 14",
            line_items=(
                QuotePdfLineItem(description="Base building", qty=1, amount_cents=24790 * 100),
                QuotePdfLineItem(description="14' Height (Double Legs)", qty=1, amount_cents=4040 * 100),
            ),
            totals=QuotePdfTotals(
                building_amount_cents=28830 * 100,
                discount_cents=0,
                subtotal_cents=28830 * 100,
                additional_charges_cents=0,
                grand_total_cents=28830 * 100,
                downpayment_cents=int(round((28830 * 100) * 0.18)),
                balance_due_cents=(28830 * 100) - int(round((28830 * 100) * 0.18)),
            ),
        )
        pdf = make_quote_pdf_bytes(artifact)
        self.assertTrue(pdf.startswith(b"%PDF"))
        # Size varies by ReportLab version and whether images are embedded; just ensure it's non-trivial.
        self.assertGreater(len(pdf), 1000)
        self.assertIn(b"Downpayment", pdf)
        self.assertIn(b"Balance Due", pdf)

    def test_pdf_includes_one_page_per_view(self) -> None:
        # Minimal, valid PNG bytes for embedding:
        # Use short, deterministic dummy bytes? (ReportLab requires real images)
        # We'll generate simple images by using the existing logo extractor path? Not available here.
        # Instead, embed no logo and provide a tiny real PNG from a 1x1 data URI.
        png_1x1 = (
            b"\x89PNG\r\n\x1a\n"
            b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde"
            b"\x00\x00\x00\nIDAT\x08\xd7c\xf8\x0f\x00\x01\x01\x01\x00\x18\xdd\x8d\x9b"
            b"\x00\x00\x00\x00IEND\xaeB`\x82"
        )
        artifact = QuotePdfArtifact(
            quote_id="TESTVIEWS",
            quote_date=date(2026, 1, 15),
            pricebook_revision="R29 (NW) demo",
            customer_name="Demo Customer",
            customer_email="demo@example.com",
            building_label="Commercial Buildings",
            building_summary="40 x 60 x 14",
            line_items=(QuotePdfLineItem(description="Base building", qty=1, amount_cents=100 * 100),),
            totals=QuotePdfTotals(
                building_amount_cents=100 * 100,
                discount_cents=0,
                subtotal_cents=100 * 100,
                additional_charges_cents=0,
                grand_total_cents=100 * 100,
                downpayment_cents=18 * 100,
                balance_due_cents=82 * 100,
            ),
            building_preview_png_bytes=png_1x1,
            building_views_png_bytes={
                "front": png_1x1,
                "back": png_1x1,
                "left": png_1x1,
                "right": png_1x1,
                "isometric": png_1x1,
            },
        )
        pdf = make_quote_pdf_bytes(artifact)
        self.assertTrue(pdf.startswith(b"%PDF"))
        # 1 summary page + 5 view pages
        self.assertGreaterEqual(self._count_pdf_pages(pdf), 6)
        # Ensure each view page label is present in the PDF bytes (compression disabled).
        self.assertGreaterEqual(pdf.count(b"BUILDING VIEW"), 5)
        for label in (b"FRONT", b"BACK", b"LEFT", b"RIGHT", b"ISOMETRIC"):
            self.assertGreaterEqual(pdf.count(label), 1)


if __name__ == "__main__":
    unittest.main()

