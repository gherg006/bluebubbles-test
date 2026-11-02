import base64           #Used for encoding messages
import uuid

#Local libary imported from seperate file.
from message_encryption import MessageEncryptor


class MessageKeyStore:          #Everything for the key handling.
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
        #Catches an error where nothing is returned.
        if result.returncode != 0:
            return None
        #If everything goes correctly then the key is unwrapped and returned.
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
        #Checks whether query was successfully executed or not.
        if result.returncode != 0 or not result.stdout.strip():
            return None
        return result.stdout.strip(), data_key
        #If successfull, returns the query result as well as the original data key.

    def unwrap(self, wrapped_key):
        #Decode database-safe base64 only immediately before AES-GCM key unwrapping.
        return self.encryptor.unwrap_data_key(
            base64.b64decode(wrapped_key, validate=True)
        )


class MessageSystem:            #New class for the whole message handling.
    #Stores encrypted new messages while keeping legacy plaintext message support.
    def __init__(self, run_query, encryptor=None):
        self.run_query = run_query
        #Now an encryptor and key store need to be added to ensure messages aren't plaintext.
        self.encryptor = encryptor or MessageEncryptor()
        self.key_store = MessageKeyStore(run_query, self.encryptor)

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
        #Query to include more of the fields that we need.
        #ciphertext, nonce, keyid, version, encrypt timestamp.
        #Additionally return json packet has more contents (all to do with encryption).
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
        #If everything succeeds, a bool value is returned to let it know whether the message was sent or not.
        return result.returncode == 0 and bool(result.stdout.strip())

    def conversation(self, username, other_user):
        #Load legacy base64 text or encrypted fields without sending sensitive values to logs.
        #This query loads the messages between two accounts.
        #It has grown since iteration 1 to account for the encryption tables being added.
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
        #Returns an empty list if the query fails
        if result.returncode != 0:
            return []

        #It selects all the messages between two users and orders them by timestamp and message id.
        #This ensures that they are always in the correct order (as messages can have the same timestamp but not ID).
        #These are then put in a simple message array.
        messages = []

        #Changed the code formatting here to make it more readable.
        #Also had to do this to accomodate the extra fields that come with encryption.
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
            #Actually appends it to the messages list.
            messages.append(
                {
                    "sender": sender,
                    "content": content,
                    "date": sent_date,
                    "time": sent_time,
                }
            )
        #Keeps looping until there is no more to be added to the array.
        return messages


    def _message_content(self, legacy_content, encryption_key_id, ciphertext, nonce, wrapped_key):
        #Keep pre-encryption rows readable and isolate a corrupted encrypted row.
        #Don't really need this feature but it's nice to have to show messages pre-encryption vs after.
        if not encryption_key_id:
            return base64.b64decode(legacy_content).decode("utf-8")
        #This is the decryption logic that is used below.
        #It unwraps the key, validates it with base64, then calls the decryption.
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
