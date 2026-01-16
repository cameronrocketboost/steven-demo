from __future__ import annotations

import unittest
from pathlib import Path

import local_demo_app
from local_demo_app import _build_selected_options_from_state
from normalized_pricebooks import build_demo_pricebook_r29, load_normalized_pricebook
from pricing_engine import CarportStyle, QuoteInput, RoofStyle, generate_quote


def _load_demo_book():
    root = Path(__file__).resolve().parents[1]
    candidates = [
        root / "out" / "Coast_To_Coast_Carports___Price_Book___R29_1" / "normalized_pricebook.json",
        root / "pricebooks" / "out" / "Coast_To_Coast_Carports___Price_Book___R29_1" / "normalized_pricebook.json",
    ]
    normalized_path = next((p for p in candidates if p.exists()), candidates[0])
    normalized = load_normalized_pricebook(normalized_path)
    return build_demo_pricebook_r29(normalized)


class TestDemoAppState(unittest.TestCase):
    def test_effective_state_uses_live_openings_when_doors_windows_is_active(self) -> None:
        """
        Regression test:

        The Doors & Windows step now supports explicit opening placement via `openings`.
        While this step is active, quote generation must read the live `openings` list
        from session_state (not the shadow snapshot), otherwise opening-backed line items
        appear to "not add" or "reset".
        """
        book = _load_demo_book()
        defaults = local_demo_app._default_state(book)

        fake_session_state: dict[str, object] = {
            "_shadow_state": {"openings": []},
            "openings": [{"id": 1, "kind": "door", "side": "front", "offset_ft": 4}],
            "opening_seq": 2,
        }

        original_session_state = local_demo_app.st.session_state
        try:
            local_demo_app.st.session_state = fake_session_state  # type: ignore[assignment]
            state = local_demo_app._effective_state(
                defaults,
                active_keys={"openings", "opening_seq"},
            )
            self.assertIsInstance(state.get("openings"), list)
            self.assertEqual(len(list(state.get("openings") or [])), 1)
            self.assertEqual(int(state.get("opening_seq") or 0), 2)
        finally:
            local_demo_app.st.session_state = original_session_state

    def test_effective_state_uses_shadow_for_non_active_leg_height(self) -> None:
        book = _load_demo_book()
        defaults = local_demo_app._default_state(book)

        # Simulate Doors & Windows step: leg_height is non-active. Streamlit resets it in live state.
        fake_session_state: dict[str, object] = {
            "_shadow_state": {
                "demo_style": "A-Frame (Horizontal)",
                "width_ft": 12,
                "length_ft": 21,
                "leg_height_ft": 11,
            },
            "demo_style": "A-Frame (Horizontal)",
            "width_ft": 12,
            "length_ft": 21,
            "leg_height_ft": 6,  # reset
            "wizard_step": 2,
        }

        original_session_state = local_demo_app.st.session_state
        try:
            local_demo_app.st.session_state = fake_session_state  # type: ignore[assignment]
            state = local_demo_app._effective_state(
                defaults,
                active_keys={"walk_in_door_type", "walk_in_door_count", "window_size", "window_count", "wizard_step"},
            )
            self.assertEqual(int(state["leg_height_ft"]), 11)
        finally:
            local_demo_app.st.session_state = original_session_state

    def test_options_rerun_does_not_overwrite_shadow_with_reset_fields(self) -> None:
        """
        Regression test for the real UI repro:

        - User sets doors/windows on Step 3 (shadow has correct values).
        - On Step 4 (Options), toggling J_TRIM triggers a rerun where Streamlit resets the
          doors/windows *values* (not dropping the keys).

        The app must NOT overwrite the shadow snapshot with the reset values during init, and
        must restore the non-active door/window fields from shadow on the Options step.
        """
        book = _load_demo_book()
        defaults = local_demo_app._default_state(book)

        fake_session_state: dict[str, object] = {
            "_shadow_state": {
                "walk_in_door_type": "Standard 36x80",
                "walk_in_door_count": 3,
                "window_size": "24x36",
                "window_count": 3,
            },
            # Streamlit "resets" values during rerun on Options step:
            "walk_in_door_type": "None",
            "walk_in_door_count": 0,
            "window_size": "None",
            "window_count": 0,
            "include_ground_certification": False,
            "selected_option_codes": ["J_TRIM"],
            "extra_panel_count": 1,
            "wizard_step": 3,
        }

        original_session_state = local_demo_app.st.session_state
        try:
            local_demo_app.st.session_state = fake_session_state  # type: ignore[assignment]
            # Init should not clobber shadow with the reset values.
            local_demo_app._init_state(book)
            # Options step sync should restore non-active doors/windows from shadow.
            local_demo_app._sync_shadow_state(
                defaults,
                active_keys={"include_ground_certification", "selected_option_codes", "extra_panel_count", "wizard_step"},
            )
            self.assertEqual(str(fake_session_state["walk_in_door_type"]), "Standard 36x80")
            self.assertEqual(int(fake_session_state["walk_in_door_count"]), 3)
            self.assertEqual(str(fake_session_state["window_size"]), "24x36")
            self.assertEqual(int(fake_session_state["window_count"]), 3)
        finally:
            local_demo_app.st.session_state = original_session_state

    def test_shadow_restore_does_not_lose_doors_windows_when_toggling_options(self) -> None:
        book = _load_demo_book()
        defaults = local_demo_app._default_state(book)

        # Simulate being on the Options step: doors/windows widgets are NOT active.
        # Streamlit "resets" (overwrites) the door/window fields to defaults during rerun.
        fake_session_state: dict[str, object] = {
            "_shadow_state": {
                "walk_in_door_type": "Standard 36x80",
                "walk_in_door_count": 2,
                "window_size": "24x36",
                "window_count": 2,
            },
            "walk_in_door_type": "None",
            "walk_in_door_count": 0,
            "window_size": "None",
            "window_count": 0,
            "include_ground_certification": True,
            "selected_option_codes": ["J_TRIM"],
            "wizard_step": 3,
        }

        original_session_state = local_demo_app.st.session_state
        try:
            local_demo_app.st.session_state = fake_session_state  # type: ignore[assignment]
            local_demo_app._sync_shadow_state(
                defaults,
                active_keys={"include_ground_certification", "selected_option_codes", "wizard_step"},
            )
            self.assertEqual(str(fake_session_state["walk_in_door_type"]), "Standard 36x80")
            self.assertEqual(int(fake_session_state["walk_in_door_count"]), 2)
            self.assertEqual(str(fake_session_state["window_size"]), "24x36")
            self.assertEqual(int(fake_session_state["window_count"]), 2)
        finally:
            local_demo_app.st.session_state = original_session_state

    def test_checkpoint_restore_rehydrates_prior_step_on_back(self) -> None:
        book = _load_demo_book()
        defaults = local_demo_app._default_state(book)

        # Simulate being on step 4, then clicking Back to step 3 where leg_height_ft should
        # come back as 11 via checkpoint restore.
        fake_session_state: dict[str, object] = {
            "wizard_step": 3,
            "wizard_checkpoints": {
                "2": {
                    "demo_style": "A-Frame (Horizontal)",
                    "width_ft": 12,
                    "length_ft": 21,
                    "leg_height_ft": 11,
                    "include_ground_certification": False,
                    "selected_option_codes": [],
                    "walk_in_door_type": "Standard 36x80",
                    "walk_in_door_count": 2,
                    "window_size": "None",
                    "window_count": 0,
                    "garage_door_type": "None",
                    "garage_door_size": "10x8",
                    "garage_door_count": 0,
                    "extra_panel_count": 0,
                }
            },
        }

        original_session_state = local_demo_app.st.session_state
        try:
            local_demo_app.st.session_state = fake_session_state  # type: ignore[assignment]
            local_demo_app._restore_checkpoint(2, defaults)
            self.assertEqual(int(fake_session_state["leg_height_ft"]), 11)
            self.assertEqual(str(fake_session_state["walk_in_door_type"]), "Standard 36x80")
            self.assertEqual(int(fake_session_state["walk_in_door_count"]), 2)
        finally:
            local_demo_app.st.session_state = original_session_state

    def test_restore_checkpoint_does_not_clobber_unrelated_shadow_keys(self) -> None:
        book = _load_demo_book()
        defaults = local_demo_app._default_state(book)

        # Shadow has last-known-good leg height, but live state was reset before navigation.
        fake_session_state: dict[str, object] = {
            "_shadow_state": {
                "demo_style": "A-Frame (Horizontal)",
                "width_ft": 22,
                "length_ft": 26,
                "leg_height_ft": 11,
                "walk_in_door_type": "Six Panel 36x80",
                "walk_in_door_count": 2,
            },
            "leg_height_ft": 6,  # reset
            "walk_in_door_type": "None",  # reset
            "walk_in_door_count": 0,
            "wizard_checkpoints": {
                # Target step checkpoint exists but doesn't mention leg_height/doors.
                "3": {"selected_option_codes": ["J_TRIM"]},
            },
        }

        original_session_state = local_demo_app.st.session_state
        try:
            local_demo_app.st.session_state = fake_session_state  # type: ignore[assignment]
            local_demo_app._restore_checkpoint(3, defaults)
            shadow = fake_session_state.get("_shadow_state")
            self.assertIsInstance(shadow, dict)
            self.assertEqual(int(shadow["leg_height_ft"]), 11)
            self.assertEqual(str(shadow["walk_in_door_type"]), "Six Panel 36x80")
            self.assertEqual(int(shadow["walk_in_door_count"]), 2)
        finally:
            local_demo_app.st.session_state = original_session_state

    def test_init_state_restores_dropped_leg_height_from_shadow(self) -> None:
        book = _load_demo_book()

        # Simulate Streamlit dropping a widget-backed key during wizard navigation.
        fake_session_state: dict[str, object] = {
            "_shadow_state": {
                "demo_style": "A-Frame (Horizontal)",
                "demo_style_prev": "A-Frame (Horizontal)",
                "width_ft": 12,
                "length_ft": 21,
                "leg_height_ft": 11,
                "include_ground_certification": False,
                "selected_option_codes": [],
                "walk_in_door_type": "None",
                "walk_in_door_count": 0,
                "window_size": "None",
                "window_count": 0,
                "garage_door_type": "None",
                "garage_door_size": "10x8",
                "garage_door_count": 0,
                "extra_panel_count": 0,
                "roof_color": "White",
                "trim_color": "White",
                "side_color": "White",
                "internal_notes": "",
                "wizard_step": 2,
            },
            # Key is "dropped" here: leg_height_ft missing in the live session_state.
            "demo_style": "A-Frame (Horizontal)",
            "width_ft": 12,
            "length_ft": 21,
            "wizard_step": 2,
        }

        original_session_state = local_demo_app.st.session_state
        try:
            local_demo_app.st.session_state = fake_session_state  # type: ignore[assignment]
            local_demo_app._init_state(book)
            self.assertEqual(int(fake_session_state["leg_height_ft"]), 11)
        finally:
            local_demo_app.st.session_state = original_session_state

    def test_build_selected_options_includes_counted_accessories(self) -> None:
        book = _load_demo_book()
        state = {
            "selected_option_codes": [],
            "walk_in_door_type": "Standard 36x80",
            "walk_in_door_count": 2,
            "window_size": "30x36",
            "window_count": 1,
            "garage_door_type": "Roll-up",
            "garage_door_size": "10x8",
            "garage_door_count": 1,
            "extra_panel_count": 3,
        }
        selected = _build_selected_options_from_state(state, book)
        codes = [s.code for s in selected]
        self.assertEqual(codes.count("WALK_IN_DOOR_STANDARD_36X80"), 2)
        self.assertEqual(codes.count("WINDOW_30X36"), 1)
        self.assertEqual(codes.count("ROLL_UP_DOOR_10X8"), 1)
        self.assertEqual(codes.count("EXTRA_PANEL"), 3)

    def test_quote_reflects_accessory_selections(self) -> None:
        book = _load_demo_book()
        state = {
            "selected_option_codes": [],
            "walk_in_door_type": "Standard 36x80",
            "walk_in_door_count": 1,
            "window_size": "24x36",
            "window_count": 2,
            "garage_door_type": "None",
            "garage_door_size": "10x8",
            "garage_door_count": 0,
            "extra_panel_count": 1,
        }
        inp = QuoteInput(
            style=CarportStyle.A_FRAME,
            roof_style=RoofStyle.HORIZONTAL,
            gauge=14,
            width_ft=12,
            length_ft=21,
            leg_height_ft=6,
            include_ground_certification=False,
            selected_options=_build_selected_options_from_state(state, book),
        )
        quote = generate_quote(inp, book)
        # Ensure the grouped line items include our accessory codes.
        codes = [li.code for li in quote.line_items]
        self.assertIn("WALK_IN_DOOR_STANDARD_36X80", codes)
        self.assertIn("WINDOW_24X36", codes)
        self.assertIn("EXTRA_PANEL", codes)


