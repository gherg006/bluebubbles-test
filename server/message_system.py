import base64
import json
import os
import tempfile
import uuid
from pathlib import Path

from message_encryption import MessageEncryptor


# Keep the default beside the service code so a non-root systemd user can write it.
DEFAULT_UPLOAD_DIRECTORY = Path(__file__).with_name("uploads")
DEFAULT_MAX_FILE_BYTES = 16 * 1024 * 1024


class FileStorageError(RuntimeError):
    pass


class EncryptedFileStorage:
    # Stores AES-GCM encrypted blobs under UUIDs; user-provided names never form paths.
    def __init__(self, encryptor, environment=None):
        environment = environment if environment is not None else os.environ
        self.encryptor = encryptor
        self.directory = Path(
            environment.get("BLUEBUBBLES_UPLOAD_DIR", DEFAULT_UPLOAD_DIRECTORY)
        )
        try:
            self.max_file_bytes = int(
                environment.get("BLUEBUBBLES_MAX_UPLOAD_BYTES", DEFAULT_MAX_FILE_BYTES)
            )
        except ValueError as error:
            raise FileStorageError("BLUEBUBBLES_MAX_UPLOAD_BYTES must be an integer.") from error
        if self.max_file_bytes <= 0:
            raise FileStorageError("BLUEBUBBLES_MAX_UPLOAD_BYTES must be greater than zero.")

    def _file_path(self, file_id):
        try:
            identifier = uuid.UUID(str(file_id))
        except (ValueError, AttributeError) as error:
            raise FileStorageError("The file identifier is invalid.") from error
        return self.directory / f"{identifier}.bin"

    def store(self, file_id, contents, data_key):
        # Write a complete authenticated encrypted blob atomically with restrictive permissions.
        if len(contents) > self.max_file_bytes:
            raise FileStorageError("The file is too large.")
        destination = self._file_path(file_id)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if destination.exists():
            raise FileStorageError("The generated file identifier already exists.")
        temporary_name = None
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=".upload-", suffix=".tmp", dir=self.directory
            )
            with os.fdopen(descriptor, "wb") as temporary_file:
                os.chmod(temporary_name, 0o600)
                temporary_file.write(self.encryptor.encrypt_file(contents, data_key))
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            os.replace(temporary_name, destination)
        except OSError as error:
            raise FileStorageError("The encrypted file could not be stored.") from error
        finally:
            if temporary_name:
                Path(temporary_name).unlink(missing_ok=True)

    def retrieve(self, file_id, data_key):
        # Authenticate and decrypt only a blob whose UUID has passed the database authorization check.
        try:
            encrypted_contents = self._file_path(file_id).read_bytes()
        except OSError as error:
            raise FileStorageError("The encrypted file is unavailable.") from error
        try:
            return self.encryptor.decrypt_file(encrypted_contents, data_key)
        except Exception as error:
            raise FileStorageError("The encrypted file could not be authenticated.") from error

    def delete(self, file_id):
        # Remove an orphan created when its corresponding message insert fails.
        try:
            self._file_path(file_id).unlink(missing_ok=True)
        except OSError:
            pass


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
    def __init__(self, run_query, encryptor=None, file_storage=None):
        self.run_query = run_query
        self.encryptor = encryptor or MessageEncryptor()
        self.key_store = MessageKeyStore(run_query, self.encryptor)
        self.file_storage = file_storage or EncryptedFileStorage(self.encryptor)

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
        return self._insert_encrypted(
            sender, recipient, ciphertext, nonce, encryption_key_id, "text", None
        )

    def send_file(self, sender, recipient, filename, contents):
        # Keep the original filename only in the encrypted message metadata.
        if not filename or len(contents) > self.file_storage.max_file_bytes:
            return False
        file_id = uuid.uuid4()
        try:
            key_record = self.key_store.get_or_create(recipient)
            if key_record is None:
                return False
            encryption_key_id, data_key = key_record
            metadata = json.dumps(
                {"file_id": str(file_id), "filename": filename},
                ensure_ascii=False,
                separators=(",", ":"),
            )
            ciphertext, nonce = self.encryptor.encrypt_message(metadata, data_key)
            self.file_storage.store(file_id, contents, data_key)
        except (FileStorageError, ValueError, TypeError):
            return False
        except Exception:
            return False

        try:
            saved = self._insert_encrypted(
                sender, recipient, ciphertext, nonce, encryption_key_id, "file", file_id
            )
        except Exception:
            self.file_storage.delete(file_id)
            return False
        if not saved:
            self.file_storage.delete(file_id)
            return False
        return str(file_id)

    def _insert_encrypted(
        self, sender, recipient, ciphertext, nonce, encryption_key_id, message_type, attachment_uuid
    ):
        # Store text and file notices in the same encrypted messages table.
        result = self.run_query(
            "INSERT INTO messages "
            "(sender_id, recipient_id, message_content, sent_at, body_ciphertext, "
            "encryption_nonce, encryption_key_id, encryption_version, encrypted_at, "
            "message_type, attachment_uuid) "
            "SELECT sender.\"userID\", recipient.\"userID\", '', CURRENT_TIMESTAMP, "
            "decode(:'ciphertext', 'base64'), decode(:'nonce', 'base64'), "
            ":'encryption_key_id', 1, CURRENT_TIMESTAMP, :'message_type', "
            "NULLIF(:'attachment_uuid', '')::uuid "
            "FROM users sender JOIN users recipient ON TRUE "
            "WHERE sender.username = :'sender' AND recipient.username = :'recipient' "
            "RETURNING message_id;",
            {
                "sender": sender,
                "recipient": recipient,
                "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
                "nonce": base64.b64encode(nonce).decode("ascii"),
                "encryption_key_id": encryption_key_id,
                "message_type": message_type,
                "attachment_uuid": str(attachment_uuid or ""),
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
            "replace(encode(encryption_keys.wrapped_key, 'base64'), E'\\n', ''), "
            "messages.message_type "
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
                message_type,
            ) = row.split("|", 8)
            content = self._message_content(
                legacy_content, encryption_key_id, ciphertext, nonce, wrapped_key
            )
            message = {"sender": sender, "content": content, "date": sent_date, "time": sent_time}
            if message_type == "file":
                try:
                    metadata = json.loads(content)
                    message.update(
                        type="file", file_id=str(uuid.UUID(metadata["file_id"])),
                        filename=metadata["filename"], content="",
                    )
                except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                    message["content"] = "[Encrypted file unavailable]"
            messages.append(message)
        return messages

    def retrieve_file(self, username, file_id):
        # Authorize either participant before unwrapping the recipient-scoped file key.
        try:
            file_id = str(uuid.UUID(str(file_id)))
        except (ValueError, AttributeError):
            return None
        result = self.run_query(
            "SELECT replace(encode(encryption_keys.wrapped_key, 'base64'), E'\\n', '') "
            "FROM messages "
            "JOIN encryption_keys ON encryption_keys.id = messages.encryption_key_id "
            "JOIN users sender ON sender.\"userID\" = messages.sender_id "
            "JOIN users recipient ON recipient.\"userID\" = messages.recipient_id "
            "WHERE messages.attachment_uuid = :'file_id'::uuid "
            "AND (sender.username = :'username' OR recipient.username = :'username') LIMIT 1;",
            {"file_id": file_id, "username": username},
        )
        if result.returncode != 0 or not result.stdout.strip():
            return None
        try:
            return self.file_storage.retrieve(
                file_id, self.key_store.unwrap(result.stdout.strip())
            )
        except (FileStorageError, ValueError):
            return None

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
