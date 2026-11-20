import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from apps.minecraft import MinecraftServerPropertiesSnapshot, Minecraft_Settings
from apps.minecraft.properties import parse_properties, replace_properties


class MinecraftPropertiesTests(unittest.TestCase):
    def test_values_round_trip_without_creating_additional_properties(self) -> None:
        for value in (
            "Hello\nonline-mode=false", " leading and trailing ", "C:\\server\\", "tabs\tand\rreturns\fformfeed",
            "世界 🙂", "", "colon: equals= hash#", "\x00\x08", "literal \\n and \\u0021",
        ):
            with self.subTest(value=value):
                source = replace_properties("# Retain comments\nonline-mode=true\n", {"motd": value})
                entries = {entry.key: entry.value for entry in parse_properties(source)}
                self.assertEqual(entries, {"online-mode": "true", "motd": value})
                self.assertTrue(source.startswith("# Retain comments\n"))

    def test_parses_java_separators_escapes_and_continued_lines(self) -> None:
        source = (
            "! A comment ending in a backslash\\\r\n"
            "\\\r\n! Comment after an empty continuation\\uBAD\r\n"
            "max-players : 20\r\n"
            "motd = First\\\r\n    Second\\nThird\r\n"
            "escaped\\ key\\: = Value\\ with\\ spaces\r\n"
            "flag\r\n"
            "unicode=\\u4e16\\u754c \\ud83d\\ude42\r\n"
            "backslash=last\\\\\r\n"
        )
        entries = {entry.key: entry.value for entry in parse_properties(source)}
        self.assertEqual(entries, {
            "max-players": "20", "motd": "FirstSecond\nThird", "escaped key:": "Value with spaces",
            "flag": "", "unicode": "世界 🙂", "backslash": "last\\",
        })
        replaced = replace_properties(source, {"motd": "Changed\nAgain"})
        self.assertNotIn("Second", replaced)
        self.assertEqual(
            {entry.key: entry.value for entry in parse_properties(replaced)},
            {**entries, "motd": "Changed\nAgain"},
        )

    def test_rejects_malformed_unicode_escapes(self) -> None:
        for value in (r"\u", r"\u123", r"\uGGGG", r"\uu1234"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "Unicode escape"):
                parse_properties(f"motd={value}")

    def test_property_names_are_case_sensitive(self) -> None:
        source = "MOTD=Custom property\nmotd=Before\n"
        updated = replace_properties(source, {"motd": "After"})
        self.assertEqual(
            {entry.key: entry.value for entry in parse_properties(updated)},
            {"MOTD": "Custom property", "motd": "After"},
        )
        with TemporaryDirectory() as temporary:
            pointer = Path(temporary) / "server.properties"
            pointer.write_text(source)
            settings = Minecraft_Settings(pointer)
            motd = settings.get_setting("motd")
            assert motd is not None
            self.assertEqual(motd.value, "Before")
            motd.update("Saved")
            settings.save()
            self.assertIn("MOTD=Custom property\n", pointer.read_text())
            reloaded = Minecraft_Settings(pointer).get_setting("motd")
            assert reloaded is not None
            self.assertEqual(reloaded.value, "Saved")

    def test_property_names_round_trip_with_java_special_characters(self) -> None:
        values = {"Space in key": "First", "key:=#!\\\n世界": "Second", "": "Empty key"}
        for source in ("# Comment\n", replace_properties("# Comment\n", values)):
            with self.subTest(source=source):
                updated = replace_properties(source, values)
                self.assertEqual({entry.key: entry.value for entry in parse_properties(updated)}, values)

    def test_appended_properties_are_not_swallowed_by_a_trailing_continuation(self) -> None:
        for source in (
            "custom=old\\", "custom=old\\\n", "custom=old\\\n\n", "\\", "\\\n", "\\\n\n", "=\\", ":\\\n",
            "escaped\\ key=value\\",
        ):
            with self.subTest(source=source):
                original = {entry.key: entry.value for entry in parse_properties(source)}
                updated = replace_properties(source, {"motd": "New"})
                self.assertEqual(
                    {entry.key: entry.value for entry in parse_properties(updated)},
                    {**original, "motd": "New"},
                )

    def test_rejects_duplicate_managed_properties_even_with_escaped_keys(self) -> None:
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            replace_properties("motd=One\nmot\\u0064=Two\n", {"motd": "Changed"})

    def test_multiline_motd_cannot_change_other_server_settings(self) -> None:
        with TemporaryDirectory() as temporary:
            pointer = Path(temporary) / "server.properties"
            pointer.write_text("motd=Before\nonline-mode=true\nmax-players=20\n")
            settings = Minecraft_Settings(pointer)
            motd = settings.get_setting("motd")
            assert motd is not None
            message = "Hello\nonline-mode=false\nBackslash\\"
            motd.update(message)

            settings.save()

            properties = {entry.key: entry.value for entry in parse_properties(pointer.read_text())}
            self.assertEqual(properties["online-mode"], "true")
            self.assertEqual(properties["motd"], message)
            reloaded = Minecraft_Settings(pointer).get_setting("motd")
            assert reloaded is not None
            self.assertEqual(reloaded.value, message)

    def test_runtime_snapshot_decodes_escaped_credentials_and_ports(self) -> None:
        with TemporaryDirectory() as temporary:
            pointer = Path(temporary) / "server.properties"
            pointer.write_text("rcon.password=pass\\\\word\\u0021\\u0020\nrcon.port:25575\nenable-rcon true\n")
            snapshot = MinecraftServerPropertiesSnapshot.load(pointer)
            self.assertEqual(snapshot.rcon_password, "pass\\word! ")
            self.assertEqual(snapshot.rcon_port, 25575)
            self.assertIs(snapshot.enable_rcon, True)
