import os
import sqlite3

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "inventory.db")


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row  # lets you access columns by name
    return conn


def create_tables():
    conn = get_connection()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS companies (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            price REAL NOT NULL,
            stock INTEGER NOT NULL DEFAULT 0,
            low_stock_threshold INTEGER NOT NULL DEFAULT 5
        );
        CREATE TABLE IF NOT EXISTS invoices (
            id INTEGER PRIMARY KEY,
            customer TEXT NOT NULL,
            date TEXT NOT NULL,
            total REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS customers (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            email TEXT NOT NULL,
            phone TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS invoice_items (
            id INTEGER PRIMARY KEY,
            invoice_id INTEGER REFERENCES invoices(id),
            product_id INTEGER REFERENCES products(id),
            quantity INTEGER NOT NULL,
            unit_price REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL
        );
    """)

    # --- Invoices: email, status, customer link ---
    invoice_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(invoices)").fetchall()
    }
    if "customer_email" not in invoice_columns:
        conn.execute("ALTER TABLE invoices ADD COLUMN customer_email TEXT NOT NULL DEFAULT ''")
    if "status" not in invoice_columns:
        conn.execute("ALTER TABLE invoices ADD COLUMN status TEXT NOT NULL DEFAULT 'active'")
    if "customer_id" not in invoice_columns:
        conn.execute("ALTER TABLE invoices ADD COLUMN customer_id INTEGER REFERENCES customers(id)")

    # --- Users: recovery passphrase ---
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS signup_attempts (
            id INTEGER PRIMARY KEY,
            ip_address TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
    """)

    user_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(users)").fetchall()
    }
    if "recovery_hash" not in user_columns:
        # Existing accounts (e.g. from the old .env-seeded admin) get an empty
        # recovery hash, meaning "forgot password" won't work for them until
        # they run reset_password.py once to set one.
        conn.execute("ALTER TABLE users ADD COLUMN recovery_hash TEXT NOT NULL DEFAULT ''")

    if "email_verified" not in user_columns:
        # Accounts that already existed before this feature are grandfathered
        # in as verified — they already proved control of their inbox by
        # actually using the app, and shouldn't get locked out retroactively.
        # Only NEW signups from here on start at 0 and have to click the link.
        conn.execute("ALTER TABLE users ADD COLUMN email_verified INTEGER NOT NULL DEFAULT 0")
        conn.execute("UPDATE users SET email_verified = 1")
        conn.execute("ALTER TABLE users ADD COLUMN verification_token TEXT")
        conn.execute("ALTER TABLE users ADD COLUMN verification_token_expires TEXT")

    # --- Money: move from REAL (float) to INTEGER cents ---
    product_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(products)").fetchall()
    }
    if "price_cents" not in product_columns:
        conn.execute("ALTER TABLE products ADD COLUMN price_cents INTEGER NOT NULL DEFAULT 0")
        conn.execute("UPDATE products SET price_cents = CAST(ROUND(price * 100) AS INTEGER)")

    invoice_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(invoices)").fetchall()
    }
    if "total_cents" not in invoice_columns:
        conn.execute("ALTER TABLE invoices ADD COLUMN total_cents INTEGER NOT NULL DEFAULT 0")
        conn.execute("UPDATE invoices SET total_cents = CAST(ROUND(total * 100) AS INTEGER)")

    item_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(invoice_items)").fetchall()
    }
    if "unit_price_cents" not in item_columns:
        conn.execute("ALTER TABLE invoice_items ADD COLUMN unit_price_cents INTEGER NOT NULL DEFAULT 0")
        conn.execute("UPDATE invoice_items SET unit_price_cents = CAST(ROUND(unit_price * 100) AS INTEGER)")

    # --- Multi-company accounts ---
    # Every user now belongs to exactly one company, with a role of 'owner'
    # or 'worker'. Products, customers, and invoices all get tagged with the
    # company they belong to; invoices also record which user created them.
    #
    # For a database that already had users/products/invoices before this
    # migration (i.e. an existing live single-company install), everything
    # gets folded into one auto-created "legacy" company, with the earliest
    # user promoted to owner. Nothing is deleted or renumbered — this only
    # adds columns and fills them in once.
    user_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(users)").fetchall()
    }
    legacy_company_id = None
    if "company_id" not in user_columns:
        pre_existing_user_ids = [
            row["id"] for row in conn.execute("SELECT id FROM users ORDER BY id").fetchall()
        ]

        conn.execute("ALTER TABLE users ADD COLUMN company_id INTEGER REFERENCES companies(id)")
        conn.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'worker'")

        if pre_existing_user_ids:
            cur = conn.execute(
                "INSERT INTO companies (name) VALUES (?)", ("My Company",))
            legacy_company_id = cur.lastrowid
            owner_id = pre_existing_user_ids[0]
            conn.execute(
                "UPDATE users SET company_id = ?, role = 'owner' WHERE id = ?",
                (legacy_company_id, owner_id))
            for worker_id in pre_existing_user_ids[1:]:
                conn.execute(
                    "UPDATE users SET company_id = ?, role = 'worker' WHERE id = ?",
                    (legacy_company_id, worker_id))

    product_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(products)").fetchall()
    }
    if "company_id" not in product_columns:
        conn.execute("ALTER TABLE products ADD COLUMN company_id INTEGER REFERENCES companies(id)")
        if legacy_company_id is not None:
            conn.execute("UPDATE products SET company_id = ?", (legacy_company_id,))

    customer_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(customers)").fetchall()
    }
    if "company_id" not in customer_columns:
        conn.execute("ALTER TABLE customers ADD COLUMN company_id INTEGER REFERENCES companies(id)")
        if legacy_company_id is not None:
            conn.execute("UPDATE customers SET company_id = ?", (legacy_company_id,))

    invoice_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(invoices)").fetchall()
    }
    if "company_id" not in invoice_columns:
        conn.execute("ALTER TABLE invoices ADD COLUMN company_id INTEGER REFERENCES companies(id)")
        conn.execute("ALTER TABLE invoices ADD COLUMN created_by INTEGER REFERENCES users(id)")
        if legacy_company_id is not None:
            legacy_owner = conn.execute(
                "SELECT id FROM users WHERE company_id = ? AND role = 'owner' LIMIT 1",
                (legacy_company_id,)).fetchone()
            owner_id = legacy_owner["id"] if legacy_owner else None
            conn.execute(
                "UPDATE invoices SET company_id = ?, created_by = ?",
                (legacy_company_id, owner_id))

    # --- Company passcode (shared secret workers use alongside their own
    # password) and a uniqueness guarantee on company name, since worker
    # login now looks a company up by name. ---
    company_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(companies)").fetchall()
    }
    if "passcode_hash" not in company_columns:
        conn.execute("ALTER TABLE companies ADD COLUMN passcode_hash TEXT NOT NULL DEFAULT ''")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_companies_name_lower ON companies (LOWER(name))")

    # --- When an invoice was last emailed (NULL = never) ---
    invoice_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(invoices)").fetchall()
    }
    if "emailed_at" not in invoice_columns:
        conn.execute("ALTER TABLE invoices ADD COLUMN emailed_at TEXT")

    # --- Has this user already been shown the welcome tour? ---
    # Existing users are marked as done (they already know the app); only
    # accounts created from now on start at 0 and see the tour once.
    user_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(users)").fetchall()
    }
    if "onboarded" not in user_columns:
        conn.execute("ALTER TABLE users ADD COLUMN onboarded INTEGER NOT NULL DEFAULT 0")
        conn.execute("UPDATE users SET onboarded = 1")
    # --- Each invoice line keeps the product name it was billed under, so
    # renaming a product later never changes invoices already sent. ---
    item_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(invoice_items)").fetchall()
    }
    if "product_name" not in item_columns:
        conn.execute("ALTER TABLE invoice_items ADD COLUMN product_name TEXT")
        conn.execute(
            "UPDATE invoice_items SET product_name = "
            "(SELECT name FROM products WHERE products.id = invoice_items.product_id)")

    # --- Failed login / recovery attempts (for lockouts) ---
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS login_failures (
            id INTEGER PRIMARY KEY,
            attempt_key TEXT NOT NULL,
            ip_address TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_login_failures_key ON login_failures (attempt_key, created_at);
        CREATE INDEX IF NOT EXISTS idx_login_failures_ip ON login_failures (ip_address, created_at);
    """)

    # --- Payments: who has paid what. An invoice's balance is its total minus
    # the sum of its payments. Invoices that existed before this feature are
    # recorded as paid in full (they were completed sales); the owner can
    # remove that payment on any invoice that was actually on credit. ---
    invoice_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(invoices)").fetchall()
    }
    if "due_date" not in invoice_columns:
        conn.execute("ALTER TABLE invoices ADD COLUMN due_date TEXT")
    if not conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='payments'").fetchone():
        conn.executescript("""
            CREATE TABLE payments (
                id INTEGER PRIMARY KEY,
                invoice_id INTEGER NOT NULL REFERENCES invoices(id),
                company_id INTEGER NOT NULL REFERENCES companies(id),
                amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
                method TEXT NOT NULL DEFAULT 'cash',
                note TEXT NOT NULL DEFAULT '',
                paid_on TEXT NOT NULL DEFAULT (date('now')),
                recorded_by INTEGER REFERENCES users(id),
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX idx_payments_invoice ON payments (invoice_id);
        """)
        conn.execute("""
            INSERT INTO payments (invoice_id, company_id, amount_cents, method, note, paid_on, recorded_by)
            SELECT id, company_id, total_cents, 'other', 'Marked paid when payment tracking was added',
                   date, created_by
            FROM invoices WHERE status = 'active' AND total_cents > 0 AND company_id IS NOT NULL
        """)

    # --- Business profile (shown on invoices) ---
    company_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(companies)").fetchall()
    }
    for col in ("address", "phone", "biz_email", "bank_name", "account_name", "account_number", "logo_file"):
        if col not in company_columns:
            conn.execute(f"ALTER TABLE companies ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")

    # --- Public share link for each invoice (WhatsApp) ---
    invoice_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(invoices)").fetchall()
    }
    if "share_token" not in invoice_columns:
        conn.execute("ALTER TABLE invoices ADD COLUMN share_token TEXT")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_invoices_share ON invoices (share_token)")

    # --- Cost price (for profit). NULL = not entered. Each sale keeps the
    # cost at the time, so later cost changes don't rewrite past profit. ---
    product_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(products)").fetchall()
    }
    if "cost_cents" not in product_columns:
        conn.execute("ALTER TABLE products ADD COLUMN cost_cents INTEGER")
    item_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(invoice_items)").fetchall()
    }
    if "unit_cost_cents" not in item_columns:
        conn.execute("ALTER TABLE invoice_items ADD COLUMN unit_cost_cents INTEGER")

    if "reset_token_hash" not in user_columns:
        conn.execute("ALTER TABLE users ADD COLUMN reset_token_hash TEXT")
        conn.execute("ALTER TABLE users ADD COLUMN reset_token_expires TEXT")
    if "checklist_dismissed" not in user_columns:
        conn.execute("ALTER TABLE users ADD COLUMN checklist_dismissed INTEGER NOT NULL DEFAULT 0")

    # --- Currency ---
    # Each company bills in one currency (an ISO code like NGN or USD).
    # Each invoice also stores the currency it was created in, so changing
    # the company setting later never relabels old invoices. Existing data
    # was always shown in dollars, so it's backfilled as USD.
    company_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(companies)").fetchall()
    }
    if "currency" not in company_columns:
        conn.execute("ALTER TABLE companies ADD COLUMN currency TEXT NOT NULL DEFAULT 'USD'")
    invoice_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(invoices)").fetchall()
    }
    if "currency" not in invoice_columns:
        conn.execute("ALTER TABLE invoices ADD COLUMN currency TEXT")
        conn.execute(
            "UPDATE invoices SET currency = COALESCE("
            "(SELECT currency FROM companies WHERE companies.id = invoices.company_id), 'USD')")

    conn.commit()
    conn.close()


def users_exist():
    conn = get_connection()
    count = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
    conn.close()
    return count > 0
