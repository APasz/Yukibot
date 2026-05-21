from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from _security import Access_Control, Power_Level


class SecurityTests(unittest.TestCase):
    def test_power_level_order_includes_admin_between_user_and_sudo(self) -> None:
        self.assertLess(Power_Level.user, Power_Level.admin)
        self.assertLess(Power_Level.admin, Power_Level.sudo)
        self.assertLess(Power_Level.sudo, Power_Level.root)

    def test_access_control_accepts_plural_role_aliases(self) -> None:
        with TemporaryDirectory() as tmp:
            pointer = Path(tmp) / "users.json"
            pointer.write_text(json.dumps({"USERS": [1], "ADMINS": [2], "SUDOERS": [3], "ROOTS": [4]}))
            acl = Access_Control(pointer)

        self.assertEqual(acl.level_of(1), Power_Level.user)
        self.assertEqual(acl.level_of(2), Power_Level.admin)
        self.assertEqual(acl.level_of(3), Power_Level.sudo)
        self.assertEqual(acl.level_of(4), Power_Level.root)

    def test_access_control_preserves_legacy_numeric_levels(self) -> None:
        with TemporaryDirectory() as tmp:
            pointer = Path(tmp) / "users.json"
            pointer.write_text(json.dumps({"2": [20], "3": [30]}))
            acl = Access_Control(pointer)

        self.assertEqual(acl.level_of(20), Power_Level.sudo)
        self.assertEqual(acl.level_of(30), Power_Level.root)

    def test_admin_can_only_promote_and_demote_up_to_user(self) -> None:
        with TemporaryDirectory() as tmp:
            pointer = Path(tmp) / "users.json"
            pointer.write_text(json.dumps({"admin": [100], "user": [200]}))
            acl = Access_Control(pointer)

            promoted = acl.promote(100, 300)
            self.assertEqual(promoted, Power_Level.user)
            self.assertEqual(acl.level_of(300), Power_Level.user)

            with self.assertRaises(PermissionError):
                acl.promote(100, 200)

            demoted = acl.demote(100, 300)
            self.assertEqual(demoted, Power_Level.guest)
            self.assertEqual(acl.level_of(300), Power_Level.guest)

            payload = json.loads(pointer.read_text())
            self.assertEqual(payload["user"], [200])
            self.assertEqual(payload["admin"], [100])
            self.assertEqual(payload["sudo"], [])
            self.assertEqual(payload["root"], [])

    def test_sudo_can_only_promote_and_demote_up_to_admin(self) -> None:
        with TemporaryDirectory() as tmp:
            pointer = Path(tmp) / "users.json"
            pointer.write_text(json.dumps({"sudo": [500], "admin": [400], "user": [300]}))
            acl = Access_Control(pointer)

            promoted = acl.promote(500, 300)
            self.assertEqual(promoted, Power_Level.admin)
            self.assertEqual(acl.level_of(300), Power_Level.admin)

            demoted = acl.demote(500, 400)
            self.assertEqual(demoted, Power_Level.user)
            self.assertEqual(acl.level_of(400), Power_Level.user)

            with self.assertRaises(PermissionError):
                acl.promote(500, 300)

            payload = json.loads(pointer.read_text())
            self.assertEqual(payload["user"], [400])
            self.assertEqual(sorted(payload["admin"]), [300])
            self.assertEqual(payload["sudo"], [500])
            self.assertEqual(payload["root"], [])

    def test_root_can_only_promote_and_demote_up_to_sudo(self) -> None:
        with TemporaryDirectory() as tmp:
            pointer = Path(tmp) / "users.json"
            pointer.write_text(json.dumps({"root": [900], "sudo": [800], "admin": [700]}))
            acl = Access_Control(pointer)

            promoted = acl.promote(900, 700)
            self.assertEqual(promoted, Power_Level.sudo)
            self.assertEqual(acl.level_of(700), Power_Level.sudo)

            demoted = acl.demote(900, 800)
            self.assertEqual(demoted, Power_Level.admin)
            self.assertEqual(acl.level_of(800), Power_Level.admin)

            with self.assertRaises(PermissionError):
                acl.promote(900, 700)

            payload = json.loads(pointer.read_text())
            self.assertEqual(sorted(payload["admin"]), [800])
            self.assertEqual(sorted(payload["sudo"]), [700])
            self.assertEqual(payload["root"], [900])

    def test_sudo_bulk_demote_to_guest_respects_manageable_levels(self) -> None:
        with TemporaryDirectory() as tmp:
            pointer = Path(tmp) / "users.json"
            pointer.write_text(json.dumps({"sudo": [500], "admin": [400], "user": [300], "root": [600]}))
            acl = Access_Control(pointer)

            removed = acl.demote_to_guest_many(500, [300, 400, 500, 600, 700])

            self.assertEqual(removed, (300, 400))
            self.assertEqual(acl.level_of(300), Power_Level.guest)
            self.assertEqual(acl.level_of(400), Power_Level.guest)
            self.assertEqual(acl.level_of(500), Power_Level.sudo)
            self.assertEqual(acl.level_of(600), Power_Level.root)

            payload = json.loads(pointer.read_text())
            self.assertEqual(payload["user"], [])
            self.assertEqual(payload["admin"], [])
            self.assertEqual(payload["sudo"], [500])
            self.assertEqual(payload["root"], [600])


if __name__ == "__main__":
    unittest.main()
