"""Tests for Google Cloud KMS integration."""

import hashlib
from unittest.mock import MagicMock
from unittest.mock import Mock
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from simplevalidations.core import gcp_kms


def _generate_test_key_pair():
    """Generate a test EC key pair."""
    private_key = ec.generate_private_key(ec.SECP256R1())
    public_key = private_key.public_key()
    return private_key, public_key


def _public_key_to_pem(public_key) -> str:
    """Convert public key to PEM format."""
    pem_bytes = public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return pem_bytes.decode()


def _public_key_to_der(public_key) -> bytes:
    """Convert public key to DER format."""
    return public_key.public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )


class TestGetPublicKeyDer:
    """Tests for get_public_key_der function."""

    @patch("simplevalidations.core.gcp_kms._kms_client")
    def test_fetches_and_converts_public_key(self, mock_kms_client):
        """Test that get_public_key_der fetches and converts KMS key to DER."""
        # Generate test key
        _, public_key = _generate_test_key_pair()
        pem = _public_key_to_pem(public_key)
        expected_der = _public_key_to_der(public_key)

        # Mock KMS response
        mock_client = MagicMock()
        mock_response = Mock()
        mock_response.pem = pem
        mock_client.get_public_key.return_value = mock_response
        mock_kms_client.return_value = mock_client

        # Test
        key_name = "projects/test/locations/us/keyRings/test/cryptoKeys/test"
        result = gcp_kms.get_public_key_der(key_name)

        # Verify
        assert result == expected_der
        mock_client.get_public_key.assert_called_once_with(
            request={"name": f"{key_name}/cryptoKeyVersions/1"}
        )

    @patch("simplevalidations.core.gcp_kms._kms_client")
    def test_caches_results(self, mock_kms_client):
        """Test that get_public_key_der caches results."""
        # Generate test key
        _, public_key = _generate_test_key_pair()
        pem = _public_key_to_pem(public_key)

        # Mock KMS response
        mock_client = MagicMock()
        mock_response = Mock()
        mock_response.pem = pem
        mock_client.get_public_key.return_value = mock_response
        mock_kms_client.return_value = mock_client

        # Clear cache first
        gcp_kms.get_public_key_der.cache_clear()

        # Test - call twice with same key
        key_name = "projects/test/locations/us/keyRings/test/cryptoKeys/test"
        gcp_kms.get_public_key_der(key_name)
        gcp_kms.get_public_key_der(key_name)

        # Verify - should only call KMS once due to caching
        assert mock_client.get_public_key.call_count == 1


class TestKidFromDer:
    """Tests for kid_from_der function."""

    def test_generates_stable_kid(self):
        """Test that kid_from_der generates stable key ID."""
        _, public_key = _generate_test_key_pair()
        der = _public_key_to_der(public_key)

        # Call twice
        kid1 = gcp_kms.kid_from_der(der)
        kid2 = gcp_kms.kid_from_der(der)

        # Should be identical
        assert kid1 == kid2

    def test_kid_is_base64url_encoded(self):
        """Test that kid is base64url encoded."""
        _, public_key = _generate_test_key_pair()
        der = _public_key_to_der(public_key)

        kid = gcp_kms.kid_from_der(der)

        # Base64url should not contain + or / or =
        assert "+" not in kid
        assert "/" not in kid
        assert "=" not in kid


class TestJwkFromGcpKey:
    """Tests for jwk_from_gcp_key function."""

    @patch("simplevalidations.core.gcp_kms.get_public_key_der")
    def test_creates_valid_jwk(self, mock_get_der):
        """Test that jwk_from_gcp_key creates valid JWK."""
        # Generate test key
        _, public_key = _generate_test_key_pair()
        der = _public_key_to_der(public_key)
        mock_get_der.return_value = der

        # Test
        key_name = "projects/test/locations/us/keyRings/test/cryptoKeys/test"
        jwk = gcp_kms.jwk_from_gcp_key(key_name, "ES256")

        # Verify JWK structure
        assert jwk["kty"] == "EC"
        assert jwk["alg"] == "ES256"
        assert jwk["use"] == "sig"
        assert "kid" in jwk
        assert jwk["kid"] == gcp_kms.kid_from_der(der)
        # EC keys should have x and y coordinates
        assert "x" in jwk
        assert "y" in jwk

    @patch("simplevalidations.core.gcp_kms.get_public_key_der")
    def test_jwk_with_different_algorithm(self, mock_get_der):
        """Test JWK creation with different algorithm."""
        _, public_key = _generate_test_key_pair()
        der = _public_key_to_der(public_key)
        mock_get_der.return_value = der

        key_name = "projects/test/locations/us/keyRings/test/cryptoKeys/test"
        jwk = gcp_kms.jwk_from_gcp_key(key_name, "PS256")

        assert jwk["alg"] == "PS256"


class TestSignData:
    """Tests for sign_data function."""

    @patch("simplevalidations.core.gcp_kms._kms_client")
    def test_signs_data_with_sha256_digest(self, mock_kms_client):
        """Test that sign_data uses SHA-256 digest."""
        # Mock KMS response
        mock_client = MagicMock()
        mock_response = Mock()
        mock_response.signature = b"test_signature"
        mock_client.asymmetric_sign.return_value = mock_response
        mock_kms_client.return_value = mock_client

        # Test
        key_name = "projects/test/locations/us/keyRings/test/cryptoKeys/test"
        data = b"test data to sign"
        signature = gcp_kms.sign_data(key_name, data)

        # Verify
        assert signature == b"test_signature"
        expected_digest = hashlib.sha256(data).digest()
        mock_client.asymmetric_sign.assert_called_once_with(
            request={
                "name": f"{key_name}/cryptoKeyVersions/1",
                "digest": {"sha256": expected_digest},
            }
        )
