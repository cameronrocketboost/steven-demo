from __future__ import annotations

import unittest
from pathlib import Path

import local_demo_app
from normalized_pricebooks import build_demo_pricebook_r29, load_normalized_pricebook


def _load_demo_book():
    root = Path(__file__).resolve().parents[1]
    candidates = [
        root / "out" / "Coast_To_Coast_Carports___Price_Book___R29_1" / "normalized_pricebook.json",
        root / "pricebooks" / "out" / "Coast_To_Coast_Carports___Price_Book___R29_1" / "normalized_pricebook.json",
    ]
    normalized_path = next((p for p in candidates if p.exists()), candidates[0])
    normalized = load_normalized_pricebook(normalized_path)
    return build_demo_pricebook_r29(normalized)


class _FakeSessionState(dict):
    def __getattr__(self, name: str):
        return self.get(name)

    def __setattr__(self, name: str, value) -> None:
        self[name] = value

    def __delattr__(self, name: str) -> None:
        try:
            del self[name]
        except KeyError as e:
            raise AttributeError(name) from e


class _StopRerun(Exception):
    pass


class TestChatCommands(unittest.TestCase):
    def test_chat_command_tokens_extracts_words(self) -> None:
        tokens = local_demo_app._chat_command_tokens("none continue, please!")
        self.assertIn("none", tokens)
        self.assertIn("continue", tokens)

    def test_chat_command_tokens_handles_start_over(self) -> None:
        tokens = local_demo_app._chat_command_tokens("Start over")
        self.assertIn("start", tokens)
        self.assertIn("over", tokens)

    def test_chat_slash_command_is_intentional(self) -> None:
        self.assertEqual(local_demo_app._chat_slash_command("/next"), "next")
        self.assertEqual(local_demo_app._chat_slash_command("  /HINT please"), "hint")
        self.assertEqual(local_demo_app._chat_slash_command(" /spply"), "apply")
        self.assertIsNone(local_demo_app._chat_slash_command("next"))

    def test_chat_bare_command_is_strict(self) -> None:
        self.assertEqual(local_demo_app._chat_bare_command("next"), "next")
        self.assertEqual(local_demo_app._chat_bare_command("next."), "next")
        self.assertIsNone(local_demo_app._chat_bare_command("next week"))

    def test_next_size_up(self) -> None:
        self.assertEqual(local_demo_app._next_size_up(20, [21, 26, 31]), 21)
        self.assertEqual(local_demo_app._next_size_up(21, [21, 26, 31]), 21)
        self.assertIsNone(local_demo_app._next_size_up(0, [21, 26, 31]))

    def test_parse_size_token(self) -> None:
        self.assertEqual(local_demo_app._parse_size_token("roll-up 10x8"), "10x8")
        self.assertEqual(local_demo_app._parse_size_token("10 x 10"), "10x10")
        self.assertIsNone(local_demo_app._parse_size_token("x10"))

    def test_parse_dimensions_ft_handles_common_variants(self) -> None:
        self.assertEqual(local_demo_app._parse_dimensions_ft("A-Frame 12x21"), (12, 21))
        self.assertEqual(local_demo_app._parse_dimensions_ft("12 × 21"), (12, 21))
        self.assertEqual(local_demo_app._parse_dimensions_ft("12' x 21'"), (12, 21))
        self.assertEqual(local_demo_app._parse_dimensions_ft("12 by 21 feet"), (12, 21))
        self.assertEqual(local_demo_app._parse_dimensions_ft("a frame 21 22"), (21, 22))

    def test_parse_style_label_is_forgiving(self) -> None:
        self.assertEqual(local_demo_app._parse_style_label("reular"), "Regular (Horizontal)")
        self.assertEqual(local_demo_app._parse_style_label("standard"), "Regular (Horizontal)")
        self.assertEqual(local_demo_app._parse_style_label("A frame vertical"), "A-Frame (Vertical)")

    def test_parse_opening_bulk_placement_instruction(self) -> None:
        self.assertEqual(
            local_demo_app._parse_opening_bulk_placement_instruction("3 doors on the left"),
            {"kind": "door", "side": "left", "count": 3},
        )
        self.assertEqual(
            local_demo_app._parse_opening_bulk_placement_instruction("all windows back"),
            {"kind": "window", "side": "back", "count": -1},
        )

    def test_openings_types_parses_doors_and_windows_in_one_message(self) -> None:
        book = _load_demo_book()
        steps = local_demo_app._wizard_steps()
        max_step_index = len(steps) - 1

        fake_session_state: _FakeSessionState = _FakeSessionState(
            {
                "lead_captured": True,
                "chat_messages": [],
                "chat_last_visible_at_ms": 0,
                "chat_last_scrolled_at_ms": 0,
                "wizard_step": 2,
                "walk_in_door_type": "None",
                "walk_in_door_count": 0,
                "window_size": "None",
                "window_count": 0,
                "garage_door_type": "None",
                "garage_door_size": "10x8",
                "garage_door_count": 0,
            }
        )

        def _raise_rerun() -> None:
            raise _StopRerun()

        original_session_state = local_demo_app.st.session_state
        original_rerun = local_demo_app.st.rerun
        try:
            local_demo_app.st.session_state = fake_session_state  # type: ignore[assignment]
            local_demo_app.st.rerun = _raise_rerun  # type: ignore[assignment]

            with self.assertRaises(_StopRerun):
                local_demo_app._handle_chat_input(
                    text="add 2 doors and 4 windows 24x36",
                    step_key="openings_types",
                    step_index=2,
                    max_step_index=max_step_index,
                    book=book,
                )

            self.assertEqual(int(fake_session_state.get("walk_in_door_count") or 0), 2)
            self.assertEqual(str(fake_session_state.get("window_size") or ""), "24x36")
            self.assertEqual(int(fake_session_state.get("window_count") or 0), 4)
            self.assertEqual(int(fake_session_state.get("wizard_step") or -1), 3)
        finally:
            local_demo_app.st.session_state = original_session_state
            local_demo_app.st.rerun = original_rerun

    def test_options_accepts_spaced_option_name_and_advances(self) -> None:
        book = _load_demo_book()
        steps = local_demo_app._wizard_steps()
        max_step_index = len(steps) - 1

        fake_session_state: _FakeSessionState = _FakeSessionState(
            {
                "lead_captured": True,
                "chat_messages": [],
                "chat_last_visible_at_ms": 0,
                "chat_last_scrolled_at_ms": 0,
                "wizard_step": 4,
                "include_ground_certification": False,
                "selected_option_codes": [],
                "extra_panel_count": 0,
            }
        )

        def _raise_rerun() -> None:
            raise _StopRerun()

        original_session_state = local_demo_app.st.session_state
        original_rerun = local_demo_app.st.rerun
        try:
            local_demo_app.st.session_state = fake_session_state  # type: ignore[assignment]
            local_demo_app.st.rerun = _raise_rerun  # type: ignore[assignment]

            # First rerun should ask for placement (optional) rather than doing nothing.
            with self.assertRaises(_StopRerun):
                local_demo_app._handle_chat_input(
                    text="j trim",
                    step_key="options",
                    step_index=4,
                    max_step_index=max_step_index,
                    book=book,
                )
            self.assertIn("J_TRIM", list(fake_session_state.get("selected_option_codes") or []))
            pending = fake_session_state.get("chat_pending_option_placement")
            self.assertIsInstance(pending, dict)
        finally:
            local_demo_app.st.session_state = original_session_state
            local_demo_app.st.rerun = original_rerun

    def test_colors_parses_all_fields_and_advances(self) -> None:
        book = _load_demo_book()
        steps = local_demo_app._wizard_steps()
        max_step_index = len(steps) - 1

        fake_session_state: _FakeSessionState = _FakeSessionState(
            {
                "lead_captured": True,
                "chat_messages": [],
                "chat_last_visible_at_ms": 0,
                "chat_last_scrolled_at_ms": 0,
                "wizard_step": 5,
                "roof_color": "White",
                "trim_color": "White",
                "side_color": "White",
            }
        )

        def _raise_rerun() -> None:
            raise _StopRerun()

        original_session_state = local_demo_app.st.session_state
        original_rerun = local_demo_app.st.rerun
        try:
            local_demo_app.st.session_state = fake_session_state  # type: ignore[assignment]
            local_demo_app.st.rerun = _raise_rerun  # type: ignore[assignment]

            with self.assertRaises(_StopRerun):
                local_demo_app._handle_chat_input(
                    text="roof white, trim black, side black",
                    step_key="colors",
                    step_index=5,
                    max_step_index=max_step_index,
                    book=book,
                )
            self.assertEqual(str(fake_session_state.get("roof_color") or ""), "White")
            self.assertEqual(str(fake_session_state.get("trim_color") or ""), "Black")
            self.assertEqual(str(fake_session_state.get("side_color") or ""), "Black")
            self.assertEqual(int(fake_session_state.get("wizard_step") or -1), 6)
        finally:
            local_demo_app.st.session_state = original_session_state
            local_demo_app.st.rerun = original_rerun

    def test_parse_opening_placement_instruction(self) -> None:
        self.assertEqual(
            local_demo_app._parse_opening_placement_instruction("door left 3"),
            {"kind": "door", "side": "left", "offset_ft": 3},
        )
        self.assertEqual(
            local_demo_app._parse_opening_placement_instruction("garage front"),
            {"kind": "garage", "side": "front", "offset_ft": 0},
        )
        self.assertIsNone(local_demo_app._parse_opening_placement_instruction("left 3"))

    def test_maybe_sync_chat_action_for_step_respects_dirty_flag(self) -> None:
        root = Path(__file__).resolve().parents[1]
        candidates = [
            root / "out" / "Coast_To_Coast_Carports___Price_Book___R29_1" / "normalized_pricebook.json",
            root / "pricebooks" / "out" / "Coast_To_Coast_Carports___Price_Book___R29_1" / "normalized_pricebook.json",
        ]
        normalized_path = next((p for p in candidates if p.exists()), candidates[0])
        normalized = load_normalized_pricebook(normalized_path)
        book = build_demo_pricebook_r29(normalized)

        fake_session_state: dict[str, object] = {
            # Canonical wizard state has no openings yet.
            "openings": [],
            "opening_seq": 1,
            "walk_in_door_type": "None",
            "window_size": "None",
            "garage_door_type": "None",
            "garage_door_size": "10x8",
            # Draft action card already has an opening (user just added it).
            "chat_action_openings": [{"id": 1, "kind": "door", "side": "front", "offset_ft": 0}],
            "chat_action_opening_seq": 2,
            "chat_action_dirty_openings_placement": True,
        }

        original_session_state = local_demo_app.st.session_state
        try:
            local_demo_app.st.session_state = fake_session_state  # type: ignore[assignment]
            local_demo_app._maybe_sync_chat_action_for_step(book=book, step_key="openings_placement")
            # Dirty draft should not be overwritten by canonical empty openings.
            self.assertEqual(len(list(fake_session_state.get("chat_action_openings") or [])), 1)
            self.assertEqual(int(fake_session_state.get("chat_action_opening_seq") or 0), 2)
            self.assertEqual(str(fake_session_state.get("chat_action_last_synced_step")), "openings_placement")
        finally:
            local_demo_app.st.session_state = original_session_state

    def test_chat_add_sets_visible_at_ms_with_assistant_delay(self) -> None:
        fake_session_state: dict[str, object] = {"chat_messages": [], "chat_last_visible_at_ms": 0}
        original_session_state = local_demo_app.st.session_state
        try:
            local_demo_app.st.session_state = fake_session_state  # type: ignore[assignment]
            local_demo_app._chat_add(role="assistant", content="First", tag="t1")
            local_demo_app._chat_add(role="assistant", content="Second", tag="t2")
            msgs = list(fake_session_state.get("chat_messages") or [])
            self.assertEqual(len(msgs), 2)
            v1 = int(msgs[0].get("visible_at_ms") or 0)
            v2 = int(msgs[1].get("visible_at_ms") or 0)
            self.assertGreaterEqual(v2, v1)
        finally:
            local_demo_app.st.session_state = original_session_state

    def test_built_size_chat_accepts_style_and_size_across_messages(self) -> None:
        book = _load_demo_book()
        steps = local_demo_app._wizard_steps()
        max_step_index = len(steps) - 1

        fake_session_state: _FakeSessionState = _FakeSessionState(
            {
                # Chat/lead basics.
                "lead_captured": True,
                "chat_messages": [],
                "chat_last_visible_at_ms": 0,
                "chat_last_scrolled_at_ms": 0,
                # Wizard basics.
                "wizard_step": 0,
                "demo_style": "A-Frame (Horizontal)",
                "width_ft": 12,
                "length_ft": 21,
                # Built & size explicitness flags.
                "chat_built_size_has_style": False,
                "chat_built_size_has_dims": False,
            }
        )

        def _raise_rerun() -> None:
            raise _StopRerun()

        original_session_state = local_demo_app.st.session_state
        original_rerun = local_demo_app.st.rerun
        try:
            local_demo_app.st.session_state = fake_session_state  # type: ignore[assignment]
            local_demo_app.st.rerun = _raise_rerun  # type: ignore[assignment]

            # Turn 1: size only → should remember dims and ask for style (not fail parsing).
            with self.assertRaises(_StopRerun):
                local_demo_app._handle_chat_input(
                    text="20x22",
                    step_key="built_size",
                    step_index=0,
                    max_step_index=max_step_index,
                    book=book,
                )
            self.assertTrue(bool(fake_session_state.get("chat_built_size_has_dims")))
            self.assertFalse(bool(fake_session_state.get("chat_built_size_has_style")))

            # Turn 2: typo'd style only → should parse as Regular and produce a size suggestion.
            with self.assertRaises(_StopRerun):
                local_demo_app._handle_chat_input(
                    text="reular",
                    step_key="built_size",
                    step_index=0,
                    max_step_index=max_step_index,
                    book=book,
                )
            self.assertTrue(bool(fake_session_state.get("chat_built_size_has_dims")))
            self.assertTrue(bool(fake_session_state.get("chat_built_size_has_style")))
            self.assertEqual(str(fake_session_state.get("demo_style")), "Regular (Horizontal)")
            pending = fake_session_state.get("chat_pending_suggestion")
            self.assertIsInstance(pending, dict)
            self.assertEqual(str(pending.get("kind")), "built_size")
            suggested = pending.get("suggested")
            self.assertIsInstance(suggested, dict)
            self.assertEqual(int(suggested.get("length_ft") or 0), 26)

            # Turn 3: typo'd /apply → should apply suggestion and auto-advance to next step.
            with self.assertRaises(_StopRerun):
                local_demo_app._handle_chat_input(
                    text="/spply",
                    step_key="built_size",
                    step_index=0,
                    max_step_index=max_step_index,
                    book=book,
                )
            self.assertEqual(int(fake_session_state.get("wizard_step") or -1), 1)
            self.assertIsNone(fake_session_state.get("chat_pending_suggestion"))
        finally:
            local_demo_app.st.session_state = original_session_state
            local_demo_app.st.rerun = original_rerun
