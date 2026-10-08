import os
import sys
import tempfile
from pathlib import Path

# Use a throwaway database and no AI key for tests. Must happen before the app is imported.
_tmp = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}/test.db"
os.environ["LLM_API_KEY"] = ""
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def make_user(client):
    """Register a user and return the Cookie header that logs in as them."""
    counter = {"n": 0}

    def _make(prefix: str) -> dict:
        counter["n"] += 1
        username = f"{prefix}{os.getpid()}x{counter['n']}x{id(counter) % 10000}"
        r = client.post("/api/auth/register", json={"username": username, "password": "secret123"})
        assert r.status_code == 201, r.text
        token = r.cookies["access_token"]
        client.cookies.clear()
        return {"username": username, "id": r.json()["id"], "headers": {"cookie": f"access_token={token}"}}

    return _make
