from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from web_dash.client_pack_drafts import ClientPackDraftStore


class ClientPackDraftStoreTests(unittest.TestCase):
    def test_draft_is_shared_across_store_instances_and_can_be_cleared(self) -> None:
        with TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            first = ClientPackDraftStore(directory)
            first.set(
                node_name="Yuki",
                app_name="Minecraft_All_Fabric",
                changelog="  Shared draft.  ",
            )
            first.close()

            second = ClientPackDraftStore(directory)
            self.assertEqual(
                second.get(node_name="yuki", app_name="minecraft_all_fabric"),
                "Shared draft.",
            )
            second.clear(node_name="yuki", app_name="minecraft_all_fabric")
            self.assertIsNone(
                second.get(node_name="yuki", app_name="minecraft_all_fabric")
            )
            second.close()

    def test_empty_draft_is_distinct_from_missing_draft(self) -> None:
        with TemporaryDirectory() as temp_dir:
            store = ClientPackDraftStore(Path(temp_dir))
            store.set(node_name="yuki", app_name="minecraft", changelog="   ")

            self.assertEqual(store.get(node_name="yuki", app_name="minecraft"), "")
            self.assertIsNone(store.get(node_name="yuki", app_name="other"))
            store.close()


if __name__ == "__main__":
    unittest.main()
