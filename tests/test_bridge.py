import asyncio
import concurrent.futures
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import bridge as bridge_module
from bridge import ActiveTurn, Bridge


class FakeChannel:
    def __init__(self):
        self.sent = []
        self.scheduled = []

    async def send(self, receive_id, content, options=None):
        self.sent.append((receive_id, content["text"], options))
        return SimpleNamespace(success=True, error=None)

    def schedule(self, coroutine):
        task = asyncio.create_task(coroutine)
        future = concurrent.futures.Future()

        def finish(completed):
            if completed.cancelled():
                future.cancel()
            elif completed.exception() is not None:
                future.set_exception(completed.exception())
            else:
                future.set_result(completed.result())

        task.add_done_callback(finish)
        self.scheduled.append(future)
        return future


class FakeClient:
    def __init__(self):
        self.closed = asyncio.Event()
        self.close_calls = 0
        self.thread = FakeThread()

    async def __aenter__(self):
        return self

    async def close(self):
        self.close_calls += 1
        self.closed.set()

    async def thread_start(self, **_options):
        return self.thread


class FailingCloseClient(FakeClient):
    async def close(self):
        self.close_calls += 1
        raise RuntimeError("close failed")


class HangingControlClient(FakeClient):
    async def models(self):
        await asyncio.Event().wait()


class StartingClient(FakeClient):
    def __init__(self):
        super().__init__()
        self.started = asyncio.Event()

    async def __aenter__(self):
        self.started.set()
        await self.closed.wait()
        raise ConnectionError("closed during startup")


class FakeThread:
    def __init__(self):
        self.id = "thread-new"
        self.turn_calls = []

    async def turn(self, text, **options):
        self.turn_calls.append((text, options))
        return SimpleNamespace(id="turn-new")


class HangingTurnStartThread(FakeThread):
    async def turn(self, text, **options):
        self.turn_calls.append((text, options))
        await asyncio.Event().wait()


class FakeTurn:
    def __init__(self):
        self.interrupt_calls = 0

    async def interrupt(self):
        self.interrupt_calls += 1


class HangingTurn:
    id = "turn-hanging"

    def __init__(self, client):
        self.client = client
        self.closed = False

    async def stream(self):
        try:
            await self.client.closed.wait()
            raise ConnectionError("app-server closed")
            yield None
        finally:
            self.closed = True


class BlockingTierBridge(Bridge):
    def __init__(self, channel):
        super().__init__(channel)
        self.tier_started = asyncio.Event()
        self.release_tier = asyncio.Event()

    async def effective_service_tier(self, _session):
        self.tier_started.set()
        await self.release_tier.wait()
        return "default"


class BridgeReconnectTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.env = patch.dict(
            "os.environ",
            {
                "XDG_CONFIG_HOME": self.temp_dir.name,
                "FEISHU_ALLOWED_OPEN_ID": "user-1",
                "CODEX_REQUEST_TIMEOUT_SECONDS": "0.02",
                "CODEX_TURN_IDLE_TIMEOUT_SECONDS": "0.02",
                "CODEX_RECONNECT_CLEANUP_TIMEOUT_SECONDS": "0.02",
            },
            clear=False,
        )
        self.env.start()
        self.channel = FakeChannel()
        self.bridge = Bridge(self.channel)

    async def asyncTearDown(self):
        self.env.stop()
        self.temp_dir.cleanup()

    async def test_control_commands_use_bounded_short_lived_client(self):
        client = HangingControlClient()

        with patch.object(bridge_module, "AsyncCodex", return_value=client):
            with self.assertRaises(asyncio.TimeoutError):
                await self.bridge.available_models()

        self.assertEqual(client.close_calls, 1)

    async def test_reconnect_gate_rechecks_seen_id_after_wait(self):
        self.bridge.reconnecting = True
        messages = [
            SimpleNamespace(
                id="duplicate",
                message_id="duplicate",
                chat_id="chat-1",
                chat_type="p2p",
                sender_id="user-1",
                body_text="/pwd",
                content_text="/pwd",
            )
            for _ in range(2)
        ]
        calls = 0
        original_mark_seen = self.bridge.mark_seen

        def count_mark_seen(message_id):
            nonlocal calls
            calls += 1
            original_mark_seen(message_id)

        self.bridge.mark_seen = count_mark_seen
        handlers = [asyncio.create_task(self.bridge.on_message(message)) for message in messages]
        await asyncio.sleep(0)
        self.bridge.reconnecting = False
        await asyncio.gather(*handlers)

        self.assertEqual(calls, 1)

    async def test_active_slot_exists_before_fast_tier_lookup(self):
        bridge = BlockingTierBridge(self.channel)
        message = SimpleNamespace(
            id="message-fast",
            message_id="message-fast",
            chat_id="chat-fast",
            chat_type="p2p",
            sender_id="user-1",
            body_text="start once",
            content_text="start once",
        )

        client = FakeClient()
        with patch.object(bridge_module, "AsyncCodex", return_value=client):
            handler = asyncio.create_task(bridge.on_message(message))
            await bridge.tier_started.wait()

            self.assertIn("chat-fast", bridge.active_turns)
            bridge.on_reconnecting()
            bridge.release_tier.set()
            await asyncio.wrap_future(bridge.reconnect_cleanup_future)
            await asyncio.gather(handler, return_exceptions=True)
        self.assertEqual(bridge.last_tasks["chat-fast"]["status"], "interrupted")

    async def test_old_reconnect_generation_cannot_open_new_gate(self):
        self.bridge.on_reconnecting()
        first_generation = self.bridge.reconnect_generation
        first_cleanup = self.bridge.reconnect_cleanup_future
        self.bridge.on_reconnecting()
        second_generation = self.bridge.reconnect_generation

        await self.bridge.notify_reconnected(first_generation, first_cleanup)

        self.assertGreater(second_generation, first_generation)
        self.assertTrue(self.bridge.reconnecting)

    async def test_reconnect_interrupts_stuck_turn_and_preserves_prompt(self):
        client = FakeClient()
        turn = FakeTurn()
        owner = asyncio.create_task(client.closed.wait())
        state = ActiveTurn(task=owner, chat_id="chat-1", client=client, turn=turn)
        self.bridge.active_turns["chat-1"] = state
        self.bridge.last_tasks["chat-1"] = {
            "status": "running",
            "chat_id": "chat-1",
            "cwd": "/tmp/project",
            "prompt": "do work",
            "delivered": False,
        }

        self.bridge.on_reconnecting()
        interrupted = await asyncio.wrap_future(self.bridge.reconnect_cleanup_future)

        self.assertEqual(interrupted, ["chat-1"])
        self.assertEqual(turn.interrupt_calls, 1)
        self.assertEqual(client.close_calls, 1)
        self.assertNotIn("chat-1", self.bridge.active_turns)
        task = self.bridge.last_tasks["chat-1"]
        self.assertEqual(task["status"], "interrupted")
        self.assertEqual(task["prompt"], "do work")

        self.bridge.on_reconnected()
        await asyncio.wrap_future(self.channel.scheduled[-1])
        self.assertIn("没有自动重复执行任务", self.channel.sent[-1][1])
        self.assertIn("/recover-last", self.channel.sent[-1][1])

    async def test_reconnect_during_startup_preserves_recoverable_task(self):
        client = StartingClient()
        message = SimpleNamespace(
            id="message-starting",
            message_id="message-starting",
            chat_id="chat-starting",
            chat_type="p2p",
            sender_id="user-1",
            body_text="do not lose this",
            content_text="do not lose this",
        )

        with patch.object(bridge_module, "AsyncCodex", return_value=client):
            owner = asyncio.create_task(self.bridge.on_message(message))
            await client.started.wait()
            self.bridge.on_reconnecting()
            await asyncio.wrap_future(self.bridge.reconnect_cleanup_future)
            self.bridge.on_reconnected()
            await owner
            await asyncio.wrap_future(self.channel.scheduled[-1])

        task = self.bridge.last_tasks["chat-starting"]
        self.assertEqual(task["status"], "interrupted")
        self.assertEqual(task["prompt"], "do not lose this")

    async def test_turn_start_timeout_is_interrupted_without_retry(self):
        client = FakeClient()
        client.thread = HangingTurnStartThread()
        self.bridge.effective_service_tier = AsyncMock(return_value="default")
        message = SimpleNamespace(
            id="message-timeout",
            message_id="message-timeout",
            chat_id="chat-timeout",
            chat_type="p2p",
            sender_id="user-1",
            body_text="perform side effect",
            content_text="perform side effect",
        )

        with patch.object(bridge_module, "AsyncCodex", return_value=client):
            await self.bridge.on_message(message)

        task = self.bridge.last_tasks["chat-timeout"]
        self.assertEqual(len(client.thread.turn_calls), 1)
        self.assertEqual(task["status"], "interrupted")
        self.assertEqual(task["interrupted_reason"], "codex_state_unknown")
        self.assertIn("没有自动重试", task["result"])

    async def test_stream_idle_timeout_closes_client_before_unblocking(self):
        client = FakeClient()
        turn = HangingTurn(client)
        state = ActiveTurn(
            task=asyncio.current_task(), chat_id="chat-1", client=client, turn=turn
        )

        with self.assertRaisesRegex(RuntimeError, "没有返回事件"):
            await self.bridge.run_turn(SimpleNamespace(chat_id="chat-1"), turn, state)

        self.assertEqual(client.close_calls, 1)
        self.assertTrue(turn.closed)

    async def test_failed_close_keeps_slot_quarantined(self):
        client = FailingCloseClient()
        owner = asyncio.create_task(asyncio.sleep(0))
        await owner
        state = ActiveTurn(
            task=owner, chat_id="chat-1", client=client, turn=FakeTurn()
        )
        self.bridge.active_turns["chat-1"] = state
        self.bridge.last_tasks["chat-1"] = {"status": "running"}

        await self.bridge.invalidate_active_turn("chat-1", state)

        self.assertIs(self.bridge.active_turns["chat-1"], state)
        self.assertTrue(state.invalidated)

    async def test_reconnect_redelivers_completed_result(self):
        self.bridge.last_tasks["chat-1"] = {
            "status": "completed",
            "cwd": "/tmp/project",
            "result": "finished while offline",
            "delivered": False,
        }

        await self.bridge.notify_reconnected()

        self.assertTrue(self.bridge.last_tasks["chat-1"]["delivered"])
        self.assertIn("现补发结果", self.channel.sent[-1][1])
        self.assertIn("finished while offline", self.channel.sent[-1][1])


if __name__ == "__main__":
    unittest.main()
