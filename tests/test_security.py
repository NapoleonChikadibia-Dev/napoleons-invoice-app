import os
import sqlite3
import subprocess
import sys

from conftest import ROOT, login_owner, login_worker


# ---------- SECRET_KEY / cookies ----------

def _start_app(env_extra):
    env = {k: v for k, v in os.environ.items() if k not in ("SECRET_KEY", "APP_ENV")}
    env.update(env_extra)
    code = ("import env; env.load_local_env = lambda: None\n"
            "import db, tempfile, os; db.DB_PATH = os.path.join(tempfile.mkdtemp(), 'x.db')\n"
            "import app")
    return subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True)


def test_production_refuses_placeholder_secret_key():
    result = _start_app({"APP_ENV": "production",
                         "SECRET_KEY": "pick-any-long-random-string-of-letters-and-numbers"})
    assert result.returncode != 0
    assert "SECRET_KEY" in result.stderr


def test_production_refuses_missing_secret_key():
    assert _start_app({"APP_ENV": "production"}).returncode != 0


def test_production_starts_with_real_key():
    assert _start_app({"APP_ENV": "production", "SECRET_KEY": "a" * 64}).returncode == 0


def test_local_still_starts_without_key():
    assert _start_app({}).returncode == 0


def test_cookie_flags(app_module):
    cfg = app_module.app.config
    assert cfg["SESSION_COOKIE_HTTPONLY"] is True
    assert cfg["SESSION_COOKIE_SAMESITE"] == "Lax"


# ---------- Login lockout ----------

def test_owner_locked_after_5_wrong_passwords(company):
    c = company.app.test_client()
    for _ in range(5):
        r = login_owner(c, "wrong-password")
        assert r.headers["Location"].startswith("/login")
    # Even the right password is refused while locked.
    r = login_owner(c)
    assert r.headers["Location"].startswith("/login")
    assert "Too many wrong attempts" in c.get("/login").get_data(as_text=True)


def test_lock_expires_after_window(company):
    import db
    c = company.app.test_client()
    for _ in range(5):
        login_owner(c, "wrong-password")
    conn = sqlite3.connect(db.DB_PATH)
    conn.execute("UPDATE login_failures SET created_at = datetime('now', '-16 minutes')")
    conn.commit()
    assert login_owner(c).headers["Location"] == "/"


def test_success_resets_the_counter(company):
    c = company.app.test_client()
    for _ in range(4):
        login_owner(c, "wrong-password")
    assert login_owner(c).headers["Location"] == "/"
    c.post("/logout")
    for _ in range(4):
        login_owner(c, "wrong-password")
    assert login_owner(c).headers["Location"] == "/"


def test_team_login_locked_after_5_wrong(company):
    c = company.app.test_client()
    for _ in range(5):
        login_worker(c, "nope")
    assert login_worker(c).headers["Location"].startswith("/worker-login")


# ---------- Proxy header ----------

def test_forwarded_ip_ignored_unless_trusted(company):
    with company.app.test_request_context("/", headers={"X-Forwarded-For": "1.2.3.4"},
                                          environ_base={"REMOTE_ADDR": "9.9.9.9"}):
        assert company.client_ip() == "9.9.9.9"
    company.TRUST_PROXY = True
    with company.app.test_request_context("/", headers={"X-Forwarded-For": "1.2.3.4"},
                                          environ_base={"REMOTE_ADDR": "9.9.9.9"}):
        assert company.client_ip() == "1.2.3.4"


# ---------- Owner-only actions ----------

def _make_invoice(client, paid_now="none"):
    r = client.post("/invoice/create", data={"customer_id": "1", "product_id": "1", "quantity": "2",
                                             "paid_now": paid_now})
    return int(r.headers["Location"].rsplit("/", 1)[1])


def test_worker_cannot_delete_product(company):
    import db
    c = company.app.test_client()
    login_worker(c)
    c.post("/products/delete/1")
    assert sqlite3.connect(db.DB_PATH).execute("SELECT COUNT(*) FROM products WHERE id = 1").fetchone()[0] == 1
    assert "Delete Rice" not in c.get("/products").get_data(as_text=True)


def test_worker_cannot_cancel_invoice(company):
    import db
    c = company.app.test_client()
    login_worker(c)
    inv = _make_invoice(c)
    c.post(f"/invoice/{inv}/cancel")
    conn = sqlite3.connect(db.DB_PATH)
    assert conn.execute("SELECT status FROM invoices WHERE id = ?", (inv,)).fetchone()[0] == "active"
    assert conn.execute("SELECT stock FROM products WHERE id = 1").fetchone()[0] == 8
    assert "Cancel invoice" not in c.get(f"/invoice/{inv}").get_data(as_text=True)


def test_owner_can_cancel_and_stock_returns(company):
    import db
    c = company.app.test_client()
    login_owner(c)
    inv = _make_invoice(c)
    c.post(f"/invoice/{inv}/cancel")
    conn = sqlite3.connect(db.DB_PATH)
    assert conn.execute("SELECT status FROM invoices WHERE id = ?", (inv,)).fetchone()[0] == "cancelled"
    assert conn.execute("SELECT stock FROM products WHERE id = 1").fetchone()[0] == 10


def test_owner_can_delete_unused_product(company):
    import db
    c = company.app.test_client()
    login_owner(c)
    c.post("/products/delete/1")
    assert sqlite3.connect(db.DB_PATH).execute("SELECT COUNT(*) FROM products").fetchone()[0] == 0


# ---------- Things that must keep working ----------

def test_main_pages_render(company):
    c = company.app.test_client()
    login_owner(c)
    for url in ["/", "/products", "/customers", "/invoices", "/invoice/new",
                "/low-stock", "/reports", "/team", "/settings"]:
        assert c.get(url).status_code == 200, url


def test_landing_page_for_visitors(company):
    r = company.app.test_client().get("/")
    assert r.status_code == 200 and "Never lose track of stock" in r.get_data(as_text=True)


def test_cannot_cancel_invoice_with_payments(company):
    import db
    c = company.app.test_client()
    login_owner(c)
    inv = _make_invoice(c, paid_now="full")
    c.post(f"/invoice/{inv}/cancel")
    assert sqlite3.connect(db.DB_PATH).execute("SELECT status FROM invoices WHERE id = ?", (inv,)).fetchone()[0] == "active"
