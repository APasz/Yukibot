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


if __name__ == "__main__":
    unittest.main()
