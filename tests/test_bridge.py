import unittest
from pathlib import Path
from unittest.mock import patch

import bridge
from openai_codex.generated.v2_all import SubAgentActivityThreadItem


class CompatibilityTests(unittest.TestCase):
    def test_completed_subagent_activity_can_be_read(self):
        item = SubAgentActivityThreadItem.model_validate(
            {
                "type": "subAgentActivity",
                "id": "subagent-completed-test",
                "agentPath": "/root/review",
                "agentThreadId": "00000000-0000-0000-0000-000000000000",
                "kind": "completed",
            }
        )

        self.assertIsNotNone(item.kind)

    def test_configured_codex_binary_has_highest_priority(self):
        with patch.dict("os.environ", {"CODEX_BIN": "/opt/codex/bin/codex"}), patch(
            "bridge.shutil.which", return_value="/usr/local/bin/codex"
        ):
            self.assertEqual(bridge.resolve_codex_bin(), "/opt/codex/bin/codex")

    def test_stable_user_install_wins_over_transient_path_shim(self):
        stable_bin = Path.home() / ".local" / "bin" / "codex"
        if not stable_bin.is_file():
            self.skipTest("stable user Codex install is not present")

        with patch.dict("os.environ", {"CODEX_BIN": ""}), patch(
            "bridge.shutil.which", return_value="/tmp/transient/codex"
        ):
            self.assertEqual(bridge.resolve_codex_bin(), str(stable_bin))


class FastModeTests(unittest.TestCase):
    def test_service_tier_follows_all_chats_bound_to_a_thread(self):
        instance = bridge.Bridge.__new__(bridge.Bridge)
        instance.initial_cwd = "/tmp"
        instance.state = {
            "chat-a": {"thread_id": "thread-1", "service_tier": None},
            "chat-b": {"thread_id": "thread-1", "service_tier": None},
            "chat-c": {"thread_id": "thread-2", "service_tier": None},
        }
        instance.save_state = lambda: None

        instance.set_service_tier("chat-a", "fast")

        self.assertEqual(instance.state["chat-a"]["service_tier"], "fast")
        self.assertEqual(instance.state["chat-b"]["service_tier"], "fast")
        self.assertIsNone(instance.state["chat-c"]["service_tier"])

    def test_bridge_defaults_and_explicit_fast_off(self):
        instance = bridge.Bridge.__new__(bridge.Bridge)
        instance.reasoning_effort = "high"
        instance.service_tier = "fast"
        session = {"reasoning_effort": None, "service_tier": None}

        self.assertEqual(instance.effective_reasoning_effort(session), "high")
        self.assertEqual(instance.effective_service_tier(session), "fast")

        session["service_tier"] = "off"
        self.assertIsNone(instance.effective_service_tier(session))


class CommandHintTests(unittest.IsolatedAsyncioTestCase):
    async def test_bare_slash_shows_commands_without_starting_a_task(self):
        instance = bridge.Bridge.__new__(bridge.Bridge)
        instance.allowed_open_id = "ou_test"
        instance.initial_cwd = "/Users/bytedance/Projects"
        instance.state = {}
        instance.seen_ids = []
        instance.pending_deletes = {}
        instance.active_turns = {}
        instance.mark_seen = lambda _message_id: None
        replies = []

        async def reply(_message, text):
            replies.append(text)

        instance.reply = reply
        message = type(
            "Message",
            (),
            {
                "sender_id": "ou_test",
                "id": "message-1",
                "body_text": "/",
                "content_text": "/",
                "chat_id": "chat-1",
            },
        )()

        await instance.on_message(message)

        self.assertEqual(len(replies), 1)
        self.assertIn("/resume", replies[0])
        self.assertIn("/fast", replies[0])
        self.assertNotIn("chat-1", instance.active_turns)


if __name__ == "__main__":
    unittest.main()
