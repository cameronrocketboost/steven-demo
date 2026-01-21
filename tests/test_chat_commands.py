from __future__ import annotations

import unittest
from pathlib import Path

import local_demo_app
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
            # Micro-step flow: doors+windows handled, next up is garage doors.
            self.assertEqual(int(fake_session_state.get("wizard_step") or -1), 2)
            self.assertEqual(str(fake_session_state.get("openings_types_stage") or ""), "garage")

            # Finish the last micro-step (garage) with "none" → advance to placement step.
            with self.assertRaises(_StopRerun):
                local_demo_app._handle_chat_input(
                    text="none",
                    step_key="openings_types",
                    step_index=2,
                    max_step_index=max_step_index,
                    book=book,
                )
            self.assertEqual(int(fake_session_state.get("wizard_step") or -1), 3)
        finally:
            local_demo_app.st.session_state = original_session_state
            local_demo_app.st.rerun = original_rerun

    def test_openings_types_parses_rollup_count(self) -> None:
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

            # "2 roll-up 10x8" should set quantity to 2 (not 1).
            with self.assertRaises(_StopRerun):
                local_demo_app._handle_chat_input(
                    text="2 roll-up 10x8",
                    step_key="openings_types",
                    step_index=2,
                    max_step_index=max_step_index,
                    book=book,
                )
            self.assertEqual(str(fake_session_state.get("garage_door_type") or ""), "Roll-up")
            self.assertEqual(str(fake_session_state.get("garage_door_size") or ""), "10x8")
            self.assertEqual(int(fake_session_state.get("garage_door_count") or 0), 2)
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

            # Micro-step 1: ground certification question.
            with self.assertRaises(_StopRerun):
                local_demo_app._handle_chat_input(
                    text="no",
                    step_key="options",
                    step_index=4,
                    max_step_index=max_step_index,
                    book=book,
                )

            # Micro-step 2: choose J-Trim (spaced input) → should prompt for placement (optional) rather than doing nothing.
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

            # Regression: "back" is a navigation command, but when we're explicitly prompting
            # for option placement it should be interpreted as BACK placement (rear wall).
            with self.assertRaises(_StopRerun):
                local_demo_app._handle_chat_input(
                    text="back",
                    step_key="options",
                    step_index=4,
                    max_step_index=max_step_index,
                    book=book,
                )
            self.assertEqual(fake_session_state.get("placement_J_TRIM"), local_demo_app.SectionPlacement.BACK)
        finally:
            local_demo_app.st.session_state = original_session_state
            local_demo_app.st.rerun = original_rerun

    def test_options_parses_double_leg_alias(self) -> None:
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

            # Answer ground certification first.
            with self.assertRaises(_StopRerun):
                local_demo_app._handle_chat_input(
                    text="no",
                    step_key="options",
                    step_index=4,
                    max_step_index=max_step_index,
                    book=book,
                )

            # Then choose "double leg" (alias for DOUBLE_LEG_UP_TO_12).
            with self.assertRaises(_StopRerun):
                local_demo_app._handle_chat_input(
                    text="double leg",
                    step_key="options",
                    step_index=4,
                    max_step_index=max_step_index,
                    book=book,
                )
            self.assertIn("DOUBLE_LEG_UP_TO_12", list(fake_session_state.get("selected_option_codes") or []))
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

    def test_colors_parses_all_fields_without_commas_and_advances(self) -> None:
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
                    text="roof black trim white sides tan",
                    step_key="colors",
                    step_index=5,
                    max_step_index=max_step_index,
                    book=book,
                )
            self.assertEqual(str(fake_session_state.get("roof_color") or ""), "Black")
            self.assertEqual(str(fake_session_state.get("trim_color") or ""), "White")
            self.assertEqual(str(fake_session_state.get("side_color") or ""), "Tan")
            self.assertEqual(int(fake_session_state.get("wizard_step") or -1), 6)
        finally:
            local_demo_app.st.session_state = original_session_state
            local_demo_app.st.rerun = original_rerun

    def test_notes_no_does_not_navigate_back(self) -> None:
        book = _load_demo_book()
        steps = local_demo_app._wizard_steps()
        max_step_index = len(steps) - 1
        notes_idx = next(i for i, (_, k) in enumerate(steps) if k == "notes")
        quote_idx = next(i for i, (_, k) in enumerate(steps) if k == "quote")

        fake_session_state: _FakeSessionState = _FakeSessionState(
            {
                "lead_captured": True,
                "chat_messages": [],
                "chat_last_visible_at_ms": 0,
                "chat_last_scrolled_at_ms": 0,
                "wizard_step": notes_idx,
                "internal_notes": "something",
                # Simulate an auto-advance into Notes (so "no" could be misread as "go back").
                "chat_last_auto_advance": {"from_step_index": notes_idx - 1, "to_step_index": notes_idx},
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
                    text="no",
                    step_key="notes",
                    step_index=notes_idx,
                    max_step_index=max_step_index,
                    book=book,
                )
            self.assertEqual(str(fake_session_state.get("internal_notes") or ""), "")
            self.assertEqual(int(fake_session_state.get("wizard_step") or -1), quote_idx)
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

    def test_full_chat_conversation_step_by_step_produces_consistent_quote(self) -> None:
        """
        End-to-end regression test (chat-driven wizard).

        Simulates a real user conversation, step-by-step:
        - lead capture
        - built & size
        - leg height
        - openings (types)
        - openings (placement)
        - options (+ per-option placement prompt)
        - colors
        - notes

        Also simulates the prod Streamlit failure mode where non-active widget values get
        reset during an Options-step rerun; shadow-state restore must keep the earlier
        openings/doors/windows intact.
        """
        book = _load_demo_book()
        steps = local_demo_app._wizard_steps()
        max_step_index = len(steps) - 1
        defaults = local_demo_app._default_state(book)

        fake_session_state: _FakeSessionState = _FakeSessionState(
            {
                # Chat / lead basics
                "lead_captured": False,
                "lead_name": "",
                "lead_email": "",
                "chat_messages": [],
                "chat_last_visible_at_ms": 0,
                "chat_last_scrolled_at_ms": 0,
                # Wizard index
                "wizard_step": 0,
            }
        )

        def _raise_rerun() -> None:
            raise _StopRerun()

        def _active_keys_for_step(step_key: str) -> set[str]:
            active_keys: set[str] = set()
            if step_key == "built_size":
                active_keys.update({"demo_style", "demo_style_prev", "width_ft", "length_ft"})
            elif step_key == "leg_height":
                active_keys.add("leg_height_ft")
            elif step_key == "openings_types":
                active_keys.update(
                    {
                        "walk_in_door_type",
                        "walk_in_door_count",
                        "window_size",
                        "window_count",
                        "garage_door_type",
                        "garage_door_size",
                        "garage_door_count",
                    }
                )
            elif step_key == "openings_placement":
                active_keys.update(
                    {
                        "walk_in_door_type",
                        "walk_in_door_count",
                        "window_size",
                        "window_count",
                        "garage_door_type",
                        "garage_door_size",
                        "garage_door_count",
                        "openings",
                        "opening_seq",
                    }
                )
            elif step_key == "options":
                active_keys.update({"include_ground_certification", "selected_option_codes", "extra_panel_count"})
                for k in list(fake_session_state.keys()):
                    if isinstance(k, str) and k.startswith("placement_"):
                        active_keys.add(k)
            elif step_key == "colors":
                active_keys.update({"roof_color", "trim_color", "side_color"})
            elif step_key == "notes":
                active_keys.add("internal_notes")

            active_keys.add("wizard_step")
            active_keys.update({"manufacturer_discount_pct", "downpayment_pct"})
            return active_keys

        def _sync_like_main() -> dict[str, object]:
            step_index = int(fake_session_state.get("wizard_step") or 0)
            step_index = max(0, min(step_index, max_step_index))
            step_key = str(steps[step_index][1])
            active_keys = _active_keys_for_step(step_key)
            last_auto = fake_session_state.get("chat_last_auto_advance")
            if isinstance(last_auto, dict):
                try:
                    from_idx = int(last_auto.get("from_step_index"))
                    to_idx = int(last_auto.get("to_step_index"))
                except Exception:
                    from_idx = -1
                    to_idx = -1
                if not bool(last_auto.get("shadow_committed")) and to_idx == int(step_index) and 0 <= from_idx <= max_step_index:
                    prev_key = str(steps[from_idx][1])
                    active_keys.update(_active_keys_for_step(prev_key))
                    fake_session_state["chat_last_auto_advance"] = {**last_auto, "shadow_committed": True}
            local_demo_app._sync_shadow_state(defaults, active_keys=active_keys)
            return local_demo_app._effective_state(defaults, active_keys=active_keys)

        original_session_state = local_demo_app.st.session_state
        original_rerun = local_demo_app.st.rerun
        try:
            local_demo_app.st.session_state = fake_session_state  # type: ignore[assignment]
            local_demo_app.st.rerun = _raise_rerun  # type: ignore[assignment]

            # Initialize persisted defaults + shadow-state behavior.
            local_demo_app._init_state(book)

            # Regression: stale Streamlit session_state can contain an invalid chat_action width
            # (or a string-typed width), which would crash `st.selectbox` in the Conversation panel.
            fake_session_state["chat_action_last_synced_step"] = "built_size"
            fake_session_state["chat_action_width_ft"] = "999"
            local_demo_app._maybe_sync_chat_action_for_step(book=book, step_key="built_size")
            self.assertIn(int(fake_session_state.get("chat_action_width_ft") or 0), list(book.allowed_widths_ft))

            # Lead capture via chat (name, then email).
            with self.assertRaises(_StopRerun):
                local_demo_app._handle_chat_input(
                    text="Cameron",
                    step_key="built_size",
                    step_index=0,
                    max_step_index=max_step_index,
                    book=book,
                )
            with self.assertRaises(_StopRerun):
                local_demo_app._handle_chat_input(
                    text="cameron@example.com",
                    step_key="built_size",
                    step_index=0,
                    max_step_index=max_step_index,
                    book=book,
                )
            self.assertTrue(bool(fake_session_state.get("lead_captured")))
            _sync_like_main()

            # Step 0: Built & Size (single natural message).
            with self.assertRaises(_StopRerun):
                local_demo_app._handle_chat_input(
                    text="A-Frame 12x21",
                    step_key="built_size",
                    step_index=0,
                    max_step_index=max_step_index,
                    book=book,
                )
            self.assertEqual(int(fake_session_state.get("wizard_step") or -1), 1)
            self.assertEqual(str(fake_session_state.get("demo_style") or ""), "A-Frame (Horizontal)")
            self.assertEqual(int(fake_session_state.get("width_ft") or 0), 12)
            self.assertEqual(int(fake_session_state.get("length_ft") or 0), 21)
            _sync_like_main()

            # Step 1: Leg height.
            with self.assertRaises(_StopRerun):
                local_demo_app._handle_chat_input(
                    text="11",
                    step_key="leg_height",
                    step_index=1,
                    max_step_index=max_step_index,
                    book=book,
                )
            self.assertEqual(int(fake_session_state.get("wizard_step") or -1), 2)
            self.assertEqual(int(fake_session_state.get("leg_height_ft") or 0), 11)
            _sync_like_main()

            # Step 2: Openings types (doors + windows in one message).
            with self.assertRaises(_StopRerun):
                local_demo_app._handle_chat_input(
                    text="add 2 doors and 4 windows 24x36",
                    step_key="openings_types",
                    step_index=2,
                    max_step_index=max_step_index,
                    book=book,
                )
            # Micro-step flow: doors+windows handled; "garage" is the next micro-step.
            self.assertEqual(int(fake_session_state.get("wizard_step") or -1), 2)
            self.assertEqual(str(fake_session_state.get("openings_types_stage") or ""), "garage")
            self.assertEqual(int(fake_session_state.get("walk_in_door_count") or 0), 2)
            self.assertEqual(str(fake_session_state.get("window_size") or ""), "24x36")
            self.assertEqual(int(fake_session_state.get("window_count") or 0), 4)

            # Finish the garage micro-step (none) → advance to placement.
            with self.assertRaises(_StopRerun):
                local_demo_app._handle_chat_input(
                    text="none",
                    step_key="openings_types",
                    step_index=2,
                    max_step_index=max_step_index,
                    book=book,
                )
            self.assertEqual(int(fake_session_state.get("wizard_step") or -1), 3)
            _sync_like_main()

            # Step 3: Openings placement (bulk place).
            with self.assertRaises(_StopRerun):
                local_demo_app._handle_chat_input(
                    text="yes",
                    step_key="openings_placement",
                    step_index=3,
                    max_step_index=max_step_index,
                    book=book,
                )
            with self.assertRaises(_StopRerun):
                local_demo_app._handle_chat_input(
                    text="all doors left",
                    step_key="openings_placement",
                    step_index=3,
                    max_step_index=max_step_index,
                    book=book,
                )
            with self.assertRaises(_StopRerun):
                local_demo_app._handle_chat_input(
                    text="all windows back",
                    step_key="openings_placement",
                    step_index=3,
                    max_step_index=max_step_index,
                    book=book,
                )
            openings = list(fake_session_state.get("openings") or [])
            self.assertEqual(len(openings), 6)  # 2 doors + 4 windows
            _sync_like_main()

            # Advance placement step.
            with self.assertRaises(_StopRerun):
                local_demo_app._handle_chat_input(
                    text="/next",
                    step_key="openings_placement",
                    step_index=3,
                    max_step_index=max_step_index,
                    book=book,
                )
            self.assertEqual(int(fake_session_state.get("wizard_step") or -1), 4)
            _sync_like_main()

            # Simulate Streamlit reset bug on non-active keys during Options step rerun.
            fake_session_state["walk_in_door_type"] = "None"
            fake_session_state["walk_in_door_count"] = 0
            fake_session_state["window_size"] = "None"
            fake_session_state["window_count"] = 0
            fake_session_state["openings"] = []
            fake_session_state["opening_seq"] = 1
            state_after_restore = _sync_like_main()
            self.assertEqual(int(state_after_restore.get("walk_in_door_count") or 0), 2)
            self.assertEqual(str(state_after_restore.get("window_size") or ""), "24x36")
            self.assertEqual(int(state_after_restore.get("window_count") or 0), 4)
            self.assertEqual(len(list(state_after_restore.get("openings") or [])), 6)

            # Step 4: Options — micro-step 1 (ground certification).
            with self.assertRaises(_StopRerun):
                local_demo_app._handle_chat_input(
                    text="no",
                    step_key="options",
                    step_index=4,
                    max_step_index=max_step_index,
                    book=book,
                )
            self.assertEqual(int(fake_session_state.get("wizard_step") or -1), 4)
            self.assertFalse(bool(fake_session_state.get("include_ground_certification")))
            _sync_like_main()

            # Step 4: Options — micro-step 2 (J_TRIM) should prompt for placement, not advance yet.
            with self.assertRaises(_StopRerun):
                local_demo_app._handle_chat_input(
                    text="j trim",
                    step_key="options",
                    step_index=4,
                    max_step_index=max_step_index,
                    book=book,
                )
            self.assertEqual(int(fake_session_state.get("wizard_step") or -1), 4)
            self.assertIn("J_TRIM", list(fake_session_state.get("selected_option_codes") or []))
            self.assertIsInstance(fake_session_state.get("chat_pending_option_placement"), dict)
            _sync_like_main()

            # Provide per-option placement and advance to Colors.
            with self.assertRaises(_StopRerun):
                local_demo_app._handle_chat_input(
                    text="left",
                    step_key="options",
                    step_index=4,
                    max_step_index=max_step_index,
                    book=book,
                )
            self.assertEqual(int(fake_session_state.get("wizard_step") or -1), 5)
            _sync_like_main()

            # Step 5: Colors.
            with self.assertRaises(_StopRerun):
                local_demo_app._handle_chat_input(
                    text="roof - black - sides gray - trim tan",
                    step_key="colors",
                    step_index=5,
                    max_step_index=max_step_index,
                    book=book,
                )
            self.assertEqual(int(fake_session_state.get("wizard_step") or -1), 6)
            self.assertEqual(str(fake_session_state.get("roof_color") or ""), "Black")
            self.assertEqual(str(fake_session_state.get("trim_color") or ""), "Tan")
            self.assertEqual(str(fake_session_state.get("side_color") or ""), "Gray")
            _sync_like_main()

            # Regression: chat auto-advance can skip a rerun on the Colors step, so shadow-state must still
            # capture the chosen colors before later reruns/reset-like behavior.
            fake_session_state["roof_color"] = "White"
            fake_session_state["trim_color"] = "White"
            fake_session_state["side_color"] = "White"
            restored = _sync_like_main()
            self.assertEqual(str(restored.get("roof_color") or ""), "Black")
            self.assertEqual(str(restored.get("trim_color") or ""), "Tan")
            self.assertEqual(str(restored.get("side_color") or ""), "Gray")

            # Step 6: Notes.
            with self.assertRaises(_StopRerun):
                local_demo_app._handle_chat_input(
                    text="none",
                    step_key="notes",
                    step_index=6,
                    max_step_index=max_step_index,
                    book=book,
                )
            self.assertEqual(int(fake_session_state.get("wizard_step") or -1), 7)

            # Generate a real quote from effective state (mirrors main()).
            quote_state = _sync_like_main()
            demo_style = str(quote_state.get("demo_style") or "")
            if demo_style == "Regular (Horizontal)":
                style = CarportStyle.REGULAR
                roof_style = RoofStyle.HORIZONTAL
            elif demo_style == "A-Frame (Vertical)":
                style = CarportStyle.A_FRAME
                roof_style = RoofStyle.VERTICAL
            else:
                style = CarportStyle.A_FRAME
                roof_style = RoofStyle.HORIZONTAL

            selected_options = local_demo_app._build_selected_options_from_state(quote_state, book)
            inp = QuoteInput(
                style=style,
                roof_style=roof_style,
                gauge=14,
                width_ft=int(quote_state.get("width_ft") or 0),
                length_ft=int(quote_state.get("length_ft") or 0),
                leg_height_ft=int(quote_state.get("leg_height_ft") or 0),
                include_ground_certification=bool(quote_state.get("include_ground_certification")),
                selected_options=selected_options,
                closed_end_count=0,
                closed_side_count=0,
                lean_to_enabled=False,
                lean_to_width_ft=0,
                lean_to_length_ft=0,
                lean_to_placement=None,
            )
            quote = generate_quote(inp, book)

            # Assert quote includes our chosen options and is non-trivial.
            option_codes = [s.code for s in selected_options]
            self.assertIn("J_TRIM", option_codes)
            self.assertGreaterEqual(option_codes.count("J_TRIM"), 1)
            self.assertGreater(quote.total_usd, 0)
            self.assertIn("R29", quote.pricebook_revision)

            line_item_codes = [li.code for li in quote.line_items]
            self.assertIn("J_TRIM", line_item_codes)
            self.assertTrue(any(c.startswith("WALK_IN_DOOR_") for c in line_item_codes))
            self.assertTrue(any(c.startswith("WINDOW_") for c in line_item_codes))
        finally:
            local_demo_app.st.session_state = original_session_state
            local_demo_app.st.rerun = original_rerun
