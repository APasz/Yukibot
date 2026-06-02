from __future__ import annotations

import unittest

from node_auth import NodeAccessGrant, NodeApiScope, NodeTokenError, issue_node_token, verify_node_token


class NodeAuthTests(unittest.TestCase):
    def test_issue_and_verify_node_token(self) -> None:
        token = issue_node_token(
            secret="secret",
            grant=NodeAccessGrant(
                subject="123",
                node="erin",
                app="minecraft_alpha",
                scopes=frozenset({NodeApiScope.MODS_READ, NodeApiScope.MODS_DOWNLOAD}),
                expires_at=200,
            ),
        )

        grant = verify_node_token(
            secret="secret",
            token=token,
            node="erin",
            app="minecraft_alpha",
            required_scopes=(NodeApiScope.MODS_DOWNLOAD,),
            now=100,
        )

        self.assertEqual(grant.subject, "123")
        self.assertEqual(grant.node, "erin")
        self.assertEqual(grant.app, "minecraft_alpha")

    def test_verify_rejects_wrong_app(self) -> None:
        token = issue_node_token(
            secret="secret",
            grant=NodeAccessGrant(
                subject="123",
                node="erin",
                app="minecraft_alpha",
                scopes=frozenset({NodeApiScope.MODS_DOWNLOAD}),
                expires_at=200,
            ),
        )

        with self.assertRaisesRegex(NodeTokenError, "different app"):
            verify_node_token(
                secret="secret",
                token=token,
                node="erin",
                app="sevendays_alpha",
                required_scopes=(NodeApiScope.MODS_DOWNLOAD,),
                now=100,
            )

    def test_verify_rejects_missing_scope(self) -> None:
        token = issue_node_token(
            secret="secret",
            grant=NodeAccessGrant(
                subject="123",
                node="erin",
                app="minecraft_alpha",
                scopes=frozenset({NodeApiScope.MODS_READ}),
                expires_at=200,
            ),
        )

        with self.assertRaisesRegex(NodeTokenError, "missing required scopes"):
            verify_node_token(
                secret="secret",
                token=token,
                node="erin",
                app="minecraft_alpha",
                required_scopes=(NodeApiScope.MODS_DOWNLOAD,),
                now=100,
            )

    def test_verify_rejects_expired_token(self) -> None:
        token = issue_node_token(
            secret="secret",
            grant=NodeAccessGrant(
                subject="123",
                node="erin",
                app="minecraft_alpha",
                scopes=frozenset({NodeApiScope.MODS_READ}),
                expires_at=100,
            ),
        )

        with self.assertRaisesRegex(NodeTokenError, "expired"):
            verify_node_token(
                secret="secret",
                token=token,
                node="erin",
                app="minecraft_alpha",
                required_scopes=(NodeApiScope.MODS_READ,),
                now=100,
            )


if __name__ == "__main__":
    unittest.main()
