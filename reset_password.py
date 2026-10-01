"""
Last-resort recovery, for when an owner can't use "Forgot password"
(for example, email isn't set up on the server yet).

    python reset_password.py

Reads ADMIN_EMAIL and ADMIN_PASSWORD from .env and resets THAT ONE
account's password. Every other user, company, product and invoice is
left alone.
"""
import os
import sys

from werkzeug.security import generate_password_hash

from db import create_tables, get_connection
from env import load_local_env

load_local_env()

email = (os.getenv("ADMIN_EMAIL") or "").strip().lower()
password = os.getenv("ADMIN_PASSWORD")

if not email or not password:
    print("Set ADMIN_EMAIL and ADMIN_PASSWORD in .env first, then run this again.")
    sys.exit(1)
if len(password) < 8:
    print("ADMIN_PASSWORD must be at least 8 characters.")
    sys.exit(1)

create_tables()  # safe to call even if tables already exist

conn = get_connection()
user = conn.execute("SELECT id, role FROM users WHERE email = ?", (email,)).fetchone()
if user is None:
    conn.close()
    print(f"No account found for {email}. Nothing was changed.")
    sys.exit(1)

conn.execute("UPDATE users SET password_hash = ?, email_verified = 1 WHERE id = ?",
             (generate_password_hash(password), user["id"]))
conn.execute("DELETE FROM login_failures WHERE attempt_key = ?", ("owner:" + email,))
conn.commit()
conn.close()

print(f"Reset {email} ({user['role']}). Log in with the password from .env,")
print("then remove ADMIN_PASSWORD from .env.")
