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
            "chat_action_dirty_doors_windows": True,
        }

        original_session_state = local_demo_app.st.session_state
        try:
            local_demo_app.st.session_state = fake_session_state  # type: ignore[assignment]
            local_demo_app._maybe_sync_chat_action_for_step(book=book, step_key="doors_windows")
            # Dirty draft should not be overwritten by canonical empty openings.
            self.assertEqual(len(list(fake_session_state.get("chat_action_openings") or [])), 1)
            self.assertEqual(int(fake_session_state.get("chat_action_opening_seq") or 0), 2)
            self.assertEqual(str(fake_session_state.get("chat_action_last_synced_step")), "doors_windows")
        finally:
            local_demo_app.st.session_state = original_session_state

