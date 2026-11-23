"""Auth behaviour of the CSU client, with all HTTP mocked."""

import pytest
from conftest import ACCOUNTS_URL
from conftest import accounts_ok
from conftest import CUSTOMER_ID
from conftest import make_api
from conftest import NEW_TOKEN
from conftest import SAVED_TOKEN
from conftest import token_ok


async def test_valid_saved_token_skips_login(mod, session):
    accounts_ok(session)
    api = make_api(mod, session, token=SAVED_TOKEN, expires_in=3000)

    await api.async_login()

    assert session.calls[mod.TOKEN_URL] == 0
    assert session.calls[ACCOUNTS_URL] == 1
    assert api.access_token == SAVED_TOKEN
    assert api.customers[0].customer_id == CUSTOMER_ID


async def test_expired_saved_token_logs_in_once(mod, session):
    token_ok(session, mod)
    accounts_ok(session)
    api = make_api(mod, session, token=SAVED_TOKEN, expires_in=-10)

    await api.async_login()

    assert session.calls[mod.TOKEN_URL] == 1
    assert session.calls[ACCOUNTS_URL] == 1
    assert api.access_token == NEW_TOKEN


async def test_rejected_saved_token_relogs_in_exactly_once(mod, session):
    session.reply(ACCOUNTS_URL, status=401, payload={"error": "invalid_token"})
    token_ok(session, mod)
    accounts_ok(session)
    api = make_api(mod, session, token=SAVED_TOKEN, expires_in=3000)

    await api.async_login()

    assert session.calls[mod.TOKEN_URL] == 1
    assert session.calls[ACCOUNTS_URL] == 2
    assert api.access_token == NEW_TOKEN
    assert api.customers


async def test_rejected_fresh_token_raises_without_retry(mod, session):
    token_ok(session, mod)
    session.reply(ACCOUNTS_URL, status=401, payload={"error": "invalid_token"})
    api = make_api(mod, session)

    with pytest.raises(mod.errors.InvalidAuth):
        await api.async_login()

    assert session.calls[mod.TOKEN_URL] == 1
    assert session.calls[ACCOUNTS_URL] == 1


async def test_mfa_challenge_parses_token(mod, session):
    session.reply(
        mod.TOKEN_URL,
        status=400,
        payload={
            "error": "invalid_grant",
            "error_description": "BUS-MFAR:fake-challenge",
        },
    )
    api = make_api(mod, session)

    with pytest.raises(mod.errors.MfaRequired) as err:
        await api.async_login()

    assert err.value.mfa_token == "fake-challenge"
    assert session.calls[ACCOUNTS_URL] == 0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"status": 200, "payload": {"unexpected": "shape"}},
        {"status": 500, "body": "<html>Bad gateway</html>"},
        {"status": 200, "body": "not json at all"},
    ],
    ids=["json-without-account", "http-500-html", "200-not-json"],
)
async def test_non_auth_garbage_is_cannot_connect(mod, session, kwargs):
    session.reply(ACCOUNTS_URL, **kwargs)
    api = make_api(mod, session, token=SAVED_TOKEN, expires_in=3000)

    with pytest.raises(mod.errors.CannotConnect):
        await api.async_login()

    # An outage must not be mistaken for a bad token: no relogin attempt.
    assert session.calls[mod.TOKEN_URL] == 0
    assert session.calls[ACCOUNTS_URL] == 1


async def test_error_message_in_body_is_invalid_auth(mod, session):
    token_ok(session, mod)
    session.reply(ACCOUNTS_URL, payload={"errorMsg": "Session expired"})
    api = make_api(mod, session)

    with pytest.raises(mod.errors.InvalidAuth):
        await api.async_login()
