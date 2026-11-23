"""Load csu.py without Home Assistant (the package __init__ imports it)."""

import importlib
import json
import sys
import time
import types
from collections import defaultdict
from pathlib import Path

import pytest

COMPONENT = Path(__file__).resolve().parent.parent / "custom_components" / "csu"
ACCOUNTS_URL = "https://myaccount.csu.org/rest/account/list/"

# Fake values only: nothing here is, or resembles, a real credential.
USERNAME = "user@example.test"
PASSWORD = "fake-password"
CUSTOMER_ID = "fake-customer"
SAVED_TOKEN = "fake-saved-token"
NEW_TOKEN = "fake-new-token"


class FakeResponse:
    def __init__(self, status=200, payload=None, body=None):
        self.status = status
        self._body = body if body is not None else json.dumps(payload)

    async def json(self, content_type=None):
        return json.loads(self._body)  # raises ValueError for non-JSON bodies


class _Call:
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        if isinstance(self._response, Exception):
            raise self._response
        return self._response

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    """Stands in for aiohttp.ClientSession: queued replies per URL, no network."""

    def __init__(self):
        self._queued = defaultdict(list)
        self.calls = defaultdict(int)
        self.bodies = defaultdict(list)

    def reply(self, url, status=200, payload=None, body=None):
        self._queued[url].append(FakeResponse(status, payload, body))

    def fail(self, url, exc):
        self._queued[url].append(exc)

    def post(self, url, **kwargs):
        self.calls[url] += 1
        data = kwargs.get("data")
        self.bodies[url].append(json.loads(data) if isinstance(data, str) else None)
        if not self._queued[url]:
            raise AssertionError(f"unexpected request to {url}")
        return _Call(self._queued[url].pop(0))


@pytest.fixture(scope="session")
def mod():
    pkg = types.ModuleType("csu_pkg")
    pkg.__path__ = [str(COMPONENT)]
    sys.modules["csu_pkg"] = pkg
    csu = importlib.import_module("csu_pkg.csu")
    exceptions = importlib.import_module("csu_pkg.exceptions")
    return types.SimpleNamespace(
        CSU=csu.CSU, TOKEN_URL=csu.TOKEN_URL, errors=exceptions, csu=csu
    )


@pytest.fixture
def session():
    return FakeSession()


def token_ok(session, mod):
    session.reply(
        mod.TOKEN_URL,
        payload={
            "access_token": NEW_TOKEN,
            "expires_in": 3600,
            "user": {"customerId": CUSTOMER_ID},
        },
    )


def accounts_ok(session):
    session.reply(ACCOUNTS_URL, payload={"account": [{"accountNumber": "fake-acct"}]})


def make_api(mod, session, *, token="", expires_in=0):
    return mod.CSU(
        session,
        USERNAME,
        PASSWORD,
        access_token=token,
        token_expires_at=time.time() + expires_in,
        customer_id=CUSTOMER_ID if token else "",
    )
