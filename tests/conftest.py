import itertools
import os
import sys
import tempfile
from pathlib import Path

# Use a throwaway database and no AI key for tests. Must happen before the app is imported.
_tmp = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}/test.db"
os.environ["LLM_API_KEY"] = ""
os.environ["MESSAGE_KEY"] = "dGVzdC1rZXktZm9yLXVobHdhdmUtdGVzdHMtMzJieXQ="  # fixed 32-byte test key
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as c:
        yield c


_user_numbers = itertools.count(1)


@pytest.fixture
def make_user(client):
    """Register a user and return the Cookie header that logs in as them."""
    def _make(prefix: str) -> dict:
        username = f"{prefix}{next(_user_numbers)}"  # unique across the whole test run
        r = client.post("/api/auth/register", json={"username": username, "password": "secret123"})
        assert r.status_code == 201, r.text
        token = r.cookies["access_token"]
        client.cookies.clear()
        return {"username": username, "id": r.json()["id"], "headers": {"cookie": f"access_token={token}"}}

    return _make
