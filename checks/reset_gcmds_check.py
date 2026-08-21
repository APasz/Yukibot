import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from reset_gcmds import load_reset_token


class ResetGcmdsTokenTests(unittest.TestCase):
    def test_load_reset_token_returns_direct_token(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            token_file = Path(temporary_directory) / "token.reset"
            token_file.write_text(" direct-token\n", encoding="utf-8")

            self.assertEqual(load_reset_token(token_file), "direct-token")

    def test_load_reset_token_resolves_supported_environment_variable(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            token_file = Path(temporary_directory) / "token.reset"
            for variable_name in ("BOT_TOKEN", "YUKI_BOT_TOKEN", "ERIN_BOT_TOKEN"):
                with self.subTest(variable_name=variable_name):
                    token_file.write_text(variable_name, encoding="utf-8")
                    with patch("reset_gcmds.config.env_req", return_value="configured-token") as env_req:
                        self.assertEqual(load_reset_token(token_file), "configured-token")
                    env_req.assert_called_once_with(variable_name)

    def test_load_reset_token_rejects_empty_file(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            token_file = Path(temporary_directory) / "token.reset"
            token_file.write_text(" \n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "must contain a bot token"):
                load_reset_token(token_file)
