"""One username per person: case-insensitive, reserved names, live availability check."""


def check(client, name, headers=None):
    r = client.get("/api/auth/username-available", params={"username": name}, headers=headers or {})
    assert r.status_code == 200, r.text
    return r.json()


def test_availability_check(client, make_user):
    owner = make_user("riya_avail")
    name = owner["username"]

    taken = check(client, name.upper())  # case doesn't matter: "RIYA" is "riya"
    assert taken["available"] is False and "taken" in taken["reason"]
    assert taken["username"] == name
    assert len(taken["suggestions"]) >= 1
    for s in taken["suggestions"]:
        assert check(client, s)["available"] is True

    free = check(client, "nobody_has_this_name")
    assert free == {"username": "nobody_has_this_name", "available": True, "yours": False, "reason": None, "suggestions": []}

    assert check(client, "admin")["available"] is False
    assert "reserved" in check(client, "UhlWave")["reason"]
    assert check(client, "ab")["available"] is False
    assert check(client, "bad name!")["reason"] == "Use only letters, numbers and underscores."

    # Logged in, your own name counts as available (for the Settings form).
    mine = check(client, name, headers=owner["headers"])
    assert mine["available"] is True and mine["yours"] is True


def test_register_refuses_duplicates_in_any_case(client, make_user):
    owner = make_user("dup_case")
    r = client.post("/api/auth/register", json={"username": owner["username"].upper(), "password": "Cedar-Monsoon-42"})
    assert r.status_code == 409 and "taken" in r.json()["detail"]
    r = client.post("/api/auth/register", json={"username": "Admin", "password": "Cedar-Monsoon-42"})
    assert r.status_code == 409 and "reserved" in r.json()["detail"]
    client.cookies.clear()


def test_change_username_rules(client, make_user):
    a, b = make_user("rename_a"), make_user("rename_b")
    put = lambda name: client.put("/api/settings/username", json={"username": name}, headers=a["headers"])
    assert put(b["username"].upper()).status_code == 409   # someone else's name, any case
    assert put("support").status_code == 409                # reserved
    assert put(a["username"].upper()).status_code == 200    # your own name, new case: fine (stays lowercase)
    r = put("Fresh_Name_42")
    assert r.status_code == 200 and r.json()["username"] == "fresh_name_42"
    assert check(client, "fresh_name_42")["available"] is False


def test_availability_is_rate_limited(client):
    from app import auth
    auth._name_checks.clear()
    codes = [client.get("/api/auth/username-available", params={"username": f"probe{i}"}).status_code
             for i in range(auth.NAME_CHECKS_PER_MINUTE + 1)]
    assert codes[:-1] == [200] * auth.NAME_CHECKS_PER_MINUTE and codes[-1] == 429
    auth._name_checks.clear()
