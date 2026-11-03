"""Testes de bloqueio de destinos internos para a ferramenta web."""
import unittest
from unittest.mock import patch
from api import _validate_public_url, _ValidatedRedirectHandler


class PublicUrlTests(unittest.TestCase):
    def test_rejects_localhost(self):
        with patch.dict("os.environ", {"ORPHEUS_ALLOW_PRIVATE_WEB_URLS": "0"}):
            with self.assertRaises(ValueError):
                _validate_public_url("http://localhost:8000/secret")

    def test_rejects_private_ip(self):
        with patch.dict("os.environ", {"ORPHEUS_ALLOW_PRIVATE_WEB_URLS": "0"}):
            with patch("api.socket.getaddrinfo", return_value=[(None, None, None, None, ("192.168.1.10", 80))]):
                with self.assertRaises(ValueError):
                    _validate_public_url("http://internal-example.test")

    def test_rejects_credentials_in_url(self):
        with self.assertRaises(ValueError):
            _validate_public_url("https://user:pass@example.com/")

    def test_public_ip_allowed(self):
        with patch.dict("os.environ", {"ORPHEUS_ALLOW_PRIVATE_WEB_URLS": "0"}):
            with patch("api.socket.getaddrinfo", return_value=[(None, None, None, None, ("8.8.8.8", 443))]):
                _validate_public_url("https://example.com/news")

    def test_redirect_to_internal_blocked(self):
        handler = _ValidatedRedirectHandler()
        with patch.dict("os.environ", {"ORPHEUS_ALLOW_PRIVATE_WEB_URLS": "0"}):
            with self.assertRaises(ValueError):
                handler.redirect_request(None, None, 302, "Found", {}, "http://127.0.0.1/")


if __name__ == "__main__":
    unittest.main()
