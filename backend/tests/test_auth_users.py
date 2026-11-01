from .conftest import OWNER, login


def test_login_ok_and_me(client):
    headers = login(client, **OWNER)
    me = client.get("/api/auth/me", headers=headers).json()
    assert me["email"] == OWNER["email"]
    assert me["role"] == "owner"
    assert me["can_apply_changes"] is True
    assert "password" not in me and "password_hash" not in me


def test_login_wrong_password(client):
    resp = client.post("/api/auth/login", json={"email": OWNER["email"], "password": "wrong-password"})
    assert resp.status_code == 401


def test_endpoints_require_login(client):
    for path in ("/api/auth/me", "/api/users", "/api/integrations", "/api/chat/history"):
        assert client.get(path).status_code == 401, path


def test_garbage_token_rejected(client):
    resp = client.get("/api/auth/me", headers={"Authorization": "Bearer not-a-token"})
    assert resp.status_code == 401


def test_employee_can_add_employee_but_not_grant_rights(client, owner):
    created = client.post(
        "/api/users", headers=owner, json={"email": "a@test.local", "password": "password-aaa-1"}
    )
    assert created.status_code == 201
    emp = login(client, "a@test.local", "password-aaa-1")

    ok = client.post("/api/users", headers=emp, json={"email": "b@test.local", "password": "password-bbb-1"})
    assert ok.status_code == 201

    escalate = client.post(
        "/api/users",
        headers=emp,
        json={"email": "c@test.local", "password": "password-ccc-1", "can_apply_changes": True},
    )
    assert escalate.status_code == 403

    uid = created.json()["id"]
    patch = client.patch(f"/api/users/{uid}", headers=emp, json={"role": "owner"})
    assert patch.status_code == 403


def test_owner_grants_apply_right(client, owner):
    uid = client.post(
        "/api/users", headers=owner, json={"email": "a@test.local", "password": "password-aaa-1"}
    ).json()["id"]
    resp = client.patch(f"/api/users/{uid}", headers=owner, json={"can_apply_changes": True})
    assert resp.status_code == 200
    assert resp.json()["can_apply_changes"] is True


def test_duplicate_email_conflict(client, owner):
    body = {"email": "a@test.local", "password": "password-aaa-1"}
    assert client.post("/api/users", headers=owner, json=body).status_code == 201
    assert client.post("/api/users", headers=owner, json={**body, "email": "A@Test.local"}).status_code == 409


def test_short_password_rejected(client, owner):
    resp = client.post("/api/users", headers=owner, json={"email": "a@test.local", "password": "short"})
    assert resp.status_code == 422


def test_cannot_remove_last_owner(client, owner):
    me_id = client.get("/api/auth/me", headers=owner).json()["id"]
    assert client.patch(f"/api/users/{me_id}", headers=owner, json={"role": "employee"}).status_code == 400
    assert client.patch(f"/api/users/{me_id}", headers=owner, json={"is_active": False}).status_code == 400


def test_employee_cannot_change_other_password(client, owner):
    a = client.post("/api/users", headers=owner, json={"email": "a@test.local", "password": "password-aaa-1"}).json()
    client.post("/api/users", headers=owner, json={"email": "b@test.local", "password": "password-bbb-1"})
    emp_b = login(client, "b@test.local", "password-bbb-1")
    resp = client.patch(f"/api/users/{a['id']}", headers=emp_b, json={"password": "hacked-password-1"})
    assert resp.status_code == 403


def test_deactivated_user_loses_access(client, owner):
    uid = client.post(
        "/api/users", headers=owner, json={"email": "a@test.local", "password": "password-aaa-1"}
    ).json()["id"]
    emp = login(client, "a@test.local", "password-aaa-1")
    client.patch(f"/api/users/{uid}", headers=owner, json={"is_active": False})
    assert client.get("/api/auth/me", headers=emp).status_code == 401
    resp = client.post("/api/auth/login", json={"email": "a@test.local", "password": "password-aaa-1"})
    assert resp.status_code == 401
