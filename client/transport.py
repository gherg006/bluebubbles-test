#HTTPS only transport for all BlueBubbles client requests.

import os
import socket
import ssl
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import urlopen
#All external libraries are imported at the start of the file.

#The server IP as well as the certificate file are put here so that if locations change, I am able to change them here.
DEFAULT_SERVER_URL = "https://192.168.0.150:5000"
_BUNDLED_CA_FILE = Path(__file__).with_name("certificates") / "bluebubbles-lan-root-ca.pem"


class TransportSecurityError(ValueError):
    #Raised when the client is configured to use an insecure server URL.
    pass


def server_url():
    #Return the configured HTTPS endpoint, rejecting plaintext HTTP.
    url = os.getenv("BLUEBUBBLES_SERVER_URL", DEFAULT_SERVER_URL).rstrip("/")
    parsed = urlsplit(url)
    #Ensures that HTTPS is being used, and not HTTP.
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise TransportSecurityError(
            "BLUEBUBBLES_SERVER_URL must be an HTTPS URL, for example "
            "https://192.168.0.150:5000."
        )
    return url
    #The HTTPS url is returned.


def _certificate_authority_file():
    #Prefer an explicit CA, otherwise use the LAN CA packaged with the client (used during program dev).
    configured = os.getenv("BLUEBUBBLES_CA_CERT_FILE")
    if configured:
        return Path(configured)
    #Simply returns the certificate file so that the program doesn't have direct access to it.
    return _BUNDLED_CA_FILE if _BUNDLED_CA_FILE.is_file() else None


def ssl_context():
    #Build a TLS context with hostname and certificate checks.
    context = ssl.create_default_context()
    #Sets the minimum version to TLS1.2
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    #Certificate is put into a variable.
    ca_file = _certificate_authority_file()
    
    if ca_file:
        context.load_verify_locations(cafile=str(ca_file))
    #If the certificate is good, it will return the context for ssl.
    return context


def open_server(request, timeout=5):            #Replaces urlopen everywhere else. More secure this way.
    #Open a request using the app's verified TLS context.
    return urlopen(request, timeout=timeout, context=ssl_context())


def connection_error_message(error):
    #Common connection error messages turn into messages the user is able to read.
    #Reason is grabbed from the error.
    #Not going to comment each error, they are self explanatory from the error messages.
    reason = getattr(error, "reason", error)
    if isinstance(reason, ssl.SSLCertVerificationError):
        return (
            "Secure server verification failed. Update the complete client folder."
            "so client/certificates/bluebubbles-lan-root-ca.pem is present."
        )
    if isinstance(reason, ssl.SSLError):
        return "The server's secure connection could not be established."
    if isinstance(reason, (socket.timeout, TimeoutError)):
        return "The secure server did not respond. Check that it is running."
    if isinstance(reason, ConnectionRefusedError):
        return "The secure server is not accepting connections. Check the server service."
    return "Could not reach the secure server. Check the LAN connection and server address."
#If none of these are accepted, the client will serve an unknown error meaning it is a deeper rooted issue.
