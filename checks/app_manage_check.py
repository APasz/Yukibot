from __future__ import annotations

import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast
import unittest
from unittest.mock import patch

import hikari

import config
from _discord import App_Bound, DC_Relay
from _manager import AppInstanceCreateRequest, App_Manager
from apps._app import AM_Receiver, App, ChatRelaySupport
from apps._config import App_Config, RelayChannelSource
from cmd_app import AppManageCapability, _app_capabilities, _app_extra_capability_labels, _app_started_response_text
from cmd_dashboard import DashboardEditorService


class _RunningProcess:
    def poll(self) -> None:
        return None


class _DummyReceiver(AM_Receiver):
    async def send(self, payload: App_Bound) -> None:
        return None


class _DummyApp(App):
    async def start(self) -> bool:
        return True

    async def stop(self) -> bool:
        return True


def _build_dummy_app(
    *,
    chat_relay_outbound: bool = False,
    has_receiver: bool = False,
    join_host: str = "play.example.com",
    join_port: int | None = None,
) -> _DummyApp:
    app = object.__new__(_DummyApp)
    app.name = "dummy"
    app.friendly = "Dummy"
    app.directory = Path(".")
    app.updater = None
    app.mods = None
    app.settings = None
    app.chat_channel = None
    app.chat_channel_override = None
    app.chat_channel_source = RelayChannelSource.NONE
    app.chat_relay_outbound = chat_relay_outbound
    app.am_receiver = _DummyReceiver() if has_receiver else None
    app.cfg = App_Config(
        name="dummy",
        instance_key="alpha",
        friendly_name="Dummy",
        directory=Path("."),
        apps_dir=Path("."),
        scope="dummy",
        join_host=join_host,
        join_port=join_port,
    )
    app.process = None
    return app


class AppManageTests(unittest.TestCase):
    def test_chat_relay_support_is_inbound_when_receiver_is_present(self) -> None:
        app = _build_dummy_app(has_receiver=True)

        self.assertIs(app.chat_relay_support, ChatRelaySupport.INBOUND)
        self.assertTrue(app.supports_chat_relay)
        self.assertTrue(app.supports_inbound_chat_relay)
        self.assertTrue(app.supports_relay_system_notices)
        self.assertFalse(app.supports_outbound_chat_relay)

    def test_chat_relay_support_is_outbound_when_app_emits_without_receiver(self) -> None:
        app = _build_dummy_app(chat_relay_outbound=True)

        self.assertIs(app.chat_relay_support, ChatRelaySupport.OUTBOUND)
        self.assertTrue(app.supports_chat_relay)
        self.assertFalse(app.supports_inbound_chat_relay)
        self.assertTrue(app.supports_outbound_chat_relay)

    def test_chat_relay_support_is_bidirectional_when_app_supports_both(self) -> None:
        app = _build_dummy_app(chat_relay_outbound=True, has_receiver=True)

        self.assertIs(app.chat_relay_support, ChatRelaySupport.BIDIRECTIONAL)

    def test_app_capabilities_surface_inbound_chat_label(self) -> None:
        app = _build_dummy_app(has_receiver=True)

        capabilities = _app_capabilities(app)
        labels = _app_extra_capability_labels(app)

        self.assertIn(AppManageCapability.CHAT, capabilities)
        self.assertIn("Chat [In]", labels)
        self.assertNotIn("Toggle", labels)

    def test_started_message_includes_join_address(self) -> None:
        app = _build_dummy_app(join_port=25565)

        self.assertEqual(_app_started_response_text(app), "Dummy Started!\nJoin: `play.example.com:25565`")

    def test_started_message_includes_public_ip_fallback_for_default_public_addr(self) -> None:
        with (
            patch.object(config, "PUBLIC_ADDR", "wakusei.apasz.com"),
            patch.object(config, "PUBLIC_IP", "203.0.113.10"),
        ):
            app = _build_dummy_app(join_host="wakusei.apasz.com", join_port=25565)
            self.assertEqual(
                _app_started_response_text(app),
                "Dummy Started!\nJoin: `wakusei.apasz.com:25565 [203.0.113.10:25565]`",
            )

    def test_dashboard_services_include_join_address_for_running_app(self) -> None:
        manager = object.__new__(App_Manager)
        app = _build_dummy_app(join_port=25565)
        app.process = cast(Any, _RunningProcess())
        manager.activity_manager = None
        manager.apps = {app.name: app}
        manager.current = app.name

        lines = DashboardEditorService._service_lines(manager)

        self.assertIn("join address: play.example.com:25565", lines)

    def test_manager_rejects_chat_channel_updates_for_unsupported_apps(self) -> None:
        manager = object.__new__(App_Manager)
        app = _build_dummy_app()

        with self.assertRaisesRegex(ValueError, "does not support chat relay"):
            manager.set_app_chat_channel(app, hikari.Snowflake(123))

    def test_bind_app_channel_skips_unsupported_apps(self) -> None:
        app = _build_dummy_app()
        app.chat_channel = hikari.Snowflake(456)
        DC_Relay._chat_channels.clear()

        try:
            DC_Relay.bind_app_channel(app)

            self.assertNotIn(app.chat_channel, DC_Relay._chat_channels)
        finally:
            DC_Relay._chat_channels.clear()

    def test_bind_app_channel_skips_outbound_only_apps(self) -> None:
        app = _build_dummy_app(chat_relay_outbound=True)
        app.chat_channel = hikari.Snowflake(789)
        DC_Relay._chat_channels.clear()

        try:
            DC_Relay.bind_app_channel(app)

            self.assertNotIn(app.chat_channel, DC_Relay._chat_channels)
        finally:
            DC_Relay._chat_channels.clear()

    def test_bind_app_channel_registers_inbound_apps(self) -> None:
        app = _build_dummy_app(has_receiver=True)
        app.chat_channel = hikari.Snowflake(789)
        DC_Relay._chat_channels.clear()

        try:
            DC_Relay.bind_app_channel(app)

            self.assertEqual(DC_Relay._chat_channels[app.chat_channel], {cast(App, app)})
        finally:
            DC_Relay._chat_channels.clear()

    def test_apply_relay_channel_purges_unsupported_override(self) -> None:
        manager = object.__new__(App_Manager)
        app = _build_dummy_app()
        with TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            instances_path = temp_path / "instances.json"
            instances_path.write_text(json.dumps({"alpha": {"chat_channel": "123"}}), encoding="utf-8")
            app.cfg = App_Config(
                name="dummy_alpha",
                instance_key="alpha",
                friendly_name="Dummy",
                directory=temp_path,
                apps_dir=temp_path,
                scope="dummy",
            )
            app.chat_channel = hikari.Snowflake(123)
            app.chat_channel_override = hikari.Snowflake(123)
            app.chat_channel_source = RelayChannelSource.INSTANCE
            DC_Relay._chat_channels.clear()

            try:
                manager._apply_relay_channel(app)
            finally:
                DC_Relay._chat_channels.clear()

            payload = json.loads(instances_path.read_text(encoding="utf-8"))
            self.assertNotIn("chat_channel", payload["alpha"])
            self.assertIsNone(app.chat_channel)
            self.assertIsNone(app.chat_channel_override)
            self.assertIs(app.chat_channel_source, RelayChannelSource.NONE)

    def test_create_instance_writes_new_entry_from_template(self) -> None:
        manager = object.__new__(App_Manager)
        original_cwd = Path.cwd()
        with TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            scope_path = temp_path / "apps" / "demo"
            scope_path.mkdir(parents=True)
            instances_path = scope_path / "instances.json"
            instances_path.write_text(
                json.dumps(
                    {
                        "alpha": {
                            "friendly_name": "Demo Alpha",
                            "directory": "{APPS}/demo-alpha",
                            "server_log_file": "{WD}/Server.log",
                            "port": 12345,
                        }
                    }
                ),
                encoding="utf-8",
            )

            os.chdir(temp_path)
            try:
                instance_name = manager.create_instance(
                    AppInstanceCreateRequest(
                        scope="demo",
                        instance_key="beta",
                        friendly_name="Demo Beta",
                        subfolder="demo-beta",
                        port=23456,
                        server_log_file="{WD}/logs/server.log",
                    )
                )
            finally:
                os.chdir(original_cwd)

            payload = json.loads(instances_path.read_text(encoding="utf-8"))
            self.assertEqual(instance_name, "demo_beta")
            self.assertEqual(payload["beta"]["friendly_name"], "Demo Beta")
            self.assertEqual(payload["beta"]["directory"], "{APPS}/demo-beta")
            self.assertEqual(payload["beta"]["server_log_file"], "{WD}/logs/server.log")
            self.assertEqual(payload["beta"]["join_port"], 23456)

    def test_create_instance_rejects_subfolder_escape(self) -> None:
        manager = object.__new__(App_Manager)
        original_cwd = Path.cwd()
        with TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            scope_path = temp_path / "apps" / "demo"
            scope_path.mkdir(parents=True)
            instances_path = scope_path / "instances.json"
            instances_path.write_text(
                json.dumps({"alpha": {"friendly_name": "Demo Alpha", "directory": "{APPS}/demo-alpha"}}),
                encoding="utf-8",
            )

            os.chdir(temp_path)
            try:
                with self.assertRaisesRegex(ValueError, "DIR_APP|within DIR_APP|stay within DIR_APP"):
                    manager.create_instance(
                        AppInstanceCreateRequest(
                            scope="demo",
                            instance_key="beta",
                            friendly_name="Demo Beta",
                            subfolder="../escape",
                        )
                    )
            finally:
                os.chdir(original_cwd)

            payload = json.loads(instances_path.read_text(encoding="utf-8"))
            self.assertEqual(tuple(payload.keys()), ("alpha",))


if __name__ == "__main__":
    unittest.main()
