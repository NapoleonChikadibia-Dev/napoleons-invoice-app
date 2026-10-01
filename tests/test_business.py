import re
import sqlite3
from io import BytesIO

import pytest

from conftest import login_owner, login_worker


def db():
    import db as dbmod
    conn = sqlite3.connect(dbmod.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def make_invoice(client, **over):
    data = {"customer_id": "1", "product_id": "1", "quantity": "2", "paid_now": "full", "pay_method": "cash"}
    data.update(over)
    r = client.post("/invoice/create", data=data)
    return int(r.headers["Location"].rsplit("/", 1)[1])


def paid(inv_id):
    return db().execute("SELECT COALESCE(SUM(amount_cents), 0) FROM payments WHERE invoice_id = ?", (inv_id,)).fetchone()[0]


# ---------- Payments ----------

def test_paid_in_full_is_the_default(company):
    c = company.app.test_client(); login_owner(c)
    inv = make_invoice(c)
    assert paid(inv) == 170000
    assert "Paid in full. Thank you" in c.get(f"/invoice/{inv}").get_data(as_text=True)


def test_not_yet_paid_shows_as_owed(company):
    c = company.app.test_client(); login_owner(c)
    inv = make_invoice(c, paid_now="none", due_in_days="7")
    assert paid(inv) == 0
    dash = c.get("/").get_data(as_text=True)
    assert "₦1,700.00" in dash and "Who owes you" in dash and "Ada" in dash
    assert "Owes ₦1,700.00" in c.get("/customers").get_data(as_text=True)


def test_part_payment(company):
    c = company.app.test_client(); login_owner(c)
    inv = make_invoice(c, paid_now="part", paid_amount="500")
    assert paid(inv) == 50000
    page = c.get(f"/invoice/{inv}").get_data(as_text=True)
    assert "Part-paid" in page and "₦1,200.00" in page


@pytest.mark.parametrize("amount", ["0", "1700", "5000", "abc"])
def test_bad_part_payment_rejected(company, amount):
    c = company.app.test_client(); login_owner(c)
    r = c.post("/invoice/create", data={"customer_id": "1", "product_id": "1", "quantity": "2",
                                        "paid_now": "part", "paid_amount": amount})
    assert r.headers["Location"].endswith("/invoice/new")
    assert db().execute("SELECT COUNT(*) FROM invoices").fetchone()[0] == 0


def test_record_payments_until_paid(company):
    c = company.app.test_client(); login_worker(c)            # staff can record money received
    inv = make_invoice(c, paid_now="none")
    c.post(f"/invoice/{inv}/payments", data={"amount": "1,000", "method": "transfer"})
    assert paid(inv) == 100000
    c.post(f"/invoice/{inv}/payments", data={"amount": "800", "method": "cash"})   # more than balance
    assert paid(inv) == 100000
    c.post(f"/invoice/{inv}/payments", data={"amount": "700", "method": "cash"})
    assert paid(inv) == 170000
    assert "Fully paid" in c.get(f"/invoice/{inv}").get_data(as_text=True)


def test_only_owner_removes_payments(company):
    c = company.app.test_client(); login_worker(c)
    inv = make_invoice(c)
    pid = db().execute("SELECT id FROM payments WHERE invoice_id = ?", (inv,)).fetchone()[0]
    c.post(f"/payments/{pid}/delete")
    assert paid(inv) == 170000
    o = company.app.test_client(); login_owner(o)
    o.post(f"/payments/{pid}/delete")
    assert paid(inv) == 0


def test_overdue(company):
    c = company.app.test_client(); login_owner(c)
    inv = make_invoice(c, paid_now="none")
    conn = db(); conn.execute("UPDATE invoices SET due_date = date('now', '-3 days')"); conn.commit()
    assert "Overdue" in c.get(f"/invoice/{inv}").get_data(as_text=True)
    assert "1 overdue" in c.get("/").get_data(as_text=True)


# ---------- Business details ----------

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def test_business_details_on_invoice(company, tmp_path, monkeypatch):
    monkeypatch.setattr(company, "LOGO_DIR", str(tmp_path))
    c = company.app.test_client(); login_owner(c)
    c.post("/settings/business", data={"phone": "0803 111 2222", "address": "Mile 1, PH", "bank_name": "GTBank",
                                       "account_name": "Acme Ltd", "account_number": "0123-456-789",
                                       "logo": (BytesIO(PNG), "logo.png")}, content_type="multipart/form-data")
    row = db().execute("SELECT * FROM companies WHERE id = 1").fetchone()
    assert row["account_number"] == "0123456789" and row["logo_file"].endswith(".png")
    assert (tmp_path / row["logo_file"]).exists()
    unpaid = make_invoice(c, paid_now="none")
    page = c.get(f"/invoice/{unpaid}").get_data(as_text=True)
    assert "0123456789" in page and "GTBank" in page and "Mile 1, PH" in page and row["logo_file"] in page
    paid_inv = make_invoice(c)
    assert "0123456789" not in c.get(f"/invoice/{paid_inv}").get_data(as_text=True)   # nothing to pay


def test_fake_image_rejected(company, tmp_path, monkeypatch):
    logos = tmp_path / "logos"
    monkeypatch.setattr(company, "LOGO_DIR", str(logos))
    c = company.app.test_client(); login_owner(c)
    c.post("/settings/business", data={"logo": (BytesIO(b"<script>alert(1)</script>"), "logo.png")},
           content_type="multipart/form-data")
    assert db().execute("SELECT logo_file FROM companies WHERE id = 1").fetchone()[0] == ""
    assert not logos.exists() or list(logos.iterdir()) == []


def test_worker_cannot_change_business_details(company):
    c = company.app.test_client(); login_worker(c)
    c.post("/settings/business", data={"account_number": "999"})
    assert db().execute("SELECT account_number FROM companies WHERE id = 1").fetchone()[0] == ""


# ---------- WhatsApp / public link ----------

def test_public_link_works_without_login(company):
    c = company.app.test_client(); login_owner(c)
    c.post("/settings/business", data={"bank_name": "GTBank", "account_number": "0123456789"},
           content_type="multipart/form-data")
    inv = make_invoice(c, paid_now="none")
    page = c.get(f"/invoice/{inv}").get_data(as_text=True)
    link = re.search(r'data-copy="(http[^"]+/i/[^"]+)"', page).group(1)
    path = link.split("localhost", 1)[1]
    guest = company.app.test_client()
    r = guest.get(path)
    body = r.get_data(as_text=True)
    assert r.status_code == 200 and "0123456789" in body and "Rice" in body
    assert r.headers["X-Robots-Tag"] == "noindex"
    assert "Payments" not in body and "Record payment" not in body           # no admin bits
    assert guest.get("/i/not-a-real-token-xxxxxxxxxx").status_code == 404


def test_cancelled_invoice_link_stops_working(company):
    c = company.app.test_client(); login_owner(c)
    inv = make_invoice(c, paid_now="none")
    page = c.get(f"/invoice/{inv}").get_data(as_text=True)
    path = re.search(r'data-copy="http://localhost(/i/[^"]+)"', page).group(1)
    c.post(f"/invoice/{inv}/cancel")
    assert company.app.test_client().get(path).status_code == 404


def test_whatsapp_button_uses_customer_number(company):
    conn = db(); conn.execute("UPDATE customers SET phone = '0803 123 4567' WHERE id = 1"); conn.commit()
    c = company.app.test_client(); login_owner(c)
    inv = make_invoice(c, paid_now="none")
    page = c.get(f"/invoice/{inv}").get_data(as_text=True)
    m = re.search(r'href="https://wa.me/(\d*)\?text=([^"]+)"', page)
    assert m.group(1) == "2348031234567"
    assert "Balance%20due" in m.group(2) and "/i/" in m.group(2)


def test_customer_without_email(company):
    c = company.app.test_client(); login_owner(c)
    r = c.post("/invoice/create", data={"customer_id": "__new__", "new_customer_name": "Chidi",
                                        "new_customer_phone": "08031112222", "product_id": "1",
                                        "quantity": "1", "paid_now": "none"})
    assert "/invoice/" in r.headers["Location"]
    assert db().execute("SELECT email FROM customers WHERE name = 'Chidi'").fetchone()[0] == ""


@pytest.mark.parametrize("raw,expected", [("0803 123 4567", "2348031234567"), ("+234 803 123 4567", "2348031234567"),
                                          ("", ""), ("123", "")])
def test_whatsapp_number(company, raw, expected):
    assert company.whatsapp_number(raw) == expected


# ---------- Cost & profit ----------

def test_profit_on_dashboard(company):
    c = company.app.test_client(); login_owner(c)
    c.post("/products/1/edit", data={"name": "Rice", "price": "850", "cost": "600"})
    make_invoice(c)                                       # 2 × (850 - 600) = 500
    assert "₦500.00" in c.get("/").get_data(as_text=True)
    c.post("/products/1/edit", data={"name": "Rice", "price": "850", "cost": "800"})
    assert "₦500.00" in c.get("/").get_data(as_text=True)    # past sale keeps its cost


def test_worker_cannot_see_or_set_cost(company):
    c = company.app.test_client(); login_worker(c)
    c.post("/products/add", data={"name": "Beans", "price": "100", "stock": "5", "cost": "50"})
    assert db().execute("SELECT cost_cents FROM products WHERE name = 'Beans'").fetchone()[0] is None
    assert "Profit this month" not in c.get("/").get_data(as_text=True)
    assert "Cost price" not in c.get("/products").get_data(as_text=True)
