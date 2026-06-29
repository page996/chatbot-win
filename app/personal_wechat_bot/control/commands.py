from __future__ import annotations

from app.personal_wechat_bot.config.loader import (
    accept_contact,
    accept_group,
    add_contact,
    add_group,
    create_default_config,
    rename_group,
    set_chat_provider,
    set_deepseek_provider,
)


def init_config(data_dir: str) -> None:
    create_default_config(data_dir)


def accept_contact_channel(data_dir: str, wechat_id: str) -> None:
    accept_contact(data_dir, wechat_id)


def accept_group_channel(data_dir: str, group_name: str) -> None:
    accept_group(data_dir, group_name)


def whitelist_contact(data_dir: str, wechat_id: str) -> None:
    add_contact(data_dir, wechat_id)


def whitelist_group(data_dir: str, group_name: str) -> None:
    add_group(data_dir, group_name)


def change_group_name(data_dir: str, old_name: str, new_name: str) -> None:
    rename_group(data_dir, old_name, new_name)


def set_chat_api(
    data_dir: str,
    base_url: str,
    model: str = "gpt-5.5",
    api_key_env: str = "OPENAI_API_KEY",
    max_wait_seconds: int | None = None,
) -> None:
    set_chat_provider(data_dir, base_url, model=model, api_key_env=api_key_env, max_wait_seconds=max_wait_seconds)


def set_deepseek_api(
    data_dir: str,
    base_url: str = "https://api.deepseek.com",
    model: str = "deepseek-v4-flash",
    api_key_env: str = "DEEPSEEK_API_KEY",
    max_wait_seconds: int | None = 60,
) -> None:
    set_deepseek_provider(
        data_dir,
        base_url=base_url,
        model=model,
        api_key_env=api_key_env,
        max_wait_seconds=max_wait_seconds,
    )
