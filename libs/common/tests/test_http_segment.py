"""segment(): only well-formed ids reach a service-to-service URL path (found by API fuzzing)."""

import pytest

from commerce_common.errors import NotFound
from commerce_common.http import segment


@pytest.mark.parametrize("value", ["ord_57e45ed23e07ca154ba28f70", "web_abc123", "tg_-1001234", "quote_1"])
def test_real_ids_pass_through(value: str) -> None:
    assert segment(value) == value


@pytest.mark.parametrize(
    "value",
    ["", "..", "../carts/cart_1", "a/b", "a?b=1", "ord_1#x", "ord\x1f1", "ord 1", "x" * 65, "ord_%2F"],
)
def test_anything_else_is_a_404_not_a_500(value: str) -> None:
    with pytest.raises(NotFound):
        segment(value)


def test_unstorable_text_is_a_400_not_a_500() -> None:
    from commerce_common.errors import _is_unstorable_text

    assert _is_unstorable_text(Exception('invalid byte sequence for encoding "UTF8": 0x00'))
    assert not _is_unstorable_text(RuntimeError("connection reset"))


async def test_nul_bytes_reaching_the_database_are_a_clean_400() -> None:
    """Regression (API fuzzing): a NUL byte made PostgreSQL raise; the client got a 500 and a crash log."""
    import httpx
    from fastapi import FastAPI
    from sqlalchemy.exc import DBAPIError

    from commerce_common.errors import install_error_handlers

    app = FastAPI()
    install_error_handlers(app)

    @app.get("/boom")
    async def boom() -> None:
        raise DBAPIError("SELECT 1", {}, Exception('invalid byte sequence for encoding "UTF8": 0x00'))

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        response = await client.get("/boom")  # raise_app_exceptions stays on: a re-raise would fail here
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_characters"
