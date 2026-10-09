"""Google sign-in. Google itself is faked: we swap out the function that checks
the ID token, so these tests run offline and never talk to Google."""
import pytest

from app import auth


@pytest.fixture
def fake_google(monkeypatch):
    """Turn Google sign-in on and decide what a "credential" decodes to."""
    monkeypatch.setattr(auth, "GOOGLE_CLIENT_ID", "test-client.apps.googleusercontent.com")
    monkeypatch.setattr(auth, "GOOGLE_ALLOWED_DOMAIN", "")
    accounts = {}

    def verify(credential):
        if credential not in accounts:
            raise ValueError("bad token")
        return accounts[credential]

    monkeypatch.setattr(auth, "verify_google_credential", verify)
    return accounts


def google_login(client, credential):
    r = client.post("/api/auth/google", json={"credential": credential})
    return r


def test_config_reports_client_id(client, fake_google):
    assert client.get("/api/auth/config").json() == {"google_client_id": "test-client.apps.googleusercontent.com"}


def test_google_off_when_not_configured(client, monkeypatch):
    monkeypatch.setattr(auth, "GOOGLE_CLIENT_ID", "")
    assert client.get("/api/auth/config").json() == {"google_client_id": None}
    assert google_login(client, "anything").status_code == 503


def test_first_google_login_creates_account_then_reuses_it(client, fake_google):
    fake_google["tok-riya"] = {"sub": "g-1001", "email": "Riya.Sharma@gmail.com", "email_verified": True}

    r = google_login(client, "tok-riya")
    assert r.status_code == 201, r.text
    user = r.json()
    assert user["username"] == "riya_sharma"
    assert client.get("/api/auth/me").json()["id"] == user["id"]
    client.cookies.clear()

    again = google_login(client, "tok-riya")
    assert again.status_code == 200
    assert again.json()["id"] == user["id"]
    client.cookies.clear()

    # A Google-only account has no password, so password login never works for it.
    assert client.post("/api/auth/login", json={"username": "riya_sharma", "password": "!"}).status_code == 401


def test_username_clash_gets_a_number(client, fake_google):
    client.post("/api/auth/register", json={"username": "kabir", "password": "secret123"})
    client.cookies.clear()
    fake_google["tok-kabir"] = {"sub": "g-2002", "email": "kabir@gmail.com", "email_verified": True}
    r = google_login(client, "tok-kabir")
    assert r.status_code == 201
    assert r.json()["username"] == "kabir2"
    client.cookies.clear()


def test_rejects_bad_or_unverified_tokens(client, fake_google):
    assert google_login(client, "forged").status_code == 401
    fake_google["tok-unverified"] = {"sub": "g-3003", "email": "x@gmail.com", "email_verified": False}
    assert google_login(client, "tok-unverified").status_code == 401
    assert client.get("/api/auth/me").status_code == 401


def test_allowed_domain(client, fake_google, monkeypatch):
    monkeypatch.setattr(auth, "GOOGLE_ALLOWED_DOMAIN", "students.iitmandi.ac.in")
    fake_google["tok-outsider"] = {"sub": "g-4004", "email": "someone@gmail.com", "email_verified": True}
    fake_google["tok-student"] = {"sub": "g-5005", "email": "b23001@students.iitmandi.ac.in", "email_verified": True}
    assert google_login(client, "tok-outsider").status_code == 403
    assert google_login(client, "tok-student").status_code == 201
    client.cookies.clear()


def test_google_user_can_chat(client, fake_google, make_user):
    fake_google["tok-meera"] = {"sub": "g-6006", "email": "meera@gmail.com", "email_verified": True}
    meera = google_login(client, "tok-meera").json()
    client.cookies.clear()
    bob = make_user("bob")
    r = client.post("/api/conversations", json={"username": meera["username"]}, headers=bob["headers"])
    assert r.status_code == 200
    assert r.json()["title"] == meera["username"]
