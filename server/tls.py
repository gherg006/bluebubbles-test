# Strict TLS configuration for the BlueBubbles HTTP API.

import os
import ssl
from pathlib import Path


DEFAULT_CERT_FILE = "/etc/bluebubbles/tls/server-cert.pem"
DEFAULT_KEY_FILE = "/etc/bluebubbles/tls/server-key.pem"


class TLSConfigurationError(RuntimeError):
    # Raised when the server cannot start with a secure TLS configuration.
    pass


def tls_context(environment=None):
    # Return a TLS-1.2-or-newer server context, with no HTTP fallback.
    environment = environment if environment is not None else os.environ
    certificate = Path(environment.get("BLUEBUBBLES_TLS_CERT_FILE", DEFAULT_CERT_FILE))
    private_key = Path(environment.get("BLUEBUBBLES_TLS_KEY_FILE", DEFAULT_KEY_FILE))
    missing = [str(path) for path in (certificate, private_key) if not path.is_file()]
    if missing:
        raise TLSConfigurationError(
            "TLS cannot start because required file(s) are missing: " + ", ".join(missing)
        )

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    try:
        context.load_cert_chain(certfile=certificate, keyfile=private_key)
    except ssl.SSLError as error:
        raise TLSConfigurationError("TLS certificate or private key is invalid.") from error
    return context
