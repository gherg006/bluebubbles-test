"""HTTPS-only transport for all BlueBubbles client requests."""

import os
import ssl
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import urlopen


DEFAULT_SERVER_URL = "https://192.168.0.150:5000"
_BUNDLED_CA_FILE = Path(__file__).with_name("certificates") / "bluebubbles-lan-root-ca.pem"


class TransportSecurityError(ValueError):
    """Raised when the client is configured to use an insecure server URL."""


def server_url():
    """Return the configured HTTPS endpoint, rejecting plaintext HTTP outright."""
    url = os.getenv("BLUEBUBBLES_SERVER_URL", DEFAULT_SERVER_URL).rstrip("/")
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise TransportSecurityError(
            "BLUEBUBBLES_SERVER_URL must be an HTTPS URL, for example "
            "https://192.168.0.150:5000."
        )
    return url


def _certificate_authority_file():
    """Prefer an explicit CA, otherwise use the LAN CA packaged with the client."""
    configured = os.getenv("BLUEBUBBLES_CA_CERT_FILE")
    if configured:
        return Path(configured)
    return _BUNDLED_CA_FILE if _BUNDLED_CA_FILE.is_file() else None


def ssl_context():
    """Build a verifying TLS context; certificate and hostname checks stay enabled."""
    context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    ca_file = _certificate_authority_file()
    if ca_file:
        context.load_verify_locations(cafile=str(ca_file))
    return context


def open_server(request, timeout=5):
    """Open a request using the app's verified TLS context."""
    return urlopen(request, timeout=timeout, context=ssl_context())
