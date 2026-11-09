"""Unit tests for GitHub App JWT generation and installation token exchange."""

import time
from unittest.mock import MagicMock, patch
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization
import jwt

from src.worker.auth import generate_jwt, get_app_installation_token, load_private_key


@pytest.fixture
def test_rsa_key():
    """Generates a test RSA private key."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")
    return pem


def test_generate_jwt(test_rsa_key):
    token = generate_jwt("12345", test_rsa_key, expiration_seconds=300)
    assert token is not None

    # Decode without verification to inspect claims
    decoded = jwt.decode(token, options={"verify_signature": False})
    assert decoded["iss"] == "12345"
    assert "exp" in decoded
    assert "iat" in decoded


def test_load_private_key(tmp_path, test_rsa_key):
    key_file = tmp_path / "test-app.pem"
    key_file.write_text(test_rsa_key)

    # From path
    loaded = load_private_key(str(key_file))
    assert "BEGIN PRIVATE KEY" in loaded

    # From string content
    loaded_direct = load_private_key(test_rsa_key)
    assert loaded_direct == test_rsa_key.strip()


def test_get_app_installation_token(test_rsa_key):
    mock_resp = MagicMock()
    mock_resp.status_code = 201
    mock_resp.json.return_value = {"token": "ghs_installation_secret_abc123"}

    with patch("httpx.Client.post", return_value=mock_resp):
        tok = get_app_installation_token(
            app_id=9876,
            private_key_or_path=test_rsa_key,
            installation_id=5555,
        )
        assert tok == "ghs_installation_secret_abc123"
