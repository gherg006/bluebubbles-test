import base64       #Used for encoding messages.
import hashlib
import hmac
import json
import os
import tempfile
import uuid
from pathlib import Path
#The external libraries are imported at the start of the file.

#Needed for encryption as well as the authentication.
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from message_encryption import MessageEncryptor


#Keep the default beside the service code so a non-root systemd user can write it.
#Assigns all the variables so that they can be changed later on.
DEFAULT_UPLOAD_DIRECTORY = Path(__file__).with_name("uploads")
MAXIMUM_FILE_BYTES = 2 * 1024 * 1024 * 1024
DEFAULT_MAX_FILE_BYTES = MAXIMUM_FILE_BYTES
FILE_CHUNK_BYTES = 1024 * 1024
MESSAGE_PAGE_SIZE = 100


class FileStorageError(RuntimeError):
    #Catches a file storage error that is for all of the files.
    pass


class EncryptedFileStorage:             #The class for the file storage.
    #Stores AES-GCM encrypted blobs under UUIDs.
    #User provided paths are validated and initialised.
    
    def __init__(self, encryptor, environment=None):
        #Fallback environment variable is used if not provided by the user.
        environment = environment if environment is not None else os.environ

        self.encryptor = encryptor
        #Set the target upload directory using env config or defaults.
        self.directory = Path(
            environment.get("BLUEBUBBLES_UPLOAD_DIR", DEFAULT_UPLOAD_DIRECTORY)
        )
        try:
            #Parse max fule size limit from env variables.
            self.max_file_bytes = int(
                environment.get("BLUEBUBBLES_MAX_UPLOAD_BYTES", DEFAULT_MAX_FILE_BYTES)
            )
        except ValueError as error:
            #Raise custom error if size value cannot be parsed as an integer.
            raise FileStorageError("BLUEBUBBLES_MAX_UPLOAD_BYTES must be an integer.") from error
        if self.max_file_bytes <= 0:
            #Raise error if file size is zero or negative.
            raise FileStorageError("BLUEBUBBLES_MAX_UPLOAD_BYTES must be greater than zero.")
        if self.max_file_bytes > MAXIMUM_FILE_BYTES:
            #Raise error if file size limit exceeds 2 GB.
            raise FileStorageError("BLUEBUBBLES_MAX_UPLOAD_BYTES cannot exceed 2 GB.")

    def _file_path(self, file_id):
        #Safely restore and construct the file path for an identifier.
        try:
            identifier = uuid.UUID(str(file_id))
        except (ValueError, AttributeError) as error:
            #Handle invalid identifier.
            raise FileStorageError("The file identifier is invalid.") from error
        #Return the completed path combining directory and file uuid.
        return self.directory / f"{identifier}.bin"

    def store(self, file_id, contents, data_key):
        #Write a complete authenticated encrypted blob atomically with restrictive permissions.
        #Checks if file size is within limit. If not, raises an error.
        if len(contents) > self.max_file_bytes:
            raise FileStorageError("The file is too large.")
        #Resolve the file path and create the directory if it doesn't exist.
        #Has restricted permissions to prevent unauthorized access.
        destination = self._file_path(file_id)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        #Raise an error if the file already exists.
        if destination.exists():
            raise FileStorageError("The generated file identifier already exists.")
        temporary_name = None
        try:
            #Create a temporary file to store the encrypted file.
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=".upload-", suffix=".tmp", dir=self.directory
            )
            with os.fdopen(descriptor, "wb") as temporary_file:
                #Set restrictive file permissions on temp file.
                os.chmod(temporary_name, 0o600)
                #Encrypt the file contents and write them to the disk.
                temporary_file.write(self.encryptor.encrypt_file(contents, data_key))
                #Flush and sync the file to disk.
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            #Replace the temp file with the permanent one.
            os.replace(temporary_name, destination)
        except OSError as error:
            #Catch OS errors and give out an error message.
            raise FileStorageError("The encrypted file could not be stored.") from error
        finally:
            if temporary_name:
                #Clean up temporary file if it still exists after an exception.
                Path(temporary_name).unlink(missing_ok=True)

    @staticmethod
    def _checksum(value):
        #Validate that the checksum is a 64 character string.
        if not isinstance(value, str) or len(value) != 64:
            raise FileStorageError("The file checksum is invalid.")
        try:
            #Verify the string is a hex number.
            int(value, 16)
        except ValueError as error:
            raise FileStorageError("The file checksum is invalid.") from error
        #Return the normalised lowercase checksum string.
        return value.lower()

    def store_stream(self, file_id, source, data_key, expected_checksum):
        #Encrypt a upload in chunks, validating its client-provided SHA-256 digest.
        expected_checksum = self._checksum(expected_checksum)
        destination = self._file_path(file_id)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if destination.exists():
            raise FileStorageError("The generated file identifier already exists.")
        temporary_name = None
        try:
            #Create a temporary file in the storage directory for atomic processing.
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=".upload-", suffix=".tmp", dir=self.directory
            )
            digest = hashlib.sha256()
            #Generate a random crypto key for the upload. This key is used to encrypt the data.
            #Initialize the GCM cipher with the key and nonce.
            nonce = os.urandom(self.encryptor._nonce_size)
            encryptor = Cipher(algorithms.AES(data_key), modes.GCM(nonce)).encryptor()
            encryptor.authenticate_additional_data(self.encryptor._file_aad)
            total_bytes = 0
            with os.fdopen(descriptor, "wb") as temporary_file:
                #Set restrictive file permissions on the temp file.
                #Prevents it being modified.
                os.chmod(temporary_name, 0o600)
                temporary_file.write(nonce)
                while True:
                    #Read incoming data stream in chunks.
                    chunk = source.read(FILE_CHUNK_BYTES)
                    if not chunk:
                        break
                    if not isinstance(chunk, bytes):
                        raise FileStorageError("The uploaded file is invalid.")
                    total_bytes += len(chunk)
                    #Enforce maximum file size limits during streaming.
                    if total_bytes > self.max_file_bytes:
                        raise FileStorageError("The file is too large.")
                    digest.update(chunk)
                    #Encrypt each chunk on the fly and write it to the temp file.
                    temporary_file.write(encryptor.update(chunk))
                #Verify that the the computed SHA-256 hash matches the expected checksum.
                #This prevents the attacker from modifying the file after it.
                if not hmac.compare_digest(digest.hexdigest(), expected_checksum):
                    raise FileStorageError("The uploaded file checksum does not match.")
                #Finalise encryption, append the auth tag, flush buffers and sync to disk.
                temporary_file.write(encryptor.finalize())
                temporary_file.write(encryptor.tag)
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            #Atomically move the completed file to its final destination path.
            os.replace(temporary_name, destination)
            return digest.hexdigest()
        except OSError as error:
            raise FileStorageError("The encrypted file could not be stored.") from error
        finally:
            #If it fails, then clean up the temporary file.
            if temporary_name:
                Path(temporary_name).unlink(missing_ok=True)

    def retrieve(self, file_id, data_key):
        #Authenticate and decrypt only a blob whose UUID has passed the database authorization check.
        try:
            #Read the encrypted file from the file path.
            encrypted_contents = self._file_path(file_id).read_bytes()
        except OSError as error:
            #Raise a storage error if the file cant be accessed or sent.
            raise FileStorageError("The encrypted file is unavailable.") from error
        try:
            #Decrypt the encrypted file contents with the data key.
            return self.encryptor.decrypt_file(encrypted_contents, data_key)
        except Exception as error:
            #For any exceptions then raise the error.
            raise FileStorageError("The encrypted file could not be authenticated.") from error

    def retrieve_stream(self, file_id, data_key, expected_checksum):
        #Verify the complete encrypted blob before sending any plaintext to a client.
        expected_checksum = self._checksum(expected_checksum)
        path = self._file_path(file_id)
        try:
            #Retrieve the total size of the encrypted file on disk.
            encrypted_size = path.stat().st_size
        except OSError as error:
            raise FileStorageError("The encrypted file is unavailable.") from error
        #Calculate the total overhead size (nonce + AES-CGM tag).
        overhead = self.encryptor._nonce_size + 16
        if encrypted_size < overhead:
            raise FileStorageError("The encrypted file is invalid.")
        #Determine the size minus the overhead.
        plaintext_size = encrypted_size - overhead
        #Verify the file stream integrity against the expected checksum.
        self._verify_stream(path, data_key, plaintext_size, expected_checksum)
        #Return decrypted stream.
        return self._decrypt_stream(path, data_key, plaintext_size), plaintext_size

    def _verify_stream(self, path, data_key, plaintext_size, expected_checksum):
        digest = hashlib.sha256()
        for chunk in self._decrypt_stream(path, data_key, plaintext_size):
            digest.update(chunk)
        if not hmac.compare_digest(digest.hexdigest(), expected_checksum):
            raise FileStorageError("The encrypted file checksum does not match.")

    def _decrypt_stream(self, path, data_key, plaintext_size):
        #AES-GCM validates at EOF
        #Callers verify first so no unauthenticated bytes are sent.
        try:
            #Open encrypted file in binary mode.
            encrypted_file = path.open("rb")
        except OSError as error:
            raise FileStorageError("The encrypted file is unavailable.") from error
        try:
            #Reads the nonce from the start of the file.
            nonce = encrypted_file.read(self.encryptor._nonce_size)
            #Seek to the end of the payload to get the nonce and plaintext size.
            encrypted_file.seek(self.encryptor._nonce_size + plaintext_size)
            tag = encrypted_file.read(16)
            #Validate that nonce and auth tag are the correct length.
            if len(nonce) != self.encryptor._nonce_size or len(tag) != 16:
                raise FileStorageError("The encrypted file is invalid.")
            #Initialise AES-CGM decryptor with data key, nonce and data.
            decryptor = Cipher(algorithms.AES(data_key), modes.GCM(nonce, tag)).decryptor()
            decryptor.authenticate_additional_data(self.encryptor._file_aad)
            encrypted_file.seek(self.encryptor._nonce_size)
            remaining = plaintext_size
            #Stream, decrypt and yield any remaining plaintext data iteratively.
            while remaining:
                encrypted_chunk = encrypted_file.read(min(FILE_CHUNK_BYTES, remaining))
                if not encrypted_chunk:
                    raise FileStorageError("The encrypted file is truncated.")
                remaining -= len(encrypted_chunk)
                plaintext = decryptor.update(encrypted_chunk)
                if plaintext:
                    yield plaintext
            #Finalise plaintext data.
            final_plaintext = decryptor.finalize()
            if final_plaintext:
                yield final_plaintext
        except FileStorageError:
            raise
        except Exception as error:
            #Catch decryption or auth errors.
            raise FileStorageError("The encrypted file could not be authenticated.") from error
        finally:
            #Close the file.
            encrypted_file.close()

    def delete(self, file_id):
        #Remove an orphan created when its corresponding message insert fails.
        try:
            #Attempt to unlink the file, ignoring any errors if it does not exist.
            self._file_path(file_id).unlink(missing_ok=True)
        except OSError:
            pass


class MessageKeyStore:
    #Stores recipient-scoped data keys wrapped by the server master key.
    def __init__(self, run_query, encryptor):
        #Passes in arguments to their respective variables.
        self.run_query = run_query
        self.encryptor = encryptor

    def get_or_create(self, recipient):
        #Reuse the recipient's latest active key or make one before saving a message.
        scope_id = str(
            uuid.uuid5(uuid.NAMESPACE_URL, f"bluebubbles:message-recipient:{recipient}")
        )
        #The query is ran to get the key.
        #Changed from previous version to use the scope_id instead of the recipient's ID.
        #Additionally, the query isn't ran raw now, and instead through a variable.
        active_key_query = (
            "SELECT encryption_keys.id, "
            "replace(encode(encryption_keys.wrapped_key, 'base64'), E'\\n', '') "
            "FROM encryption_keys "
            "WHERE encryption_keys.scope_type = 'user' "
            "AND encryption_keys.scope_id = :'scope_id' "
            "AND encryption_keys.status = 'active' "
            "ORDER BY encryption_keys.key_version DESC, encryption_keys.id DESC LIMIT 1;"
        )
        #Query is actually ran.
        result = self.run_query(active_key_query, {"scope_id": scope_id})
        #Catches an error where nothing is returned.
        if result.returncode != 0:
            return None
        if result.stdout.strip():
            key_id, wrapped_key = result.stdout.strip().split("|", 1)
            return key_id, self.encryptor.unwrap_data_key(
                base64.b64decode(wrapped_key, validate=True)
            )
        #The a new data key is being created.
        data_key = self.encryptor.create_data_key()
        #This new key is then wrapped.
        wrapped_key = base64.b64encode(self.encryptor.wrap_data_key(data_key)).decode("ascii")
        #This inserts a new record into the table.
        #Associates an encryption key with a user.
        #If there are conflicts, it will ignore them.
        result = self.run_query(
            "INSERT INTO encryption_keys "
            "(scope_type, scope_id, wrapped_key, key_version, status, created_at, rotated_at) "
            "SELECT 'user', :'scope_id', decode(:'wrapped_key', 'base64'), "
            "1, 'active', CURRENT_TIMESTAMP, NULL "
            "FROM users recipient WHERE recipient.username = :'recipient' "
            "ON CONFLICT (scope_type, scope_id, key_version) DO NOTHING "
            "RETURNING id;",
            {
                "recipient": recipient,
                "scope_id": scope_id,
                "wrapped_key": wrapped_key,
            },
        )
        if result.returncode != 0:
            return None
        if result.stdout.strip():
            return result.stdout.strip(), data_key

        #Another request inserted this recipient's first key while we were creating ours.
        #Use the key so both messages can be sent.
        result = self.run_query(active_key_query, {"scope_id": scope_id})
        if result.returncode != 0 or not result.stdout.strip():
            return None
        key_id, wrapped_key = result.stdout.strip().split("|", 1)
        return key_id, self.encryptor.unwrap_data_key(
            base64.b64decode(wrapped_key, validate=True)
        )

    def unwrap(self, wrapped_key):
        #Decode database-safe base64 only immediately before AES-GCM key unwrapping.
        return self.encryptor.unwrap_data_key(
            base64.b64decode(wrapped_key, validate=True)
        )


class MessageSystem:            #New class for the whole message handling.
    #Stores encrypted new messages while keeping legacy plaintext message support.

    #file_storage added as a parameter to allow for different storage methods.
    def __init__(self, run_query, encryptor=None, file_storage=None):
        self.run_query = run_query
        #Now an encryptor and key store need to be added to ensure messages aren't plaintext.
        self.encryptor = encryptor or MessageEncryptor()
        self.key_store = MessageKeyStore(run_query, self.encryptor)
        self.file_storage = file_storage or EncryptedFileStorage(self.encryptor)

    def send(self, sender, recipient, content):
        #Encrypt the content before the insert and save no plaintext in message_content.
        try:
            key_record = self.key_store.get_or_create(recipient)
            if key_record is None:
                return False
            #The keyid is saved to a variable, which is then used to send the ciphertext and nonce that was used.
            encryption_key_id, data_key = key_record
            ciphertext, nonce = self.encryptor.encrypt_message(content, data_key)
        #Standard exception handling in case something goes wrong.
        except Exception:
            return False
        #Insert the encrypted message into the database.
        saved = self._insert_encrypted(
            sender, recipient, ciphertext, nonce, encryption_key_id, "text", None
        )
        #If the insert was successful, return the encrypted message.
        if not saved:
            return False
        return {**saved, "sender": sender, "content": content}

    def send_file(self, sender, recipient, filename, contents):
        #Keep the original filename only in the encrypted message metadata.
        if not filename or len(contents) > self.file_storage.max_file_bytes:
            return False
        #Generate a unique file ID for the file.
        file_id = uuid.uuid4()
        try:
            #Checksum the contents of the file to ensure it is not altered during transfer.
            checksum = hashlib.sha256(contents).hexdigest()
            key_record = self.key_store.get_or_create(recipient)
            #Get the encryption key for the recipient.
            if key_record is None:
                return False
            encryption_key_id, data_key = key_record
            #Encrypt the metadata and the contents of the file.
            metadata = json.dumps(
                {"file_id": str(file_id), "filename": filename, "checksum": checksum},
                ensure_ascii=False,
                separators=(",", ":"),
            )
            #Encrypt the metadata and the contents of the file.
            ciphertext, nonce = self.encryptor.encrypt_message(metadata, data_key)
            self.file_storage.store(file_id, contents, data_key)
        #Handle any exceptions that occur during the transfer process.
        except (FileStorageError, ValueError, TypeError):
            return False
        except Exception:
            return False

        try:
            #Insert the encrypted file into the database.
            saved = self._insert_encrypted(
                sender, recipient, ciphertext, nonce, encryption_key_id, "file", file_id, checksum
            )
        #Handle any exceptions that occur during the insertion process.
        except Exception:
            self.file_storage.delete(file_id)
            return False
        if not saved:
            #If the file was not saved, delete it from the storage.
            self.file_storage.delete(file_id)
            return False
        #Return the ID of the file that was sent.
        return str(file_id)

    def send_file_stream(self, sender, recipient, filename, source, checksum):
        #Store a large upload without buffering it
        #Then bind its SHA-256 checksum to the row.
        if not filename:
            return False
        #Create a unique ID for the file.
        file_id = uuid.uuid4()
        try:
            #The checksum is calculated and stored in the database.
            checksum = self.file_storage._checksum(checksum)
            #The key record is retrieved from the key store.
            key_record = self.key_store.get_or_create(recipient)
            #If there is no record for the recipient, return False.
            if key_record is None:
                return False
            #The encryption key and data key are retrieved.
            encryption_key_id, data_key = key_record
            #The metadata is created and encrypted.
            metadata = json.dumps(
                {"file_id": str(file_id), "filename": filename, "checksum": checksum},
                ensure_ascii=False,
                separators=(",", ":"),
            )
            # The data is encrypted and stored in the file storage.
            ciphertext, nonce = self.encryptor.encrypt_message(metadata, data_key)
            self.file_storage.store_stream(file_id, source, data_key, checksum)
        #Handle exceptions and errors. If any error occurs, return False.
        except (FileStorageError, ValueError, TypeError):
            return False
        except Exception:
            return False
        try:
            #The encrypted record is inserted into the key store.
            saved = self._insert_encrypted(
                sender, recipient, ciphertext, nonce, encryption_key_id, "file", file_id, checksum
            )
        #If any errors occur then delete the partial file and return false.
        except Exception:
            self.file_storage.delete(file_id)
            return False
        if not saved:
            self.file_storage.delete(file_id)
            return False
        #Return the saved record.
        return {
            **saved, "sender": sender, "content": "", "type": "file",
            "file_id": str(file_id), "filename": filename, "checksum": checksum,
        }

    def _insert_encrypted(
        self, sender, recipient, ciphertext, nonce, encryption_key_id, message_type, attachment_uuid,
        attachment_checksum=None,
    ):          # Insert the encrypted record into the key store.
        #Query to include more of the fields that we need.
        #ciphertext, nonce, keyid, version, encrypt timestamp.
        #Additionally return json packet has more contents (all to do with encryption).

        #Message_type, the uuid and checksum of attachments, and date are added to the query.
        #This is then put into the json packet.
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
            "RETURNING message_id, to_char(sent_at, 'DD/MM/YYYY'), "
            "to_char(sent_at, 'HH24:MI');",
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
        #If everything succeeds, a bool value is returned to let it know whether the message was sent or not.
        if result.returncode != 0 or not result.stdout.strip():
            return None
        #Split the output into the message ID, date, and time.
        message_id, sent_date, sent_time = result.stdout.strip().split("|", 2)
        #Return the message details as a dictionary.
        return {"id": int(message_id), "date": sent_date, "time": sent_time}

    def conversation(self, username, other_user, before_id=None, after_id=None):
        #Load legacy base64 text or encrypted fields without sending sensitive values to logs.
        #This query loads the messages between two accounts.
        #It has grown since iteration 1 to account for the encryption tables being added.

        cursor_clause = ""
        values = {"username": username, "other_user": other_user}
        if before_id is not None:
            # If a before_id is provided, the query will select messages before that ID.
            cursor_clause = "AND messages.message_id < :'cursor'::bigint "
            values["cursor"] = before_id
        elif after_id is not None:
           #If an after_id is provided, the query will select messages after that ID.
            cursor_clause = "AND messages.message_id > :'cursor'::bigint "
            values["cursor"] = after_id
        #Bases the direction of the query based on whether an after_id or before_id is provided.
        direction = "ASC" if after_id is not None else "DESC"
        #Constructs the SQL query with the placeholders and values.
        #Message type and attachment checksums are added to the query too.
        result = self.run_query(
            "SELECT messages.message_id, sender.username, "
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
            "WHERE ((sender.username = :'username' AND recipient.username = :'other_user') "
            "OR (sender.username = :'other_user' AND recipient.username = :'username')) "
            + cursor_clause +
            f"ORDER BY messages.message_id {direction} LIMIT {MESSAGE_PAGE_SIZE + 1};",
            values,
        )
        #Returns an empty list if the query fails
        if result.returncode != 0:
            return []

        #It selects all the messages between two users and orders them by timestamp and message id.
        #This ensures that they are always in the correct order (as messages can have the same timestamp but not ID).
        #These are then put in a simple message array.
        messages = []

         #Changed the code formatting here to make it more readable.
        #Also had to do this to accomodate the extra fields that come with encryption.
        #Added the message type and checksum too.
        for row in result.stdout.splitlines():
            (
                message_id,
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
            ) = row.split("|", 10)
            #The content is then decrypted and converted to a string.
            #This is done by calling the _message_content method.
            content = self._message_content(
                legacy_content, encryption_key_id, ciphertext, nonce, wrapped_key
            )
            #The message is then added to the messages dictionary.
            message = {
                "id": int(message_id), "sender": sender, "content": content,
                "date": sent_date, "time": sent_time,
            }
            # If it is a file, the metadata is parsed and added to the message dictionary.
            if message_type == "file":
                try:
                    metadata = json.loads(content)
                    message.update(
                        type="file", file_id=str(uuid.UUID(metadata["file_id"])),
                        filename=metadata["filename"], checksum=attachment_checksum,
                        content="",
                    )
                #If there is an error parsing the metadata, the content is set to an error message.
                except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                    message["content"] = "[Encrypted file unavailable]"
            messages.append(message)
        if direction == "DESC":
            messages.reverse()
        return messages

    def retrieve_file(self, username, file_id):
        #Authorize either user before unwrapping the recipient-scoped file key.
        try:
            #Check if the file_id is a UUID and convert it to a string if necessary.
            file_id = str(uuid.UUID(str(file_id)))
        #If the file_id is not a UUID, return None.
        except (ValueError, AttributeError):
            return None
        #Retrieve the recipient's public key from the database.
        #Ensures that it is either the sender or recipient requesting it.
        #Prevents unauthorised access to the recipient's key.
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
        #If the query returns no results or fails, return None.
        if result.returncode != 0 or not result.stdout.strip():
            return None
        try:
            #Split the result into the wrapped key and checksum.
            wrapped_key, checksum = result.stdout.strip().split("|", 1)
            #Retrieve the file contents using the wrapped key and checksum.
            contents, _ = self.file_storage.retrieve_stream(
                file_id, self.key_store.unwrap(wrapped_key), checksum
            )
            #Combine all the decrypted chunks and return it.
            return b"".join(contents)
        except (FileStorageError, ValueError):
            return None

    def retrieve_file_stream(self, username, file_id):
        #Return a verified streaming file body only to one of its participants.
        try:
            #Validate and normalise the file ID into a standard UUID format.
            file_id = str(uuid.UUID(str(file_id)))
        except (ValueError, AttributeError):
            #Return none if the file ID is invalid.
            return None
        #Fetches the wrapped key and attachment checksum from the database.
        #Ensures the user has the necessary permissions to access the file.
        #AKA the user requesting is either the sender or recipient.
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
        #Return none if query fails, has no result, or isn't authorised.
        if result.returncode != 0 or not result.stdout.strip():
            return None
        try:
            #Split the query result to extract the base64 encoded wrapped key and checksum.
            wrapped_key, checksum = result.stdout.strip().split("|", 1)
            #Retrieve the contents and length of the file using key and checksum.
            contents, length = self.file_storage.retrieve_stream(
                file_id, self.key_store.unwrap(wrapped_key), checksum
            )
            #Return the stream contents, length and checksum.
            return contents, length, checksum
        except (FileStorageError, ValueError):
            return None

    def _message_content(self, legacy_content, encryption_key_id, ciphertext, nonce, wrapped_key):
        #Keep pre-encryption rows readable and isolate a corrupted encrypted row.
        #Don't really need this feature but it's nice to have to show messages pre-encryption vs after.
        if not encryption_key_id:
            #This is the decryption logic that is used below.
            #It unwraps the key, validates it with base64, then calls the decryption.
            return base64.b64decode(legacy_content).decode("utf-8")
        try:
            data_key = self.key_store.unwrap(wrapped_key)
            return self.encryptor.decrypt_message(
                base64.b64decode(ciphertext, validate=True),
                base64.b64decode(nonce, validate=True),
                data_key,
            )
        #Standard exception handling, catches all errors.
        except Exception:
            return "[Encrypted message unavailable]"
