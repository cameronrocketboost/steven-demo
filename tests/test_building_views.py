from __future__ import annotations

import unittest

from building_views import (
    BuildingColorScheme,
    BuildingOpening,
    BuildingOpeningKind,
    BuildingSide,
    render_building_views_png,
)


class TestBuildingViews(unittest.TestCase):
    def test_render_building_views_returns_non_empty_png_bytes(self) -> None:
        views = render_building_views_png(
            width_ft=12,
            length_ft=21,
            height_ft=9,
            colors=BuildingColorScheme(roof="Burgundy", trim="White", sides="Sandstone"),
            openings=(
                BuildingOpening(side=BuildingSide.FRONT, kind=BuildingOpeningKind.DOOR, width_ft=3, height_ft=7),
                BuildingOpening(side=BuildingSide.RIGHT, kind=BuildingOpeningKind.WINDOW, width_ft=2, height_ft=3),
            ),
            view_names=("isometric", "front", "right"),
        )
        self.assertIn("isometric", views)
        self.assertIn("front", views)
        self.assertIn("right", views)
        self.assertGreater(len(views["isometric"]), 1000)

    def test_render_is_deterministic_for_same_inputs(self) -> None:
        a = render_building_views_png(
            width_ft=18,
            length_ft=26,
            height_ft=12,
            colors=BuildingColorScheme(roof="Red", trim="Black", sides="Tan"),
            openings=(
                BuildingOpening(side=BuildingSide.FRONT, kind=BuildingOpeningKind.GARAGE_DOOR, width_ft=10, height_ft=8),
                BuildingOpening(side=BuildingSide.RIGHT, kind=BuildingOpeningKind.WINDOW, width_ft=2, height_ft=3),
            ),
            view_names=("isometric",),
        )["isometric"]
        b = render_building_views_png(
            width_ft=18,
            length_ft=26,
            height_ft=12,
            colors=BuildingColorScheme(roof="Red", trim="Black", sides="Tan"),
            openings=(
                BuildingOpening(side=BuildingSide.FRONT, kind=BuildingOpeningKind.GARAGE_DOOR, width_ft=10, height_ft=8),
                BuildingOpening(side=BuildingSide.RIGHT, kind=BuildingOpeningKind.WINDOW, width_ft=2, height_ft=3),
            ),
            view_names=("isometric",),
        )["isometric"]
        self.assertEqual(a, b)

    def test_openings_change_output(self) -> None:
        base = render_building_views_png(
            width_ft=18,
            length_ft=26,
            height_ft=12,
            colors=BuildingColorScheme(roof="Red", trim="Black", sides="Tan"),
            view_names=("front",),
        )["front"]
        with_openings = render_building_views_png(
            width_ft=18,
            length_ft=26,
            height_ft=12,
            colors=BuildingColorScheme(roof="Red", trim="Black", sides="Tan"),
            openings=(BuildingOpening(side=BuildingSide.FRONT, kind=BuildingOpeningKind.DOOR, width_ft=3, height_ft=7),),
            view_names=("front",),
        )["front"]
        self.assertNotEqual(base, with_openings)


if __name__ == "__main__":
    unittest.main()

