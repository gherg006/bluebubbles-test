import base64
import binascii
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
#We don't need to use many libraries, mainly just for encryption.
#The most important one here is the AESCGM as it is what is actually doing the encryption.

class EncryptionConfigurationError(RuntimeError):
    pass


class MessageEncryptor:
    #AES-GCM uses a 96-bit nonce and includes its auth tag in ciphertext.
    _nonce_size = 12
    _key_size = 32
    _key_wrap_aad = b"bluebubbles:message-key:v1"
    _message_aad = b"bluebubbles:message:v1"
    #All of the variables have been defined.
    #This allows us to change the values only here without needing to change them everywhere.

    def __init__(self, environment=None):
        self.environment = environment if environment is not None else os.environ

    def create_data_key(self):
        # Create an independent AES-256 key for one recipient's stored messages.
        return os.urandom(self._key_size)

    def wrap_data_key(self, data_key):
        #Prefix the wrapping nonce so that it is self contained in the DB.
        nonce = os.urandom(self._nonce_size)
        #A random nonce is generated with the correct size.
        wrapped_key = AESGCM(self._master_key()).encrypt(
            nonce, data_key, self._key_wrap_aad
        )
        return nonce + wrapped_key
        #The new wrapped key is returned as well as the nonce.

    def unwrap_data_key(self, wrapped_key):
        #Recover a data key only after AES-GCM authenticates the stored wrapped value.
        #Checks first to ensure the key is the right length.
        if len(wrapped_key) <= self._nonce_size:
            raise ValueError("Wrapped encryption key is invalid.")
        #Nonce and key are written to variables.
        nonce = wrapped_key[:self._nonce_size]
        encrypted_key = wrapped_key[self._nonce_size:]
        #Variables are passed into the AES decryption method then the nonce and key are returned.
        return AESGCM(self._master_key()).decrypt(
            nonce, encrypted_key, self._key_wrap_aad
        )

    def encrypt_message(self, content, data_key):
        #Encrypt text before it is passed to the database.
        #This is because the database acts as a dumb store.
        nonce = os.urandom(self._nonce_size)
        #The message is encrypted and the nonce and ciphertext is returned.
        ciphertext = AESGCM(data_key).encrypt(
            nonce, content.encode("utf-8"), self._message_aad
        )
        return ciphertext, nonce

    def decrypt_message(self, ciphertext, nonce, data_key):
        #Return authenticated UTF-8 text.
        #This will be passed out as the API response.
        return AESGCM(data_key).decrypt(
            nonce, ciphertext, self._message_aad
        ).decode("utf-8")

    def _master_key(self):
        encoded_key = self.environment.get("BLUEBUBBLES_MESSAGE_MASTER_KEY", "")
        #This gets the encoded master key that the program uses.
        #It checks whether one has been configured yet before trying to fetch anything.
        if not encoded_key:
            raise EncryptionConfigurationError(
                "BLUEBUBBLES_MESSAGE_MASTER_KEY is not configured."
            )
        #It then tries to ensure that it is encoded in the right manner.
        #If it is, then it also checks that it decodes to 32 bytes (the correct length)
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
        #Only if all those checks pass, then the master key is returned.