import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))

from tls import TLSConfigurationError, tls_context


class TLSConfigurationTests(unittest.TestCase):
    def test_missing_certificate_files_prevent_server_start(self):
        with tempfile.TemporaryDirectory() as directory:
            environment = {
                "BLUEBUBBLES_TLS_CERT_FILE": str(Path(directory) / "certificate.pem"),
                "BLUEBUBBLES_TLS_KEY_FILE": str(Path(directory) / "key.pem"),
            }
            with self.assertRaises(TLSConfigurationError):
                tls_context(environment)


if __name__ == "__main__":
    unittest.main()
