"""Shared test setup: every test gets a fresh, empty database file."""
import importlib
import os
import sqlite3
import sys

import pytest
from werkzeug.security import generate_password_hash

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


@pytest.fixture
def app_module(tmp_path, monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "t" * 64)
    for name in ("SMTP_HOST", "SMTP_FROM", "SMTP_USERNAME", "SMTP_PASSWORD", "APP_ENV", "TRUST_PROXY"):
        monkeypatch.delenv(name, raising=False)
    import env
    monkeypatch.setattr(env, "load_local_env", lambda: None)
    import db
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test.db"))
    sys.modules.pop("app", None)
    module = importlib.import_module("app")
    module.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    return module


@pytest.fixture
def company(app_module):
    """One company with an owner, a worker, a product and an invoice."""
    import db
    conn = sqlite3.connect(db.DB_PATH)
    conn.execute("INSERT INTO companies (id, name, passcode_hash, currency) VALUES (1, 'Acme', ?, 'NGN')",
                 (generate_password_hash("passcode1"),))
    conn.execute("INSERT INTO users (id, email, password_hash, recovery_hash, company_id, role, email_verified, onboarded) "
                 "VALUES (1, 'owner@acme.ng', ?, ?, 1, 'owner', 1, 1)",
                 (generate_password_hash("ownerpass1"), generate_password_hash("my recovery words")))
    conn.execute("INSERT INTO users (id, email, password_hash, recovery_hash, company_id, role, email_verified, onboarded) "
                 "VALUES (2, 'worker@acme.ng', ?, '', 1, 'worker', 1, 1)",
                 (generate_password_hash("workerpass1"),))
    conn.execute("INSERT INTO products (id, name, price, price_cents, stock, company_id) VALUES (1, 'Rice', 850, 85000, 10, 1)")
    conn.execute("INSERT INTO customers (id, name, email, company_id) VALUES (1, 'Ada', 'ada@x.ng', 1)")
    conn.commit()
    conn.close()
    return app_module


def login_owner(client, password="ownerpass1"):
    return client.post("/login", data={"email": "owner@acme.ng", "password": password})


def login_worker(client, password="workerpass1"):
    return client.post("/worker-login", data={"company_name": "Acme", "company_passcode": "passcode1",
                                              "username": "worker@acme.ng", "password": password})


@pytest.fixture
def outbox(app_module, monkeypatch):
    """Pretend email is configured and capture every email instead of sending."""
    sent = []
    for k, v in {"SMTP_HOST": "x", "SMTP_FROM": "app@x.ng", "SMTP_USERNAME": "u", "SMTP_PASSWORD": "p"}.items():
        monkeypatch.setenv(k, v)

    def fake_send(to, subject, body):
        sent.append({"to": to, "subject": subject, "body": body})
        return True
    monkeypatch.setattr(app_module, "send_plain_email", fake_send)
    return sent
