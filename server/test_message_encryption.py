import base64
import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(__file__))

from message_encryption import MessageEncryptor
from message_system import MessageSystem


class FakeDatabase:
    def __init__(self):
        self.wrapped_key = None
        self.insert_values = None

    def run_query(self, query, values):
        self.last_query = query
        if query.startswith("SELECT encryption_keys.id"):
            if self.wrapped_key is None:
                return SimpleNamespace(returncode=0, stdout="")
            return SimpleNamespace(returncode=0, stdout=f"7|{self.wrapped_key}\n")
        if query.startswith("INSERT INTO encryption_keys"):
            self.wrapped_key = values["wrapped_key"]
            return SimpleNamespace(returncode=0, stdout="7\n")
        if query.startswith("INSERT INTO messages"):
            self.insert_values = values
            return SimpleNamespace(returncode=0, stdout="42\n")
        if query.startswith("SELECT sender.username"):
            return SimpleNamespace(returncode=0, stdout=self.conversation_output)
        return SimpleNamespace(returncode=1, stdout="")


class MessageEncryptionTests(unittest.TestCase):
    def setUp(self):
        self.environment = {
            "BLUEBUBBLES_MESSAGE_MASTER_KEY": base64.b64encode(os.urandom(32)).decode("ascii")
        }
        self.database = FakeDatabase()
        self.database.conversation_output = ""
        self.messages = MessageSystem(
            self.database.run_query, MessageEncryptor(self.environment)
        )

    def test_new_message_insert_has_no_plaintext_and_can_be_decrypted(self):
        original_content = "Private message | with unicode: £"

        self.assertTrue(self.messages.send("alice", "bob", original_content))
        self.assertNotIn("content", self.database.insert_values)
        self.assertNotIn(original_content, self.database.insert_values.values())
        self.assertEqual(self.database.insert_values["encryption_key_id"], "7")

        data_key = self.messages.key_store.unwrap(self.database.wrapped_key)
        decrypted_content = self.messages.encryptor.decrypt_message(
            base64.b64decode(self.database.insert_values["ciphertext"]),
            base64.b64decode(self.database.insert_values["nonce"]),
            data_key,
        )
        self.assertEqual(decrypted_content, original_content)

    def test_new_message_keeps_legacy_content_column_non_secret(self):
        self.assertTrue(self.messages.send("alice", "bob", "Private text"))
        self.assertIn("recipient.\"userID\", '', CURRENT_TIMESTAMP", self.database.last_query)

    def test_conversation_decrypts_new_rows_and_reads_legacy_rows(self):
        self.assertTrue(self.messages.send("alice", "bob", "Encrypted text"))
        encrypted_row = "|".join(
            (
                "alice",
                "",
                "18/09/2026",
                "12:00",
                "7",
                self.database.insert_values["ciphertext"],
                self.database.insert_values["nonce"],
                self.database.wrapped_key,
            )
        )
        legacy_row = "|".join(
            (
                "bob",
                base64.b64encode("Older text".encode("utf-8")).decode("ascii"),
                "17/09/2026",
                "11:00",
                "",
                "",
                "",
                "",
            )
        )
        self.database.conversation_output = f"{encrypted_row}\n{legacy_row}\n"

        conversation = self.messages.conversation("alice", "bob")

        self.assertEqual([row["content"] for row in conversation], ["Encrypted text", "Older text"])

    def test_each_new_message_uses_a_fresh_nonce(self):
        self.assertTrue(self.messages.send("alice", "bob", "First message"))
        first_nonce = self.database.insert_values["nonce"]

        self.assertTrue(self.messages.send("alice", "bob", "Second message"))

        self.assertNotEqual(first_nonce, self.database.insert_values["nonce"])


if __name__ == "__main__":
    unittest.main()
