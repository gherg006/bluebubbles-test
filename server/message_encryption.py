import base64
import binascii
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class EncryptionConfigurationError(RuntimeError):
    pass


class MessageEncryptor:
    # AES-GCM uses a 96-bit nonce and includes its authentication tag in ciphertext.
    _nonce_size = 12
    _key_size = 32
    _key_wrap_aad = b"bluebubbles:message-key:v1"
    _message_aad = b"bluebubbles:message:v1"
    _file_aad = b"bluebubbles:file:v1"

    def __init__(self, environment=None):
        self.environment = environment if environment is not None else os.environ

    def create_data_key(self):
        # Create an independent AES-256 key for one recipient's stored messages.
        return os.urandom(self._key_size)

    def wrap_data_key(self, data_key):
        # Prefix the wrapping nonce so the wrapped value is self-contained in the database.
        nonce = os.urandom(self._nonce_size)
        wrapped_key = AESGCM(self._master_key()).encrypt(
            nonce, data_key, self._key_wrap_aad
        )
        return nonce + wrapped_key

    def unwrap_data_key(self, wrapped_key):
        # Recover a data key only after AES-GCM authenticates the stored wrapped value.
        if len(wrapped_key) <= self._nonce_size:
            raise ValueError("Wrapped encryption key is invalid.")
        nonce = wrapped_key[:self._nonce_size]
        encrypted_key = wrapped_key[self._nonce_size:]
        return AESGCM(self._master_key()).decrypt(
            nonce, encrypted_key, self._key_wrap_aad
        )

    def encrypt_message(self, content, data_key):
        # Encrypt text before it is passed to the database layer.
        nonce = os.urandom(self._nonce_size)
        ciphertext = AESGCM(data_key).encrypt(
            nonce, content.encode("utf-8"), self._message_aad
        )
        return ciphertext, nonce

    def decrypt_message(self, ciphertext, nonce, data_key):
        # Return authenticated UTF-8 text for the API response.
        return AESGCM(data_key).decrypt(
            nonce, ciphertext, self._message_aad
        ).decode("utf-8")

    def encrypt_file(self, contents, data_key):
        # Prefix the nonce so a UUID-addressed blob is independently decryptable.
        nonce = os.urandom(self._nonce_size)
        return nonce + AESGCM(data_key).encrypt(nonce, contents, self._file_aad)

    def decrypt_file(self, encrypted_contents, data_key):
        # Reject truncated and tampered encrypted files before returning any bytes.
        if len(encrypted_contents) <= self._nonce_size:
            raise ValueError("Encrypted file is invalid.")
        nonce = encrypted_contents[:self._nonce_size]
        return AESGCM(data_key).decrypt(
            nonce, encrypted_contents[self._nonce_size:], self._file_aad
        )

    def _master_key(self):
        encoded_key = self.environment.get("BLUEBUBBLES_MESSAGE_MASTER_KEY", "")
        if not encoded_key:
            raise EncryptionConfigurationError(
                "BLUEBUBBLES_MESSAGE_MASTER_KEY is not configured."
            )
        try:
            master_key = base64.b64decode(encoded_key, validate=True)
        except (ValueError, binascii.Error) as error:
            raise EncryptionConfigurationError(
                "BLUEBUBBLES_MESSAGE_MASTER_KEY must be base64 encoded."
            ) from error
        if len(master_key) != self._key_size:
            raise EncryptionConfigurationError(
                "BLUEBUBBLES_MESSAGE_MASTER_KEY must decode to 32 bytes."
            )
        return master_key
