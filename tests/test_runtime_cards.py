from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.personal_wechat_bot.conversation.ledger import ConversationLedgerStore
from app.personal_wechat_bot.conversation.ledger_context import LedgerContextAssembler
from app.personal_wechat_bot.domain.models import NormalizedMessage
from app.personal_wechat_bot.persona.runtime_cards import RuntimeCardStore


class RuntimeCardStoreTest(unittest.TestCase):
    def test_data_directory_projection_is_not_imported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            old_root = data_dir / "runtime_cards"
            old_root.mkdir(parents=True)
            (old_root / "state.json").write_text(
                '{"enabled_skill_ids": [], "equipped_persona_id": ""}',
                encoding="utf-8",
            )

            state = RuntimeCardStore(data_dir).state()

            self.assertIn("skill.file_workspace_agent", state["state"]["enabled_skill_ids"])
            self.assertEqual(state["state"]["equipped_persona_id"], "persona.default_wechat_friend")

    def test_defaults_enable_file_workspace_and_dialogue_skills(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = RuntimeCardStore(Path(tmp) / "data")

            state = store.state()
            prompt = "\n".join(store.prompt_lines())

            self.assertEqual(state["status"], "ok")
            self.assertIn("skill.file_workspace_agent", prompt)
            self.assertIn("skill.foreground_dialogue", prompt)
            self.assertIn("persona.default_wechat_friend", prompt)

    def test_save_equip_and_unload_task_card(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = RuntimeCardStore(Path(tmp) / "data")

            saved = store.apply_action(
                "save-task",
                {
                    "name": "长期审计任务",
                    "objective": "持续审计发送前队列",
                    "constraints": "不要泄露内部日志",
                },
            )
            card_id = saved["card"]["card_id"]
            enabled_prompt = "\n".join(store.prompt_lines())
            store.apply_action("unload-task", {"card_id": card_id})
            disabled_prompt = "\n".join(store.prompt_lines())

            self.assertTrue(card_id.startswith("task."))
            self.assertIn("持续审计发送前队列", enabled_prompt)
            self.assertNotIn("持续审计发送前队列", disabled_prompt)

    def test_save_persona_accepts_tavern_style_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = RuntimeCardStore(Path(tmp) / "data")

            store.apply_action(
                "save-persona",
                {
                    "name": "分析型伙伴",
                    "description": "稳定、清醒、直接",
                    "personality": "温和但会指出风险",
                    "scenario": "微信前台协作",
                },
            )
            prompt = "\n".join(store.prompt_lines())

            self.assertIn("分析型伙伴", prompt)
            self.assertIn("Description: 稳定、清醒、直接", prompt)
            self.assertIn("Personality: 温和但会指出风险", prompt)

    def test_channel_override_can_customize_persona_and_skills(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = RuntimeCardStore(Path(tmp) / "data")
            saved = store.apply_action(
                "save-persona",
                {"name": "ChannelPersona", "content": "Use a precise channel-specific voice."},
            )
            persona_id = saved["card"]["card_id"]
            store.apply_action("equip-persona", {"card_id": "persona.default_wechat_friend"})

            store.apply_action(
                "set-channel-persona",
                {"conversation_id": "conv-special", "card_id": persona_id},
            )
            store.apply_action(
                "set-channel-skills",
                {"conversation_id": "conv-special", "card_ids": ["skill.foreground_dialogue"]},
            )

            global_prompt = "\n".join(store.prompt_lines())
            channel_prompt = "\n".join(store.prompt_lines(conversation_id="conv-special"))

            self.assertIn("persona.default_wechat_friend", global_prompt)
            self.assertIn("skill.file_workspace_agent", global_prompt)
            self.assertIn("ChannelPersona", channel_prompt)
            self.assertIn("skill.foreground_dialogue", channel_prompt)
            self.assertNotIn("persona.default_wechat_friend", channel_prompt)
            self.assertNotIn("skill.file_workspace_agent", channel_prompt)
            self.assertIn("conv-special", store.state()["state"]["channel_overrides"])

    def test_ledger_context_uses_channel_specific_runtime_cards(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            runtime_cards = RuntimeCardStore(data_dir)
            saved = runtime_cards.apply_action(
                "save-persona",
                {"name": "ConvTwoPersona", "content": "Only conv2 should see this persona."},
            )
            runtime_cards.apply_action("equip-persona", {"card_id": "persona.default_wechat_friend"})
            runtime_cards.apply_action(
                "set-channel-persona",
                {"conversation_id": "conv2", "card_id": saved["card"]["card_id"]},
            )
            ledger = ConversationLedgerStore(data_dir)
            conv1_message = _message("conv1-msg", "conv1 text", session_id="session_default", conversation_id="conv1")
            conv2_message = _message("conv2-msg", "conv2 text", session_id="session_default", conversation_id="conv2")
            ledger.append_message(conv1_message)
            ledger.append_message(conv2_message)

            conv1_rendered = LedgerContextAssembler(ledger, runtime_cards=runtime_cards).build_snapshot(conv1_message).render_for_prompt()
            conv2_rendered = LedgerContextAssembler(ledger, runtime_cards=runtime_cards).build_snapshot(conv2_message).render_for_prompt()

            self.assertNotIn("Only conv2 should see this persona.", conv1_rendered)
            self.assertIn("Only conv2 should see this persona.", conv2_rendered)

    def test_runtime_cards_are_not_session_memory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            runtime_cards = RuntimeCardStore(data_dir)
            runtime_cards.apply_action("save-task", {"name": "固定任务", "content": "长期生效"})
            ledger = ConversationLedgerStore(data_dir)
            old_message = _message("old", "旧会话", session_id="session_old")
            new_message = _message("new", "新会话", session_id="session_new")
            ledger.append_message(old_message)
            ledger.append_message(new_message)

            snapshot = LedgerContextAssembler(ledger, runtime_cards=runtime_cards).build_snapshot(new_message)
            rendered = snapshot.render_for_prompt()

            self.assertIn("Persistent runtime cards", rendered)
            self.assertIn("长期生效", rendered)
            self.assertIn("新会话", rendered)
            self.assertNotIn("旧会话", rendered)


def _message(message_id: str, text: str, *, session_id: str, conversation_id: str = "conv1") -> NormalizedMessage:
    return NormalizedMessage(
        message_id=message_id,
        conversation_id=conversation_id,
        conversation_type="private",
        chat_title="PAGE",
        sender_name="PAGE",
        sender_wechat_id="wxid_page",
        text=text,
        is_self=False,
        received_at="2026-06-30T00:00:00+08:00",
        metadata={"session_id": session_id},
    )


if __name__ == "__main__":
    unittest.main()
