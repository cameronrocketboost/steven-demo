from __future__ import annotations

from pricing_engine import CarportStyle, PriceBook, RoofStyle


def load_sample_pricebook_r29() -> PriceBook:
    """
    Hardcoded, small-slice demo data based on the R29 screenshots.

    Intent:
    - Enough to generate one convincing quote for recording a demo.
    - Easy to replace later with parsed Excel/CSV tables.
    """
    allowed_widths_ft = (12,)
    allowed_lengths_ft = (21, 26, 31, 36, 41, 46, 51)
    allowed_leg_heights_ft = (6, 7, 8, 9, 10, 11, 12)

    # Base prices (partial): Certified Carports 40 Lbs PSF, Vertical Roof Style, 14 gauge.
    # Screenshot clearly shows 12x20..12x50 values; we normalize to the nearest "allowed_lengths_ft".
    # For the demo we anchor to 21/26/31/etc and keep just one width.
    base_prices_usd = {
        (CarportStyle.A_FRAME, RoofStyle.VERTICAL, 14, 12, 21): 2895,
        (CarportStyle.A_FRAME, RoofStyle.VERTICAL, 14, 12, 26): 3495,
        (CarportStyle.A_FRAME, RoofStyle.VERTICAL, 14, 12, 31): 3995,
        (CarportStyle.A_FRAME, RoofStyle.VERTICAL, 14, 12, 36): 4595,
        (CarportStyle.A_FRAME, RoofStyle.VERTICAL, 14, 12, 41): 4995,
        (CarportStyle.A_FRAME, RoofStyle.VERTICAL, 14, 12, 46): 4995,
        (CarportStyle.A_FRAME, RoofStyle.VERTICAL, 14, 12, 51): 6195,
    }

    option_prices_by_length_usd = {
        # Option list screenshot: "GROUND CERTIFICATION"
        "GROUND_CERTIFICATION": {
            21: 600,
            26: 700,
            31: 700,
            36: 800,
            41: 900,
            46: 1000,
            51: 1200,
        }
    }

    # Option list screenshot: leg height add-on table (STD) for 6-12ft (partial but readable).
    leg_height_addon_by_length_usd = {
        6: {21: 110, 26: 135, 31: 150, 36: 180, 41: 205, 46: 240, 51: 265},
        7: {21: 0, 26: 0, 31: 0, 36: 0, 41: 0, 46: 0, 51: 0},
        8: {21: 200, 26: 265, 31: 300, 36: 360, 41: 410, 46: 480, 51: 530},
        9: {21: 325, 26: 400, 31: 450, 36: 580, 41: 690, 46: 720, 51: 800},
        10: {21: 1285, 26: 1630, 31: 1895, 36: 2220, 41: 2570, 46: 2960, 51: 3310},
        11: {21: 1390, 26: 1760, 31: 2050, 36: 2400, 41: 2770, 46: 3200, 51: 3570},
        12: {21: 1560, 26: 1895, 31: 2100, 36: 2580, 41: 2975, 46: 3440, 51: 3840},
    }

    return PriceBook(
        revision="R29 (screenshots) – page 4/5 sample",
        allowed_widths_ft=allowed_widths_ft,
        allowed_lengths_ft=allowed_lengths_ft,
        allowed_leg_heights_ft=allowed_leg_heights_ft,
        base_prices_usd=base_prices_usd,
        option_prices_by_length_usd=option_prices_by_length_usd,
        leg_height_addon_by_length_usd=leg_height_addon_by_length_usd,
    )


