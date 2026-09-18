import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))

from transport import TransportSecurityError, server_url


class TransportTests(unittest.TestCase):
    def test_default_endpoint_uses_https(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(server_url(), "https://192.168.0.150:5000")

    def test_http_configuration_is_rejected(self):
        with patch.dict(os.environ, {"BLUEBUBBLES_SERVER_URL": "http://192.168.0.150:5000"}):
            with self.assertRaises(TransportSecurityError):
                server_url()


if __name__ == "__main__":
    unittest.main()
