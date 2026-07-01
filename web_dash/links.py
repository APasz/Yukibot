from __future__ import annotations

from urllib.parse import quote

import config


def _required_path_segment(value: str, *, field_name: str) -> str:
    text = value.strip()
    if not text:
        raise ValueError(f"{field_name} must not be blank.")
    return text


def mod_web_node_path(node_name: str) -> str:
    resolved_node_name = _required_path_segment(node_name, field_name="Mod web node name")
    return f"/mod-web/nodes/{quote(resolved_node_name, safe='')}"


def mod_web_node_system_path(node_name: str) -> str:
    return f"{mod_web_node_path(node_name)}/system"


def mod_web_node_app_path(node_name: str, app_name: str) -> str:
    resolved_app_name = _required_path_segment(app_name, field_name="Mod web app name")
    return f"{mod_web_node_path(node_name)}/mods/{quote(resolved_app_name, safe='')}"


def mod_web_node_chat_path(node_name: str, app_name: str) -> str:
    resolved_app_name = _required_path_segment(app_name, field_name="Mod web chat app name")
    return f"{mod_web_node_path(node_name)}/chat/{quote(resolved_app_name, safe='')}"


def current_node_app_url(app_name: str) -> str:
    return f"{config.MOD_WEB_SERVER.public_base_url.rstrip('/')}{mod_web_node_app_path(config.MOD_WEB_SERVER.node_name, app_name)}"


def current_node_chat_url(app_name: str) -> str:
    return f"{config.MOD_WEB_SERVER.public_base_url.rstrip('/')}{mod_web_node_chat_path(config.MOD_WEB_SERVER.node_name, app_name)}"


__all__: tuple[str, ...] = (
    "current_node_app_url",
    "current_node_chat_url",
    "mod_web_node_app_path",
    "mod_web_node_chat_path",
    "mod_web_node_path",
    "mod_web_node_system_path",
)
