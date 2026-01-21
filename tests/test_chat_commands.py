from __future__ import annotations

import unittest
from pathlib import Path

import local_demo_app
from normalized_pricebooks import build_demo_pricebook_r29, load_normalized_pricebook


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

