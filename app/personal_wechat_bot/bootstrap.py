from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.personal_wechat_bot.config.schema import BotConfig
from app.personal_wechat_bot.agent.tool_orchestrator import ToolTaskOrchestrator
from app.personal_wechat_bot.conversation.channel_store import ConversationChannelStore
from app.personal_wechat_bot.conversation.context_store import ConversationContextStore
from app.personal_wechat_bot.conversation.engine import ConversationEngine
from app.personal_wechat_bot.conversation.ledger import ConversationLedgerStore
from app.personal_wechat_bot.conversation.ledger_context import LedgerContextAssembler
from app.personal_wechat_bot.conversation.link_annotations import LinkAnnotationService
from app.personal_wechat_bot.llm.fake import FakeLLMClient
from app.personal_wechat_bot.llm.key_pool import ApiKeyPool, ConversationKeyAssigner
from app.personal_wechat_bot.llm.model_router import ModelRouter
from app.personal_wechat_bot.llm.openai_client import RelayOpenAIClient
from app.personal_wechat_bot.logging.event_log import EventLogger
from app.personal_wechat_bot.memory.file_index import FileIndex
from app.personal_wechat_bot.normalizer.normalizer import MessageNormalizer
from app.personal_wechat_bot.persona.topic_classifier import AITopicClassifier
from app.personal_wechat_bot.reply_gate.gate import ReplyGate
from app.personal_wechat_bot.reply_gate.confirm_queue import ConfirmQueue
from app.personal_wechat_bot.reply_gate.send_executor import GuardedSendExecutor
from app.personal_wechat_bot.router.deduper import Deduper
from app.personal_wechat_bot.router.cooldown import ConversationCooldown
from app.personal_wechat_bot.router.router import Router
from app.personal_wechat_bot.tools.defaults import register_default_tools
from app.personal_wechat_bot.tools.registry import ToolRegistry
from app.personal_wechat_bot.tools.runtime import ToolRuntime
from app.personal_wechat_bot.wechat_driver.send_driver_factory import build_send_driver
from app.personal_wechat_bot.workspace.file_workspace import FileWorkspace


@dataclass
class BotRuntime:
    config: BotConfig
    normalizer: MessageNormalizer
    router: Router
    topic_classifier: AITopicClassifier
    conversation: ConversationEngine
    cooldown: ConversationCooldown
    reply_gate: ReplyGate
    event_logger: EventLogger
    file_index: FileIndex
    model_router: ModelRouter
    key_pool: ApiKeyPool
    key_assigner: ConversationKeyAssigner
    channel_store: ConversationChannelStore
    context_store: ConversationContextStore
    ledger_store: ConversationLedgerStore
    ledger_context: LedgerContextAssembler
    link_annotations: LinkAnnotationService
    file_workspace: FileWorkspace
    active_driver: object | None = None


def build_runtime(config: BotConfig) -> BotRuntime:
    data_root = Path(config.data_dir)
    event_logger = EventLogger(data_root / "logs.jsonl")
    file_index = FileIndex(data_root / "file_index.sqlite")
    context_store = ConversationContextStore(data_root, max_recent_messages=config.context_window_messages)
    ledger_store = ConversationLedgerStore(data_root)
    ledger_context = LedgerContextAssembler(ledger_store, max_recent_entries=config.context_window_messages + 10)
    file_workspace = FileWorkspace(data_root / "file_workspace")
    model_router = ModelRouter(config.providers)
    chat_provider = model_router.chat_provider().config
    key_pool = ApiKeyPool(chat_provider, data_root)
    key_assigner = ConversationKeyAssigner(key_pool)
    channel_store = ConversationChannelStore(
        data_root,
        key_pool,
        file_workspace_root=data_root / "file_workspace",
        context_root=data_root / "conversation_context",
    )
    llm = (
        RelayOpenAIClient(chat_provider, key_pool=key_pool, channel_store=channel_store)
        if chat_provider.base_url
        else FakeLLMClient(model=chat_provider.model)
    )
    registry = ToolRegistry()
    register_default_tools(registry, data_root=data_root, config=config, file_index=file_index)
    tools = ToolRuntime(registry, event_logger)
    link_annotations = LinkAnnotationService(ledger_store, tools)
    tool_orchestrator = ToolTaskOrchestrator(data_root, max_parallel=2)
    send_driver = build_send_driver(config)
    return BotRuntime(
        config=config,
        normalizer=MessageNormalizer(),
        router=Router(config, Deduper(data_root / "processed_messages.sqlite"), channel_store=channel_store),
        topic_classifier=AITopicClassifier(llm=llm, config=config),
        conversation=ConversationEngine(
            config=config,
            llm=llm,
            tools=tools,
            tool_orchestrator=tool_orchestrator,
            context_store=context_store,
            ledger_context=ledger_context,
        ),
        cooldown=ConversationCooldown(config.group_cooldown_seconds, data_root / "conversation_cooldowns.sqlite"),
        reply_gate=ReplyGate(
            mode=config.mode,
            confirm_queue=ConfirmQueue(data_root / "confirm_queue.jsonl"),
            auto_executor=GuardedSendExecutor(config, send_driver),
        ),
        event_logger=event_logger,
        file_index=file_index,
        model_router=model_router,
        key_pool=key_pool,
        key_assigner=key_assigner,
        channel_store=channel_store,
        context_store=context_store,
        ledger_store=ledger_store,
        ledger_context=ledger_context,
        link_annotations=link_annotations,
        file_workspace=file_workspace,
    )
