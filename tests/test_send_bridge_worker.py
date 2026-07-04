from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.personal_wechat_bot.config.loader import create_default_config, load_config
from app.personal_wechat_bot.control.send_commands import send_approved_confirm_item, set_send_controls
from app.personal_wechat_bot.conversation.ledger import ConversationLedgerStore
from app.personal_wechat_bot.domain.models import ReplyCandidate
from app.personal_wechat_bot.reply_gate.confirm_queue import ConfirmQueue
from app.personal_wechat_bot.reply_gate.send_executor import GuardedSendExecutor
from app.personal_wechat_bot.runtime.send_bridge_worker import BridgeWorker, run_bridge_worker
from app.personal_wechat_bot.wechat_driver.bridge_send import BridgeOutboxSendDriver, BridgeOutboxStore
from app.personal_wechat_bot.wechat_driver.send_backends import DryRunSendBackend, SendOutcome


class _FlakyBackend:
    """Fails the first N attempts for a receiver, then succeeds."""

    name = "flaky"

    def __init__(self, fail_times: int) -> None:
        self.fail_times = fail_times
        self.attempts = 0

    def health_check(self) -> bool:
        return True

    def send_text(self, receiver: str, text: str) -> SendOutcome:
        self.attempts += 1
        if self.attempts <= self.fail_times:
            return SendOutcome.failure("transient")
        return SendOutcome.success("ok")

    def send_file(self, receiver: str, path: str, caption: str = "") -> SendOutcome:
        return SendOutcome.success("ok")

    def close(self) -> None:
        return None


class SendBridgeWorkerTest(unittest.TestCase):
    def test_worker_delivers_pending_text_and_acks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            store = BridgeOutboxStore(data_dir)
            record = store.enqueue("wxid_a", "hello there")

            backend = DryRunSendBackend()
            worker = BridgeWorker(data_dir, backend)
            processed = worker.run_once()

            self.assertEqual(processed, 1)
            self.assertEqual(backend.sent_texts, [("wxid_a", "hello there")])
            state = store.state(limit=10)
            self.assertEqual(state["pending_count"], 0)
            self.assertEqual(state["items"][0]["status"], "sent")
            self.assertEqual(state["items"][0]["bridge_id"], record["bridge_id"])

    def test_worker_prefers_record_receiver_over_conversation_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            store = BridgeOutboxStore(data_dir)
            store.enqueue("hashed-conversation-id", "hello there", receiver="wxid_real_alice")

            backend = DryRunSendBackend()
            BridgeWorker(data_dir, backend).run_once()

            self.assertEqual(backend.sent_texts, [("wxid_real_alice", "hello there")])

    def test_worker_is_restart_safe_and_does_not_resend(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            store = BridgeOutboxStore(data_dir)
            store.enqueue("wxid_a", "one")

            backend1 = DryRunSendBackend()
            BridgeWorker(data_dir, backend1).run_once()
            self.assertEqual(len(backend1.sent_texts), 1)

            # A fresh worker (simulating a restart) must not re-send the acked record.
            backend2 = DryRunSendBackend()
            processed = BridgeWorker(data_dir, backend2).run_once()
            self.assertEqual(processed, 0)
            self.assertEqual(backend2.sent_texts, [])

    def test_worker_retries_then_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            store = BridgeOutboxStore(data_dir)
            store.enqueue("wxid_a", "retry me")

            backend = _FlakyBackend(fail_times=2)
            worker = BridgeWorker(data_dir, backend, max_send_attempts=3)
            worker.run_once()

            self.assertEqual(backend.attempts, 3)
            state = store.state(limit=10)
            self.assertEqual(state["items"][0]["status"], "sent")

    def test_worker_marks_failed_when_all_attempts_fail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            store = BridgeOutboxStore(data_dir)
            store.enqueue("wxid_a", "doomed")

            backend = _FlakyBackend(fail_times=99)
            worker = BridgeWorker(data_dir, backend, max_send_attempts=2)
            worker.run_once()

            state = store.state(limit=10)
            self.assertEqual(state["items"][0]["status"], "failed")
            self.assertEqual(worker.stats.failed, 1)

    def test_worker_fails_missing_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            store = BridgeOutboxStore(data_dir)
            store.enqueue_file("wxid_a", str(data_dir / "does_not_exist.pdf"))

            worker = BridgeWorker(data_dir, DryRunSendBackend())
            worker.run_once()

            state = store.state(limit=10)
            self.assertEqual(state["items"][0]["status"], "failed")
            self.assertIn("file_not_found", state["items"][0]["ack"]["reason"])

    def test_worker_delivers_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            target = data_dir / "tool_outputs" / "out.docx"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"docx bytes")
            store = BridgeOutboxStore(data_dir)
            store.enqueue_file("wxid_a", str(target), caption="here")

            backend = DryRunSendBackend()
            BridgeWorker(data_dir, backend).run_once()

            self.assertEqual(backend.sent_files, [("wxid_a", str(target), "here")])
            self.assertEqual(store.state(limit=10)["items"][0]["status"], "sent")

    def test_executor_file_send_queues_file_to_bridge_outbox(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            target = data_dir / "tool_outputs" / "reply.docx"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"docx")
            set_send_controls(data_dir, enabled=True, driver="bridge_outbox")
            driver = BridgeOutboxSendDriver(send_enabled=True, data_dir=data_dir)
            config = load_config(data_dir)
            reply = ReplyCandidate(
                message_id="m-1",
                conversation_id="wxid_a",
                text="here is the doc",
                send_mode="confirm",
                model="fake",
                attachments=[{"path": str(target), "name": "reply.docx", "status": "indexed"}],
            )

            GuardedSendExecutor(config, driver).execute_confirmed(reply)

            backend = DryRunSendBackend()
            BridgeWorker(data_dir, backend).run_once()

            self.assertEqual(backend.sent_texts, [("wxid_a", "here is the doc")])
            self.assertEqual(backend.sent_files, [("wxid_a", str(target), "")])

    def test_retryable_failure_stays_pending_across_ticks(self) -> None:
        # A transient backend failure must NOT write a terminal ack; the record
        # stays pending so a later tick retries it (no silent drop).
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            store = BridgeOutboxStore(data_dir)
            store.enqueue("wxid_a", "deliver me eventually")

            class _DownThenUp:
                name = "downthenup"

                def __init__(self) -> None:
                    self.calls = 0

                def health_check(self) -> bool:
                    return True

                def send_text(self, receiver: str, text: str) -> SendOutcome:
                    self.calls += 1
                    if self.calls == 1:
                        return SendOutcome.failure("wcf_unavailable")
                    return SendOutcome.success("ok")

                def send_file(self, receiver: str, path: str, caption: str = "") -> SendOutcome:
                    return SendOutcome.success("ok")

                def close(self) -> None:
                    return None

            backend = _DownThenUp()
            worker = BridgeWorker(data_dir, backend, max_send_attempts=1)

            worker.run_once()  # tick 1: backend down -> retry, stays pending
            state_after_1 = store.state(limit=10)
            self.assertEqual(state_after_1["items"][0]["status"], "retry")
            self.assertEqual(state_after_1["pending_count"], 1)

            worker.run_once()  # tick 2: backend up -> sent
            state_after_2 = store.state(limit=10)
            self.assertEqual(state_after_2["items"][0]["status"], "sent")
            self.assertEqual(state_after_2["pending_count"], 0)

    def test_retryable_failure_becomes_terminal_after_cap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            store = BridgeOutboxStore(data_dir)
            store.enqueue("wxid_a", "never lands")

            class _AlwaysDown:
                name = "down"

                def health_check(self) -> bool:
                    return True

                def send_text(self, receiver: str, text: str) -> SendOutcome:
                    return SendOutcome.failure("wcf_connect_failed")

                def send_file(self, receiver: str, path: str, caption: str = "") -> SendOutcome:
                    return SendOutcome.failure("wcf_connect_failed")

                def close(self) -> None:
                    return None

            worker = BridgeWorker(data_dir, _AlwaysDown(), max_send_attempts=1)
            # Run more ticks than the cross-tick retry cap; must end terminal.
            for _ in range(20):
                if store.state(limit=10)["items"][0]["status"] == "failed":
                    break
                worker.run_once()

            final = store.state(limit=10)["items"][0]
            self.assertEqual(final["status"], "failed")
            self.assertIn("retries_exhausted", final["ack"]["reason"])

    def test_poison_record_is_quarantined_not_crashing_worker(self) -> None:
        # A backend that raises must not crash the whole worker; the record is
        # quarantined with a terminal ack so the queue advances.
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            store = BridgeOutboxStore(data_dir)
            store.enqueue("wxid_poison", "boom")
            store.enqueue("wxid_ok", "fine")

            class _RaiseForPoison:
                name = "raiser"

                def health_check(self) -> bool:
                    return True

                def send_text(self, receiver: str, text: str) -> SendOutcome:
                    if receiver == "wxid_poison":
                        raise ValueError("embedded null or similar")
                    return SendOutcome.success("ok")

                def send_file(self, receiver: str, path: str, caption: str = "") -> SendOutcome:
                    return SendOutcome.success("ok")

                def close(self) -> None:
                    return None

            processed = BridgeWorker(data_dir, _RaiseForPoison()).run_once()

            self.assertEqual(processed, 2)
            items = {i["conversation_id"]: i for i in store.state(limit=10)["items"]}
            self.assertEqual(items["wxid_poison"]["status"], "failed")
            self.assertIn("deliver_exception", items["wxid_poison"]["ack"]["reason"])
            self.assertEqual(items["wxid_ok"]["status"], "sent")

    def test_failed_ack_sync_is_reconciled_on_next_tick(self) -> None:
        # If the ledger/confirm sync fails at ack time, a later tick must re-sync
        # so the ledger eventually reflects delivery.
        with tempfile.TemporaryDirectory() as tmp:
            import app.personal_wechat_bot.runtime.send_bridge_worker as worker_mod

            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            set_send_controls(data_dir, enabled=True, driver="bridge_outbox")
            reply = ReplyCandidate(
                message_id="m-1",
                conversation_id="wxid_a",
                text="reconcile me",
                send_mode="confirm",
                model="fake",
            )
            ledger = ConversationLedgerStore(data_dir)
            ledger.append_reply(reply)
            queue = ConfirmQueue(data_dir / "confirm_queue.jsonl")
            queue_id = queue.enqueue(reply)
            queue.approve(queue_id, reviewer="tester")
            driver = BridgeOutboxSendDriver(send_enabled=True, data_dir=data_dir)
            send_approved_confirm_item(data_dir, queue_id, driver=driver)

            worker = BridgeWorker(data_dir, DryRunSendBackend())

            # Force the first sync to fail (simulating a Windows file lock).
            original = worker_mod.sync_bridge_ack_to_send_state
            calls = {"n": 0}

            def _flaky_sync(*args, **kwargs):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise OSError("state file locked")
                return original(*args, **kwargs)

            worker_mod.sync_bridge_ack_to_send_state = _flaky_sync
            try:
                worker.run_once()  # delivers, ack written, sync fails
                # Ledger not yet flipped because sync failed.
                self.assertNotEqual(ledger.read_entries("wxid_a")[0].send.get("status"), "sent")
                worker.run_once()  # reconcile pass re-syncs the terminal ack
            finally:
                worker_mod.sync_bridge_ack_to_send_state = original

            self.assertEqual(ledger.read_entries("wxid_a")[0].send["status"], "sent")

    def test_full_chain_queue_to_bridge_to_ledger(self) -> None:
        # End-to-end: approve -> bridge_outbox queue -> worker delivers -> ledger sent.
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            create_default_config(data_dir)
            set_send_controls(data_dir, enabled=True, driver="bridge_outbox")
            reply = ReplyCandidate(
                message_id="m-1",
                conversation_id="wxid_a",
                text="chain hello",
                send_mode="confirm",
                model="fake",
            )
            ledger = ConversationLedgerStore(data_dir)
            ledger.append_reply(reply)
            queue = ConfirmQueue(data_dir / "confirm_queue.jsonl")
            queue_id = queue.enqueue(reply)
            queue.approve(queue_id, reviewer="tester")

            driver = BridgeOutboxSendDriver(send_enabled=True, data_dir=data_dir)
            result = send_approved_confirm_item(data_dir, queue_id, driver=driver)
            self.assertEqual(result["status"], "queued_to_bridge")

            stats = run_bridge_worker(data_dir, once=True, lock_enabled=False)
            self.assertEqual(stats.delivered, 1)

            entry = ledger.read_entries("wxid_a")[0]
            self.assertEqual(entry.send["status"], "sent")
            self.assertEqual(queue.get(queue_id)["status"], "sent")


if __name__ == "__main__":
    unittest.main()
