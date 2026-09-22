import base64
import hashlib
import hmac
import json
import os
import tempfile
import uuid
from pathlib import Path

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from message_encryption import MessageEncryptor


# Keep the default beside the service code so a non-root systemd user can write it.
DEFAULT_UPLOAD_DIRECTORY = Path(__file__).with_name("uploads")
MAXIMUM_FILE_BYTES = 2 * 1024 * 1024 * 1024
DEFAULT_MAX_FILE_BYTES = MAXIMUM_FILE_BYTES
FILE_CHUNK_BYTES = 1024 * 1024


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
        if self.max_file_bytes > MAXIMUM_FILE_BYTES:
            raise FileStorageError("BLUEBUBBLES_MAX_UPLOAD_BYTES cannot exceed 2 GB.")

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

    @staticmethod
    def _checksum(value):
        if not isinstance(value, str) or len(value) != 64:
            raise FileStorageError("The file checksum is invalid.")
        try:
            int(value, 16)
        except ValueError as error:
            raise FileStorageError("The file checksum is invalid.") from error
        return value.lower()

    def store_stream(self, file_id, source, data_key, expected_checksum):
        # Encrypt a bounded upload in chunks, validating its client-provided SHA-256 digest.
        expected_checksum = self._checksum(expected_checksum)
        destination = self._file_path(file_id)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if destination.exists():
            raise FileStorageError("The generated file identifier already exists.")
        temporary_name = None
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=".upload-", suffix=".tmp", dir=self.directory
            )
            digest = hashlib.sha256()
            nonce = os.urandom(self.encryptor._nonce_size)
            encryptor = Cipher(algorithms.AES(data_key), modes.GCM(nonce)).encryptor()
            encryptor.authenticate_additional_data(self.encryptor._file_aad)
            total_bytes = 0
            with os.fdopen(descriptor, "wb") as temporary_file:
                os.chmod(temporary_name, 0o600)
                temporary_file.write(nonce)
                while True:
                    chunk = source.read(FILE_CHUNK_BYTES)
                    if not chunk:
                        break
                    if not isinstance(chunk, bytes):
                        raise FileStorageError("The uploaded file is invalid.")
                    total_bytes += len(chunk)
                    if total_bytes > self.max_file_bytes:
                        raise FileStorageError("The file is too large.")
                    digest.update(chunk)
                    temporary_file.write(encryptor.update(chunk))
                if not hmac.compare_digest(digest.hexdigest(), expected_checksum):
                    raise FileStorageError("The uploaded file checksum does not match.")
                temporary_file.write(encryptor.finalize())
                temporary_file.write(encryptor.tag)
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            os.replace(temporary_name, destination)
            return digest.hexdigest()
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

    def retrieve_stream(self, file_id, data_key, expected_checksum):
        # Verify the complete encrypted blob before streaming any plaintext to a client.
        expected_checksum = self._checksum(expected_checksum)
        path = self._file_path(file_id)
        try:
            encrypted_size = path.stat().st_size
        except OSError as error:
            raise FileStorageError("The encrypted file is unavailable.") from error
        overhead = self.encryptor._nonce_size + 16
        if encrypted_size < overhead:
            raise FileStorageError("The encrypted file is invalid.")
        plaintext_size = encrypted_size - overhead
        self._verify_stream(path, data_key, plaintext_size, expected_checksum)
        return self._decrypt_stream(path, data_key, plaintext_size), plaintext_size

    def _verify_stream(self, path, data_key, plaintext_size, expected_checksum):
        digest = hashlib.sha256()
        for chunk in self._decrypt_stream(path, data_key, plaintext_size):
            digest.update(chunk)
        if not hmac.compare_digest(digest.hexdigest(), expected_checksum):
            raise FileStorageError("The encrypted file checksum does not match.")

    def _decrypt_stream(self, path, data_key, plaintext_size):
        # AES-GCM validates at EOF; callers verify first so no unauthenticated bytes are sent.
        try:
            encrypted_file = path.open("rb")
        except OSError as error:
            raise FileStorageError("The encrypted file is unavailable.") from error
        try:
            nonce = encrypted_file.read(self.encryptor._nonce_size)
            encrypted_file.seek(self.encryptor._nonce_size + plaintext_size)
            tag = encrypted_file.read(16)
            if len(nonce) != self.encryptor._nonce_size or len(tag) != 16:
                raise FileStorageError("The encrypted file is invalid.")
            decryptor = Cipher(algorithms.AES(data_key), modes.GCM(nonce, tag)).decryptor()
            decryptor.authenticate_additional_data(self.encryptor._file_aad)
            encrypted_file.seek(self.encryptor._nonce_size)
            remaining = plaintext_size
            while remaining:
                encrypted_chunk = encrypted_file.read(min(FILE_CHUNK_BYTES, remaining))
                if not encrypted_chunk:
                    raise FileStorageError("The encrypted file is truncated.")
                remaining -= len(encrypted_chunk)
                plaintext = decryptor.update(encrypted_chunk)
                if plaintext:
                    yield plaintext
            final_plaintext = decryptor.finalize()
            if final_plaintext:
                yield final_plaintext
        except FileStorageError:
            raise
        except Exception as error:
            raise FileStorageError("The encrypted file could not be authenticated.") from error
        finally:
            encrypted_file.close()

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
            checksum = hashlib.sha256(contents).hexdigest()
            key_record = self.key_store.get_or_create(recipient)
            if key_record is None:
                return False
            encryption_key_id, data_key = key_record
            metadata = json.dumps(
                {"file_id": str(file_id), "filename": filename, "checksum": checksum},
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
                sender, recipient, ciphertext, nonce, encryption_key_id, "file", file_id, checksum
            )
        except Exception:
            self.file_storage.delete(file_id)
            return False
        if not saved:
            self.file_storage.delete(file_id)
            return False
        return str(file_id)

    def send_file_stream(self, sender, recipient, filename, source, checksum):
        # Store a large upload without buffering it, then bind its SHA-256 checksum to the row.
        if not filename:
            return False
        file_id = uuid.uuid4()
        try:
            checksum = self.file_storage._checksum(checksum)
            key_record = self.key_store.get_or_create(recipient)
            if key_record is None:
                return False
            encryption_key_id, data_key = key_record
            metadata = json.dumps(
                {"file_id": str(file_id), "filename": filename, "checksum": checksum},
                ensure_ascii=False,
                separators=(",", ":"),
            )
            ciphertext, nonce = self.encryptor.encrypt_message(metadata, data_key)
            self.file_storage.store_stream(file_id, source, data_key, checksum)
        except (FileStorageError, ValueError, TypeError):
            return False
        except Exception:
            return False
        try:
            saved = self._insert_encrypted(
                sender, recipient, ciphertext, nonce, encryption_key_id, "file", file_id, checksum
            )
        except Exception:
            self.file_storage.delete(file_id)
            return False
        if not saved:
            self.file_storage.delete(file_id)
            return False
        return str(file_id)

    def _insert_encrypted(
        self, sender, recipient, ciphertext, nonce, encryption_key_id, message_type, attachment_uuid,
        attachment_checksum=None,
    ):
        # Store text and file notices in the same encrypted messages table.
        result = self.run_query(
            "INSERT INTO messages "
            "(sender_id, recipient_id, message_content, sent_at, body_ciphertext, "
            "encryption_nonce, encryption_key_id, encryption_version, encrypted_at, "
            "message_type, attachment_uuid, attachment_checksum) "
            "SELECT sender.\"userID\", recipient.\"userID\", '', CURRENT_TIMESTAMP, "
            "decode(:'ciphertext', 'base64'), decode(:'nonce', 'base64'), "
            ":'encryption_key_id', 1, CURRENT_TIMESTAMP, :'message_type', "
            "NULLIF(:'attachment_uuid', '')::uuid, NULLIF(:'attachment_checksum', '') "
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
                "attachment_checksum": attachment_checksum or "",
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
            "messages.message_type, messages.attachment_checksum "
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
                attachment_checksum,
            ) = row.split("|", 9)
            content = self._message_content(
                legacy_content, encryption_key_id, ciphertext, nonce, wrapped_key
            )
            message = {"sender": sender, "content": content, "date": sent_date, "time": sent_time}
            if message_type == "file":
                try:
                    metadata = json.loads(content)
                    message.update(
                        type="file", file_id=str(uuid.UUID(metadata["file_id"])),
                        filename=metadata["filename"], checksum=attachment_checksum,
                        content="",
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
            "SELECT replace(encode(encryption_keys.wrapped_key, 'base64'), E'\\n', ''), "
            "messages.attachment_checksum "
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
            wrapped_key, checksum = result.stdout.strip().split("|", 1)
            contents, _ = self.file_storage.retrieve_stream(
                file_id, self.key_store.unwrap(wrapped_key), checksum
            )
            return b"".join(contents)
        except (FileStorageError, ValueError):
            return None

    def retrieve_file_stream(self, username, file_id):
        # Return a verified streaming file body only to one of its participants.
        try:
            file_id = str(uuid.UUID(str(file_id)))
        except (ValueError, AttributeError):
            return None
        result = self.run_query(
            "SELECT replace(encode(encryption_keys.wrapped_key, 'base64'), E'\\n', ''), "
            "messages.attachment_checksum "
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
            wrapped_key, checksum = result.stdout.strip().split("|", 1)
            contents, length = self.file_storage.retrieve_stream(
                file_id, self.key_store.unwrap(wrapped_key), checksum
            )
            return contents, length, checksum
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
