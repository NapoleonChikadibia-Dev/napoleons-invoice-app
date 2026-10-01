import re
import sqlite3

from conftest import login_owner


def _signup(client, **over):
    data = {"company_name": "Ada Stores", "email": "ada@shop.ng", "password": "secret123", "currency": "NGN"}
    data.update(over)
    return client.post("/signup", data=data)


def test_one_screen_signup_logs_you_straight_in(app_module, outbox):
    c = app_module.app.test_client()
    r = _signup(c)
    assert r.status_code == 302 and r.headers["Location"] == "/"
    page = c.get("/").get_data(as_text=True)
    assert "Ada Stores" in page
    assert "Please verify" in page                      # banner, not a blocker
    assert outbox and "verify-email" in outbox[0]["body"]


def test_signup_needs_no_passcode_or_passphrase(app_module):
    import db
    c = app_module.app.test_client()
    _signup(c)
    row = sqlite3.connect(db.DB_PATH).execute(
        "SELECT companies.passcode_hash, users.recovery_hash, companies.currency FROM users "
        "JOIN companies ON companies.id = users.company_id WHERE email = 'ada@shop.ng'").fetchone()
    assert row == ("", "", "NGN")


def test_verifying_removes_the_banner(app_module, outbox):
    c = app_module.app.test_client()
    _signup(c)
    link = re.search(r"/verify-email/\S+", outbox[0]["body"]).group(0)
    c.get(link)
    assert "Please verify" not in c.get("/").get_data(as_text=True)


def test_signup_validation_keeps_what_you_typed(app_module):
    r = _signup(app_module.app.test_client(), password="short")
    body = r.get_data(as_text=True)
    assert r.status_code == 400 and "8 characters" in body and 'value="Ada Stores"' in body


def test_duplicate_business_name_rejected(app_module):
    c = app_module.app.test_client()
    _signup(c)
    c.post("/logout")
    r = _signup(c, email="other@shop.ng", company_name="ada stores")
    assert r.status_code == 400 and "already taken" in r.get_data(as_text=True)


def test_unverified_owner_can_still_log_in(app_module):
    c = app_module.app.test_client()
    _signup(c)
    c.post("/logout")
    r = c.post("/login", data={"email": "ada@shop.ng", "password": "secret123"})
    assert r.headers["Location"] == "/"


# ---------- Password reset by email ----------

def test_reset_link_flow(company, outbox):
    c = company.app.test_client()
    c.post("/forgot-password", data={"email": "owner@acme.ng"})
    assert len(outbox) == 1
    link = re.search(r"/reset-password/\S+", outbox[0]["body"]).group(0)
    assert c.get(link).status_code == 200
    c.post(link, data={"password": "brandnew99", "confirm_password": "brandnew99"})
    assert login_owner(c, "brandnew99").headers["Location"] == "/"


def test_reset_link_works_only_once(company, outbox):
    c = company.app.test_client()
    c.post("/forgot-password", data={"email": "owner@acme.ng"})
    link = re.search(r"/reset-password/\S+", outbox[0]["body"]).group(0)
    c.post(link, data={"password": "brandnew99", "confirm_password": "brandnew99"})
    r = c.get(link)
    assert r.status_code == 302 and r.headers["Location"].endswith("/forgot-password")


def test_reset_link_expires(company, outbox):
    import db
    c = company.app.test_client()
    c.post("/forgot-password", data={"email": "owner@acme.ng"})
    link = re.search(r"/reset-password/\S+", outbox[0]["body"]).group(0)
    conn = sqlite3.connect(db.DB_PATH)
    conn.execute("UPDATE users SET reset_token_expires = datetime('now', '-1 minute')")
    conn.commit()
    assert c.get(link).status_code == 302


def test_unknown_email_gets_same_message_and_no_email(company, outbox):
    c = company.app.test_client()
    r = c.post("/forgot-password", data={"email": "nobody@nowhere.ng"}, follow_redirects=True)
    assert "If nobody@nowhere.ng has an account" in r.get_data(as_text=True)
    assert outbox == []


def test_reset_requests_are_capped(company, outbox):
    c = company.app.test_client()
    for _ in range(8):
        c.post("/forgot-password", data={"email": "owner@acme.ng"})
    assert len(outbox) == 5


def test_reset_token_stored_hashed(company, outbox):
    import db
    c = company.app.test_client()
    c.post("/forgot-password", data={"email": "owner@acme.ng"})
    token = re.search(r"/reset-password/(\S+)", outbox[0]["body"]).group(1)
    stored = sqlite3.connect(db.DB_PATH).execute("SELECT reset_token_hash FROM users WHERE id = 1").fetchone()[0]
    assert stored and token not in stored
