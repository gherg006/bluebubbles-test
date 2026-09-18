import base64
import uuid

from message_encryption import MessageEncryptor


class MessageKeyStore:
    # Stores recipient-scoped data keys wrapped by the server master key.
    def __init__(self, run_query, encryptor):
        self.run_query = run_query
        self.encryptor = encryptor

    def get_or_create(self, recipient):
        # Reuse the recipient's latest active key or make one before saving a message.
        scope_id = str(
            uuid.uuid5(uuid.NAMESPACE_URL, f"bluebubbles:message-recipient:{recipient}")
        )
        result = self.run_query(
            "SELECT encryption_keys.id, "
            "replace(encode(encryption_keys.wrapped_key, 'base64'), E'\\n', '') "
            "FROM encryption_keys "
            "WHERE encryption_keys.scope_type = 'user' "
            "AND encryption_keys.scope_id = :'scope_id' "
            "AND encryption_keys.status = 'active' "
            "ORDER BY encryption_keys.key_version DESC, encryption_keys.id DESC LIMIT 1;",
            {"recipient": recipient, "scope_id": scope_id},
        )
        if result.returncode != 0:
            return None
        if result.stdout.strip():
            key_id, wrapped_key = result.stdout.strip().split("|", 1)
            return key_id, self.encryptor.unwrap_data_key(
                base64.b64decode(wrapped_key, validate=True)
            )

        data_key = self.encryptor.create_data_key()
        wrapped_key = base64.b64encode(self.encryptor.wrap_data_key(data_key)).decode("ascii")
        result = self.run_query(
            "INSERT INTO encryption_keys "
            "(scope_type, scope_id, wrapped_key, key_version, status, created_at, rotated_at) "
            "SELECT 'user', :'scope_id', decode(:'wrapped_key', 'base64'), "
            "1, 'active', CURRENT_TIMESTAMP, NULL "
            "FROM users recipient WHERE recipient.username = :'recipient' "
            "RETURNING id;",
            {
                "recipient": recipient,
                "scope_id": scope_id,
                "wrapped_key": wrapped_key,
            },
        )
        if result.returncode != 0 or not result.stdout.strip():
            return None
        return result.stdout.strip(), data_key

    def unwrap(self, wrapped_key):
        # Decode database-safe base64 only immediately before AES-GCM key unwrapping.
        return self.encryptor.unwrap_data_key(
            base64.b64decode(wrapped_key, validate=True)
        )


class MessageSystem:
    # Stores encrypted new messages while retaining legacy plaintext message support.
    def __init__(self, run_query, encryptor=None):
        self.run_query = run_query
        self.encryptor = encryptor or MessageEncryptor()
        self.key_store = MessageKeyStore(run_query, self.encryptor)

    def send(self, sender, recipient, content):
        # Encrypt the content before the insert and save no plaintext in message_content.
        try:
            key_record = self.key_store.get_or_create(recipient)
            if key_record is None:
                return False
            encryption_key_id, data_key = key_record
            ciphertext, nonce = self.encryptor.encrypt_message(content, data_key)
        except Exception:
            return False
        result = self.run_query(
            "INSERT INTO messages "
            "(sender_id, recipient_id, message_content, sent_at, body_ciphertext, "
            "encryption_nonce, encryption_key_id, encryption_version, encrypted_at) "
            "SELECT sender.\"userID\", recipient.\"userID\", '', CURRENT_TIMESTAMP, "
            "decode(:'ciphertext', 'base64'), decode(:'nonce', 'base64'), "
            ":'encryption_key_id', 1, CURRENT_TIMESTAMP "
            "FROM users sender JOIN users recipient ON TRUE "
            "WHERE sender.username = :'sender' AND recipient.username = :'recipient' "
            "RETURNING message_id;",
            {
                "sender": sender,
                "recipient": recipient,
                "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
                "nonce": base64.b64encode(nonce).decode("ascii"),
                "encryption_key_id": encryption_key_id,
            },
        )
        return result.returncode == 0 and bool(result.stdout.strip())

    def conversation(self, username, other_user):
        # Load legacy base64 text or encrypted fields without sending sensitive values to logs.
        result = self.run_query(
            "SELECT sender.username, "
            "CASE WHEN messages.encryption_key_id IS NULL "
            "THEN replace(encode(convert_to(messages.message_content, 'UTF8'), 'base64'), E'\\n', '') END, "
            "to_char(messages.sent_at, 'DD/MM/YYYY'), "
            "to_char(messages.sent_at, 'HH24:MI'), "
            "messages.encryption_key_id, "
            "replace(encode(messages.body_ciphertext, 'base64'), E'\\n', ''), "
            "replace(encode(messages.encryption_nonce, 'base64'), E'\\n', ''), "
            "replace(encode(encryption_keys.wrapped_key, 'base64'), E'\\n', '') "
            "FROM messages "
            "JOIN users sender ON sender.\"userID\" = messages.sender_id "
            "JOIN users recipient ON recipient.\"userID\" = messages.recipient_id "
            "LEFT JOIN encryption_keys ON encryption_keys.id = messages.encryption_key_id "
            "WHERE (sender.username = :'username' AND recipient.username = :'other_user') "
            "OR (sender.username = :'other_user' AND recipient.username = :'username') "
            "ORDER BY messages.sent_at, messages.message_id;",
            {"username": username, "other_user": other_user},
        )
        if result.returncode != 0:
            return []

        messages = []
        for row in result.stdout.splitlines():
            (
                sender,
                legacy_content,
                sent_date,
                sent_time,
                encryption_key_id,
                ciphertext,
                nonce,
                wrapped_key,
            ) = row.split("|", 7)
            content = self._message_content(
                legacy_content, encryption_key_id, ciphertext, nonce, wrapped_key
            )
            messages.append(
                {
                    "sender": sender,
                    "content": content,
                    "date": sent_date,
                    "time": sent_time,
                }
            )
        return messages

    def _message_content(self, legacy_content, encryption_key_id, ciphertext, nonce, wrapped_key):
        # Keep pre-encryption rows readable and isolate a corrupted encrypted row.
        if not encryption_key_id:
            return base64.b64decode(legacy_content).decode("utf-8")
        try:
            data_key = self.key_store.unwrap(wrapped_key)
            return self.encryptor.decrypt_message(
                base64.b64decode(ciphertext, validate=True),
                base64.b64decode(nonce, validate=True),
                data_key,
            )
        except Exception:
            return "[Encrypted message unavailable]"
