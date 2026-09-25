import pytest

from commerce_common.auth import CallerPolicy, ServiceAuth, hash_api_key
from commerce_common.errors import Unauthorized


def make_auth() -> ServiceAuth:
    return ServiceAuth(
        [
            CallerPolicy("agent-svc", hash_api_key("agent-key"), frozenset({"commerce:read"})),
            CallerPolicy("checkout-svc", hash_api_key("checkout-key"), frozenset({"commerce:quotes:lock"})),
        ]
    )


def test_authenticates_each_caller_by_its_own_key() -> None:
    auth = make_auth()
    assert auth.authenticate("agent-key").name == "agent-svc"
    assert auth.authenticate("checkout-key").name == "checkout-svc"


def test_rejects_unknown_keys() -> None:
    with pytest.raises(Unauthorized):
        make_auth().authenticate("guess")


def test_hash_is_sha256_hex() -> None:
    digest = hash_api_key("x")
    assert len(digest) == 64
    assert digest == digest.lower()
