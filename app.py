import hashlib
import os
from decimal import Decimal, ROUND_HALF_UP
import secrets
import smtplib
import sqlite3
from email.message import EmailMessage
from email.utils import parseaddr
from functools import wraps

from flask import Flask, render_template, request, redirect, url_for, flash, session, g
from flask_wtf import CSRFProtect
from werkzeug.security import check_password_hash, generate_password_hash
from db import create_tables, get_connection, users_exist
from env import load_local_env
from datetime import date, datetime, timedelta

load_local_env()

app = Flask(__name__)

# ---------- Production safety ----------
# Set APP_ENV=production on the live server. In production the app refuses
# to start without a real SECRET_KEY (a guessable key lets anyone forge a
# login cookie), and login cookies are only sent over HTTPS.
IS_PRODUCTION = os.getenv("APP_ENV", "").strip().lower() == "production"
_PLACEHOLDER_KEYS = {"", "dev-fallback-for-local-testing", "pick-any-long-random-string-of-letters-and-numbers",
                     "replace-me-with-64-random-characters"}
_secret = (os.getenv("SECRET_KEY") or "").strip()
if _secret in _PLACEHOLDER_KEYS or len(_secret) < 32:
    if IS_PRODUCTION:
        raise RuntimeError(
            "SECRET_KEY is missing, a placeholder, or too short. Generate one with:\n"
            "    python -c \"import secrets; print(secrets.token_hex(32))\"\n"
            "and set it in the server's environment / .env.")
    print("WARNING: using an insecure SECRET_KEY. Fine for local testing; "
          "set a real one before going live (see .env.example).")
    _secret = _secret or "dev-fallback-for-local-testing"
app.secret_key = _secret
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,     # JavaScript can't read the login cookie
    SESSION_COOKIE_SAMESITE="Lax",    # other sites can't ride on it
    SESSION_COOKIE_SECURE=IS_PRODUCTION,  # HTTPS only on the live site
    PERMANENT_SESSION_LIFETIME=timedelta(days=7),
)
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024   # biggest upload (logos): 2 MB
LOGO_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "uploads", "logos")
create_tables()
csrf = CSRFProtect(app)

class InsufficientStockError(Exception):
    """Raised when the atomic stock deduction fails at write time — someone
    else's invoice used up the stock between our read and our write."""
    pass

# ---------- Currency & formatting ----------
# ISO code -> (symbol, name). Every currency here uses 2 decimal places,
# which is what the integer-cents storage assumes. To add one, add a line.
CURRENCIES = {
    "NGN": ("₦", "Nigerian naira"),
    "USD": ("$", "US dollar"),
    "GBP": ("£", "British pound"),
    "EUR": ("€", "Euro"),
    "GHS": ("GH₵", "Ghanaian cedi"),
    "KES": ("KSh", "Kenyan shilling"),
    "ZAR": ("R", "South African rand"),
    "CAD": ("CA$", "Canadian dollar"),
}
DEFAULT_CURRENCY = "NGN"

def currency_symbol(code):
    return CURRENCIES.get(code or "", CURRENCIES["USD"])[0]

def current_currency():
    """The logged-in company's currency (loaded once per request)."""
    return getattr(g, "currency", None) or "USD"

def format_cents(cents, currency=None):
    """Integer cents -> display string, e.g. 123456 -> '₦1,234.56'. No floats.
    Uses the given currency code, or the logged-in company's currency."""
    symbol = currency_symbol(currency or current_currency())
    cents = int(cents or 0)
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}{symbol}{cents // 100:,}.{cents % 100:02d}"

def format_cents_short(cents, currency=None):
    """Compact money for chart labels: 125000000 -> '₦1.3M', 450000 -> '₦4.5k'."""
    symbol = currency_symbol(currency or current_currency())
    value = int(cents or 0) / 100
    for limit, suffix in ((1_000_000, "M"), (1_000, "k")):
        if abs(value) >= limit:
            return f"{symbol}{value / limit:.1f}".rstrip("0").rstrip(".") + suffix
    return f"{symbol}{value:,.0f}"

def format_date(value):
    """'2026-09-28' -> '28 Sep 2026'; '2026-09-28 14:05' -> '28 Sep 2026, 14:05'.
    'YYYY-MM' (report months) -> 'Sep 2026'. Anything else is shown as-is."""
    if not value:
        return ""
    for fmt, out in (("%Y-%m-%d %H:%M", "%d %b %Y, %H:%M"), ("%Y-%m-%d", "%d %b %Y"), ("%Y-%m", "%b %Y")):
        try:
            return datetime.strptime(str(value), fmt).strftime(out).lstrip("0")
        except ValueError:
            continue
    return value

app.jinja_env.filters["money"] = format_cents
app.jinja_env.filters["money_short"] = format_cents_short
app.jinja_env.filters["nicedate"] = format_date

PAYMENT_METHODS = {"cash": "Cash", "transfer": "Bank transfer", "pos": "POS / card", "other": "Other"}

# SQL snippet: how much has been paid on `invoices.id` so far.
PAID_SQL = "COALESCE((SELECT SUM(amount_cents) FROM payments WHERE payments.invoice_id = invoices.id), 0)"

def payment_status(inv):
    """'cancelled' | 'paid' | 'overdue' | 'part' | 'unpaid' for an invoice row
    that includes total_cents, paid_cents, status and due_date."""
    if inv["status"] == "cancelled":
        return "cancelled"
    paid, total = inv["paid_cents"] or 0, inv["total_cents"] or 0
    if paid >= total:
        return "paid"
    due = inv["due_date"]
    if due and due < str(date.today()):
        return "overdue"
    return "part" if paid > 0 else "unpaid"

PAYMENT_STATUS_LABELS = {"paid": ("Paid", "badge-paid"), "part": ("Part-paid", "badge-warning"),
                         "unpaid": ("Unpaid", "badge-warning"), "overdue": ("Overdue", "badge-danger"),
                         "cancelled": ("Cancelled", "badge-muted")}
app.jinja_env.globals.update(payment_status=payment_status, PAYMENT_STATUS_LABELS=PAYMENT_STATUS_LABELS,
                             PAYMENT_METHODS=PAYMENT_METHODS)

def read_cost(text):
    """Optional cost price from a form. Returns (cents or None, error or None).
    Only owners may set it; for anyone else it's ignored."""
    text = (text or "").strip()
    if not text or session.get("role") != "owner":
        return None, None
    try:
        cents = parse_money(text)
    except Exception:
        return None, "Cost price must be a number."
    if cents < 0:
        return None, "Cost price can't be negative."
    return cents, None

def whatsapp_number(phone):
    """'0803 123 4567' -> '2348031234567' (wa.me wants digits with country
    code). Numbers already starting with a country code are kept. Returns ''
    if there's nothing usable, so the share opens WhatsApp's contact picker."""
    digits = "".join(ch for ch in (phone or "") if ch.isdigit())
    if len(digits) == 11 and digits.startswith("0"):
        return "234" + digits[1:]
    return digits if len(digits) >= 10 else ""

app.jinja_env.globals.update(whatsapp_number=whatsapp_number)

def get_company(conn, company_id):
    return conn.execute("SELECT * FROM companies WHERE id = ?", (company_id,)).fetchone()

def ensure_share_token(conn, invoice_id):
    """Each invoice gets a long random link the first time it's shared.
    Only someone with the exact link can open it."""
    row = conn.execute("SELECT share_token FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
    if row and row["share_token"]:
        return row["share_token"]
    token = secrets.token_urlsafe(18)
    conn.execute("UPDATE invoices SET share_token = ? WHERE id = ?", (token, invoice_id))
    conn.commit()
    return token

def sniff_image(data):
    """Return 'png' | 'jpg' | 'webp' from the file's first bytes, or None.
    Looks at the content, not the filename, so a renamed script is refused."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None

def parse_money(text):
    """'12,500.50' -> 1250050 cents. Raises ValueError on anything odd."""
    value = Decimal(str(text).replace(",", "").strip())
    if value.is_nan() or value.is_infinite():
        raise ValueError
    return int((value * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))

@app.before_request
def load_company_currency():
    company_id = session.get("company_id")
    if company_id:
        conn = get_connection()
        row = conn.execute("SELECT currency FROM companies WHERE id = ?", (company_id,)).fetchone()
        conn.close()
        g.currency = row["currency"] if row else "USD"
    if session.get("user_id"):
        conn = get_connection()
        row = conn.execute("SELECT email_verified FROM users WHERE id = ?", (session["user_id"],)).fetchone()
        conn.close()
        g.email_verified = bool(row["email_verified"]) if row else True

@app.context_processor
def inject_currencies():
    return {"currencies": CURRENCIES, "company_currency": current_currency(),
            "email_verified": getattr(g, "email_verified", True),
            "default_currency": DEFAULT_CURRENCY}

# ---------- Auth ----------

def safe_next(target):
    """Only allow redirects back into this site. Without this, a link like
    /login?next=https://evil.example sends people to another site after
    they log in (an "open redirect", common in phishing)."""
    if target and target.startswith("/") and not target.startswith("//") \
            and "\\" not in target:
        return target
    return url_for("home")

def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user_id"):
            flash("Please log in to continue.", "error")
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped

def owner_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if session.get("role") != "owner":
            flash("Only the company owner can do that.", "error")
            return redirect(url_for("home"))
        return view(*args, **kwargs)
    return wrapped

SIGNUP_RATE_LIMIT = 5          # max signups per IP
SIGNUP_RATE_WINDOW_MIN = 60    # within this many minutes
VERIFICATION_TOKEN_HOURS = 24  # how long a verification link stays valid

TRUST_PROXY = os.getenv("TRUST_PROXY", "").strip() == "1"

def client_ip():
    # X-Forwarded-For can be typed by anyone, so it's only trusted when the
    # app really sits behind a proxy that sets it (PythonAnywhere, Render,
    # Railway do). Turn that on with TRUST_PROXY=1 on those hosts.
    if TRUST_PROXY:
        forwarded = request.headers.get("X-Forwarded-For", "")
        if forwarded:
            return forwarded.split(",")[-1].strip()
    return request.remote_addr or "unknown"

# ---------- Login lockout ----------
# 5 wrong tries on one account (or 30 from one network) within 15 minutes
# locks further tries for that window. Applies to owner login, team login
# and the recovery-passphrase page.
LOGIN_MAX_FAILS = 5
LOGIN_MAX_FAILS_PER_IP = 30
LOGIN_WINDOW_MIN = 15
LOCKOUT_MESSAGE = (f"Too many wrong attempts. For your security, please wait "
                   f"{LOGIN_WINDOW_MIN} minutes and try again.")

def _window_start():
    return (datetime.utcnow() - timedelta(minutes=LOGIN_WINDOW_MIN)).strftime("%Y-%m-%d %H:%M:%S")

def login_locked(conn, key, ip):
    since = _window_start()
    by_key = conn.execute(
        "SELECT COUNT(*) FROM login_failures WHERE attempt_key = ? AND created_at >= ?",
        (key, since)).fetchone()[0]
    by_ip = conn.execute(
        "SELECT COUNT(*) FROM login_failures WHERE ip_address = ? AND created_at >= ?",
        (ip, since)).fetchone()[0]
    return by_key >= LOGIN_MAX_FAILS or by_ip >= LOGIN_MAX_FAILS_PER_IP

def record_login_failure(conn, key, ip):
    conn.execute("INSERT INTO login_failures (attempt_key, ip_address) VALUES (?, ?)", (key, ip))
    # Housekeeping: drop anything older than a day.
    conn.execute("DELETE FROM login_failures WHERE created_at < datetime('now', '-1 day')")
    conn.commit()

def clear_login_failures(conn, key):
    conn.execute("DELETE FROM login_failures WHERE attempt_key = ?", (key,))
    conn.commit()

def is_rate_limited(conn, ip):
    cutoff = (datetime.utcnow() - timedelta(minutes=SIGNUP_RATE_WINDOW_MIN)).strftime("%Y-%m-%d %H:%M:%S")
    count = conn.execute(
        "SELECT COUNT(*) AS c FROM signup_attempts WHERE ip_address = ? AND created_at >= ?",
        (ip, cutoff)).fetchone()["c"]
    return count >= SIGNUP_RATE_LIMIT

def record_signup_attempt(conn, ip):
    conn.execute("INSERT INTO signup_attempts (ip_address) VALUES (?)", (ip,))

def external_url(endpoint, **values):
    """Full link for emails. Set APP_BASE_URL in .env to your real address
    (e.g. https://yourname.pythonanywhere.com). Without it, Flask uses the
    address you're browsing on, which is 127.0.0.1 when running locally,
    and that only works on the same computer while the app is running."""
    base = os.getenv("APP_BASE_URL", "").strip().rstrip("/")
    if base:
        return base + url_for(endpoint, **values)
    return url_for(endpoint, _external=True, **values)

def send_plain_email(to_address, subject, body):
    """Send a plain-text email using the SMTP settings in .env.
    Returns False (never raises) if email isn't set up or sending fails."""
    if not is_smtp_configured():
        return False
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = os.getenv("SMTP_FROM")
    message["To"] = to_address
    message.set_content(body)
    port = int(os.getenv("SMTP_PORT", "587"))
    try:
        if port == 465:
            with smtplib.SMTP_SSL(os.getenv("SMTP_HOST"), port, timeout=10) as smtp:
                smtp.login(os.getenv("SMTP_USERNAME"), os.getenv("SMTP_PASSWORD"))
                smtp.send_message(message)
        else:
            with smtplib.SMTP(os.getenv("SMTP_HOST"), port, timeout=10) as smtp:
                smtp.starttls()
                smtp.login(os.getenv("SMTP_USERNAME"), os.getenv("SMTP_PASSWORD"))
                smtp.send_message(message)
        return True
    except (OSError, smtplib.SMTPException, ValueError):
        app.logger.exception("Email to %s failed", to_address)
        return False

def send_verification_email(email, token):
    verify_url = external_url("verify_email", token=token)
    return send_plain_email(
        email, "Verify your email",
        "Welcome! Click the link below to verify your email:\n\n"
        f"{verify_url}\n\n"
        f"This link expires in {VERIFICATION_TOKEN_HOURS} hours.")

@app.route("/signup", methods=["GET", "POST"])
def signup():
    """One short screen: business name, email, password, currency.
    The owner is logged in straight away; verifying the email can wait
    (a banner reminds them). The team passcode is set later, on the Team
    page, only when they add their first teammate."""
    if session.get("user_id"):
        return redirect(url_for("home"))
    if request.method == "POST":
        # Honeypot: a field real people never see or fill in (hidden by CSS),
        # but a lot of basic signup-spam bots fill in every field blindly.
        # If it's non-empty, silently pretend success.
        if request.form.get("website", "").strip():
            return render_template("signup_check_email.html", email="")

        company_name = request.form.get("company_name", "").strip()
        currency = request.form.get("currency", DEFAULT_CURRENCY)
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        def fail(message):
            flash(message, "error")
            return render_template("signup.html", form=request.form), 400

        if not company_name:
            return fail("Enter your business name.")
        if currency not in CURRENCIES:
            return fail("Choose the currency your business uses.")
        if not is_valid_email(email):
            return fail("Enter a valid email address.")
        if len(password) < 8:
            return fail("Your password needs at least 8 characters.")

        conn = get_connection()
        ip = client_ip()
        if is_rate_limited(conn, ip):
            conn.close()
            return fail("Too many signups from this network recently. Please try again later.")
        if conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone():
            conn.close()
            flash("That email already has an account. Log in instead.", "error")
            return redirect(url_for("login"))
        # Company names are a lookup key (teammates log in by typing the
        # name), so two companies can't share one. Checked here for a clean
        # message, backed by a UNIQUE index for correctness under races.
        if conn.execute("SELECT id FROM companies WHERE LOWER(name) = LOWER(?)", (company_name,)).fetchone():
            conn.close()
            return fail("That business name is already taken. Try adding your area, e.g. \"Ada Stores Rumuola\".")

        record_signup_attempt(conn, ip)
        token = secrets.token_urlsafe(32)
        expires = (datetime.utcnow() + timedelta(hours=VERIFICATION_TOKEN_HOURS)).strftime("%Y-%m-%d %H:%M:%S")
        try:
            cur = conn.execute("INSERT INTO companies (name, passcode_hash, currency) VALUES (?, '', ?)",
                               (company_name, currency))
        except sqlite3.IntegrityError:
            conn.close()
            return fail("That business name is already taken. Try a more specific name.")
        company_id = cur.lastrowid
        cur = conn.execute(
            "INSERT INTO users (email, password_hash, recovery_hash, company_id, role, email_verified, "
            "verification_token, verification_token_expires, onboarded) "
            "VALUES (?, ?, '', ?, 'owner', 0, ?, ?, 0)",
            (email, generate_password_hash(password), company_id, token, expires))
        user_id = cur.lastrowid
        conn.commit()
        conn.close()

        sent = send_verification_email(email, token)
        session.clear()
        session.update(user_id=user_id, username=email, role="owner",
                       company_id=company_id, company_name=company_name)
        flash(f"Welcome to Invoice Generator! We've sent a verification link to {email}." if sent
              else "Welcome to Invoice Generator! Your account is ready.", "success")
        return redirect(url_for("home"))

    return render_template("signup.html", form={})

@app.route("/verify-email/<token>")
def verify_email(token):
    conn = get_connection()
    user = conn.execute(
        "SELECT * FROM users WHERE verification_token = ?", (token,)).fetchone()

    if user is None:
        conn.close()
        flash("That verification link isn't valid. It may have already been used.", "error")
        return redirect(url_for("login"))

    expires = user["verification_token_expires"]
    if expires and datetime.utcnow() > datetime.strptime(expires, "%Y-%m-%d %H:%M:%S"):
        conn.close()
        flash("That verification link has expired. Request a new one below.", "error")
        return redirect(url_for("resend_verification", email=user["email"]))

    conn.execute(
        "UPDATE users SET email_verified = 1, verification_token = NULL, "
        "verification_token_expires = NULL WHERE id = ?",
        (user["id"],))
    conn.commit()
    conn.close()
    if session.get("user_id"):
        flash("Email verified. Thanks!", "success")
        return redirect(url_for("home"))
    flash("Email verified. You can log in now.", "success")
    return redirect(url_for("login"))

@app.route("/resend-verification", methods=["GET", "POST"])
def resend_verification():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        conn = get_connection()
        user = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()

        # Same message whether the email exists or not — don't let this form
        # be used to check which emails have accounts.
        if user and not user["email_verified"]:
            token = secrets.token_urlsafe(32)
            expires = (datetime.utcnow() + timedelta(hours=VERIFICATION_TOKEN_HOURS)).strftime("%Y-%m-%d %H:%M:%S")
            conn.execute(
                "UPDATE users SET verification_token = ?, verification_token_expires = ? WHERE id = ?",
                (token, expires, user["id"]))
            conn.commit()
            send_verification_email(email, token)
        conn.close()
        flash("If that email needs verifying, a new link is on its way.", "success")
        return redirect(url_for("home") if session.get("user_id") else url_for("login"))

    return render_template("resend_verification.html", email=request.args.get("email", ""))

@app.route("/login", methods=["GET", "POST"])
def login():
    if not users_exist():
        return redirect(url_for("signup"))
    if session.get("user_id"):
        return redirect(url_for("home"))

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        next_url = safe_next(request.form.get("next"))

        conn = get_connection()
        key, ip = "owner:" + email, client_ip()
        if login_locked(conn, key, ip):
            conn.close()
            flash(LOCKOUT_MESSAGE, "error")
            return redirect(url_for("login", next=next_url))
        user = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        company = None
        if user and user["company_id"]:
            company = conn.execute(
                "SELECT * FROM companies WHERE id = ?", (user["company_id"],)).fetchone()

        if not (user and check_password_hash(user["password_hash"], password)):
            record_login_failure(conn, key, ip)
            conn.close()
            flash("Incorrect email or password.", "error")
            return redirect(url_for("login", next=next_url))

        clear_login_failures(conn, key)
        conn.close()
        session.clear()
        session["user_id"] = user["id"]
        session["username"] = user["email"]
        session["role"] = user["role"]
        session["company_id"] = user["company_id"]
        session["company_name"] = company["name"] if company else ""
        return redirect(next_url)

    return render_template("login.html", next=request.args.get("next", ""))

@app.route("/worker-login", methods=["GET", "POST"])
def worker_login():
    if session.get("user_id"):
        return redirect(url_for("home"))

    if request.method == "POST":
        company_name = request.form.get("company_name", "").strip()
        company_passcode = request.form.get("company_passcode", "")
        username = request.form.get("username", "").strip().lower()
        password = request.form.get("password", "")
        next_url = safe_next(request.form.get("next"))

        conn = get_connection()
        key, ip = f"team:{company_name.lower()}:{username}", client_ip()
        if login_locked(conn, key, ip):
            conn.close()
            flash(LOCKOUT_MESSAGE, "error")
            return redirect(url_for("worker_login", next=next_url))
        company = conn.execute(
            "SELECT * FROM companies WHERE LOWER(name) = LOWER(?)", (company_name,)).fetchone()

        # Same generic failure message at every step below, on purpose — a
        # different message for "wrong company name" vs "wrong passcode" vs
        # "wrong username" would let someone narrow down which part they got
        # wrong through trial and error.
        generic_error = "Company name, passcode, username, or password is incorrect."

        if company is None or not company["passcode_hash"] or \
           not check_password_hash(company["passcode_hash"], company_passcode):
            record_login_failure(conn, key, ip)
            conn.close()
            flash(generic_error, "error")
            return redirect(url_for("worker_login", next=next_url))

        # Username is looked up scoped to the company we just confirmed via
        # the passcode — this is what stops someone from using company A's
        # correct name+passcode to try logging in as a same-named user who
        # actually belongs to company B.
        user = conn.execute(
            "SELECT * FROM users WHERE email = ? AND company_id = ?",
            (username, company["id"])).fetchone()

        if not user or not check_password_hash(user["password_hash"], password):
            record_login_failure(conn, key, ip)
            conn.close()
            flash(generic_error, "error")
            return redirect(url_for("worker_login", next=next_url))

        clear_login_failures(conn, key)
        conn.close()
        if not user["email_verified"]:
            flash("This account isn't verified yet. Ask your company owner to check it.", "error")
            return redirect(url_for("worker_login"))

        session.clear()
        session["user_id"] = user["id"]
        session["username"] = user["email"]
        session["role"] = user["role"]
        session["company_id"] = user["company_id"]
        session["company_name"] = company["name"]
        return redirect(next_url)

    return render_template("worker_login.html", next=request.args.get("next", ""))

@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    flash("Signed out.", "success")
    return redirect(url_for("login"))

RESET_TOKEN_MINUTES = 60

def _hash_token(token):
    return hashlib.sha256(token.encode()).hexdigest()

@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    """Email a one-time reset link. The same message is shown whether or not
    the email has an account, so this page can't be used to find accounts."""
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        if not is_smtp_configured():
            flash("Password reset by email isn't switched on for this site yet. "
                  "Please contact the site administrator.", "error")
            return redirect(url_for("forgot_password"))

        conn = get_connection()
        key, ip = "reset:" + email, client_ip()
        if login_locked(conn, key, ip):
            conn.close()
            flash(LOCKOUT_MESSAGE, "error")
            return redirect(url_for("forgot_password"))
        record_login_failure(conn, key, ip)   # caps how many emails one address can trigger

        user = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
        if user:
            token = secrets.token_urlsafe(32)
            expires = (datetime.utcnow() + timedelta(minutes=RESET_TOKEN_MINUTES)).strftime("%Y-%m-%d %H:%M:%S")
            conn.execute("UPDATE users SET reset_token_hash = ?, reset_token_expires = ? WHERE id = ?",
                         (_hash_token(token), expires, user["id"]))
            conn.commit()
            send_plain_email(
                email, "Reset your password",
                "Someone (hopefully you) asked to reset your password.\n\n"
                f"Click here to choose a new one:\n{external_url('reset_password', token=token)}\n\n"
                f"This link works once and expires in {RESET_TOKEN_MINUTES} minutes. "
                "If you didn't ask for this, you can ignore this email.")
        conn.close()
        flash(f"If {email} has an account, a reset link is on its way. Check your inbox (and spam).", "success")
        return redirect(url_for("login"))

    return render_template("forgot_password.html")

@app.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    conn = get_connection()
    user = conn.execute(
        "SELECT * FROM users WHERE reset_token_hash = ? AND reset_token_expires > ?",
        (_hash_token(token), datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"))).fetchone()
    if user is None:
        conn.close()
        flash("That reset link is invalid or has expired. Request a new one below.", "error")
        return redirect(url_for("forgot_password"))

    if request.method == "POST":
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")
        if len(password) < 8 or password != confirm_password:
            conn.close()
            flash("Passwords must match and be at least 8 characters.", "error")
            return redirect(url_for("reset_password", token=token))
        conn.execute("UPDATE users SET password_hash = ?, reset_token_hash = NULL, "
                     "reset_token_expires = NULL, email_verified = 1 WHERE id = ?",
                     (generate_password_hash(password), user["id"]))
        conn.commit()
        clear_login_failures(conn, "owner:" + user["email"])
        conn.close()
        flash("Password changed. Log in with your new password.", "success")
        return redirect(url_for("login"))

    conn.close()
    return render_template("reset_password.html", token=token)

# ---------- Team (owner only) ----------

@app.route("/team")
@login_required
@owner_required
def team_page():
    conn = get_connection()
    workers = conn.execute(
        "SELECT * FROM users WHERE company_id = ? ORDER BY (role = 'owner') DESC, email",
        (session["company_id"],)).fetchall()
    company = conn.execute(
        "SELECT * FROM companies WHERE id = ?", (session["company_id"],)).fetchone()
    conn.close()
    passcode_is_set = bool(company and company["passcode_hash"])
    return render_template("team.html", workers=workers, passcode_is_set=passcode_is_set)

@app.route("/team/passcode", methods=["POST"])
@login_required
@owner_required
def change_company_passcode():
    passcode = request.form.get("company_passcode", "")
    confirm = request.form.get("confirm_company_passcode", "")

    if len(passcode) < 8:
        flash("Company passcode must be at least 8 characters.", "error")
        return redirect(url_for("team_page"))
    if passcode != confirm:
        flash("Company passcodes don't match.", "error")
        return redirect(url_for("team_page"))

    conn = get_connection()
    conn.execute(
        "UPDATE companies SET passcode_hash = ? WHERE id = ?",
        (generate_password_hash(passcode), session["company_id"]))
    conn.commit()
    conn.close()
    flash("Company passcode updated. Share the new one with your team — the old one no longer works.", "success")
    return redirect(url_for("team_page"))

@app.route("/team/add", methods=["POST"])
@login_required
@owner_required
def add_teammate():
    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")
    confirm_password = request.form.get("confirm_password", "")

    if not is_valid_email(email):
        flash("Enter a valid email address.", "error")
        return redirect(url_for("team_page"))
    if len(password) < 8:
        flash("Password must be at least 8 characters.", "error")
        return redirect(url_for("team_page"))
    if password != confirm_password:
        flash("Passwords don't match.", "error")
        return redirect(url_for("team_page"))

    conn = get_connection()
    existing = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
    if existing:
        conn.close()
        flash("That email already has an account somewhere.", "error")
        return redirect(url_for("team_page"))

    # Workers get an empty recovery_hash on purpose — they don't get their
    # own self-service "forgot password" flow. If they lose their password,
    # the owner resets it here instead. Only owners have no one above them
    # to ask, so only owners get a recovery passphrase.
    #
    # email_verified is set to 1 immediately: the owner adding them here IS
    # the verification (they're vouching for a real teammate's address
    # directly), unlike open company signup where nobody's vouching yet.
    conn.execute(
        "INSERT INTO users (email, password_hash, recovery_hash, company_id, role, email_verified) "
        "VALUES (?, ?, '', ?, 'worker', 1)",
        (email, generate_password_hash(password), session["company_id"]))
    conn.commit()
    conn.close()
    flash(f"Added {email} to the team.", "success")
    return redirect(url_for("team_page"))

@app.route("/team/<int:user_id>/reset-password", methods=["POST"])
@login_required
@owner_required
def reset_teammate_password(user_id):
    password = request.form.get("password", "")
    confirm_password = request.form.get("confirm_password", "")

    if len(password) < 8:
        flash("Password must be at least 8 characters.", "error")
        return redirect(url_for("team_page"))
    if password != confirm_password:
        flash("Passwords don't match.", "error")
        return redirect(url_for("team_page"))

    conn = get_connection()
    # Critical: only ever touch a user who belongs to YOUR OWN company.
    target = conn.execute(
        "SELECT * FROM users WHERE id = ? AND company_id = ?",
        (user_id, session["company_id"])).fetchone()
    if target is None:
        conn.close()
        flash("That teammate wasn't found.", "error")
        return redirect(url_for("team_page"))

    conn.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                 (generate_password_hash(password), user_id))
    conn.commit()
    conn.close()
    flash(f"Password reset for {target['email']}.", "success")
    return redirect(url_for("team_page"))

# ---------- Reports (owner only) ----------

@app.route("/reports")
@login_required
@owner_required
def reports():
    conn = get_connection()
    company_id = session["company_id"]

    monthly = conn.execute("""
        SELECT strftime('%Y-%m', date) AS month, currency,
               COUNT(*) AS invoice_count,
               SUM(total_cents) AS total_cents
        FROM invoices
        WHERE company_id = ? AND status = 'active'
        GROUP BY month, currency
        ORDER BY month DESC, currency
    """, (company_id,)).fetchall()

    by_worker = conn.execute("""
        SELECT users.email AS worker_email, invoices.currency AS currency,
               COUNT(invoices.id) AS invoice_count,
               COALESCE(SUM(invoices.total_cents), 0) AS total_cents
        FROM invoices
        JOIN users ON users.id = invoices.created_by
        WHERE invoices.company_id = ? AND invoices.status = 'active'
        GROUP BY invoices.created_by, invoices.currency
        ORDER BY total_cents DESC
    """, (company_id,)).fetchall()

    conn.close()
    return render_template("reports.html", monthly=monthly, by_worker=by_worker)

# ---------- Products ----------

@app.route("/")
def home():
    # Visitors who aren't logged in see the landing page; everyone else
    # goes straight to their dashboard.
    if not session.get("user_id"):
        return render_template("landing.html", currencies=CURRENCIES)
    company_id = session["company_id"]
    conn = get_connection()

    def scalar(sql, *args):
        return conn.execute(sql, (company_id, *args)).fetchone()[0] or 0

    today = date.today()
    this_month = today.strftime("%Y-%m")
    prev_month = (today.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    # Money totals only add up invoices in the company's current currency,
    # so a company that switched from $ to ₦ never sees the two mixed.
    cur = current_currency()
    active_in_month = ("SELECT {} FROM invoices WHERE company_id = ? "
                       "AND status = 'active' AND substr(date, 1, 7) = ? AND currency = ?")

    stats = {
        "products": scalar("SELECT COUNT(*) FROM products WHERE company_id = ?"),
        "customers": scalar("SELECT COUNT(*) FROM customers WHERE company_id = ?"),
        "invoices": scalar("SELECT COUNT(*) FROM invoices WHERE company_id = ?"),
        "teammates": scalar("SELECT COUNT(*) FROM users WHERE company_id = ? AND role = 'worker'"),
        "low_stock": scalar("SELECT COUNT(*) FROM products WHERE company_id = ? "
                            "AND stock <= low_stock_threshold"),
        "month_invoices": scalar(active_in_month.format("COUNT(*)"), this_month, cur),
        "month_total_cents": scalar(active_in_month.format("SUM(total_cents)"), this_month, cur),
        "prev_month_total_cents": scalar(active_in_month.format("SUM(total_cents)"), prev_month, cur),
    }

    # Last 6 months of revenue, oldest first, with empty months filled as 0.
    months = []
    cursor = today.replace(day=1)
    for _ in range(6):
        months.append(cursor.strftime("%Y-%m"))
        cursor = (cursor - timedelta(days=1)).replace(day=1)
    months.reverse()
    totals = {row["month"]: row["total_cents"] for row in conn.execute(
        "SELECT substr(date, 1, 7) AS month, SUM(total_cents) AS total_cents FROM invoices "
        "WHERE company_id = ? AND status = 'active' AND substr(date, 1, 7) >= ? "
        "AND currency = ? GROUP BY month",
        (company_id, months[0], cur)).fetchall()}
    revenue = [{"month": m, "label": datetime.strptime(m, "%Y-%m").strftime("%b"),
                "total_cents": totals.get(m, 0)} for m in months]

    top_products = conn.execute("""
        SELECT products.name, SUM(invoice_items.quantity) AS units,
               SUM(invoice_items.quantity * invoice_items.unit_price_cents) AS total_cents
        FROM invoice_items
        JOIN invoices ON invoices.id = invoice_items.invoice_id
        JOIN products ON products.id = invoice_items.product_id
        WHERE invoices.company_id = ? AND invoices.status = 'active' AND invoices.currency = ?
        GROUP BY products.id ORDER BY total_cents DESC LIMIT 5
    """, (company_id, cur)).fetchall()
    recent = conn.execute(
        "SELECT id, customer, date, total_cents, status, currency, due_date, " + PAID_SQL + " AS paid_cents FROM invoices "
        "WHERE company_id = ? ORDER BY id DESC LIMIT 5", (company_id,)).fetchall()
    low = conn.execute(
        "SELECT name, stock, low_stock_threshold FROM products WHERE company_id = ? "
        "AND stock <= low_stock_threshold ORDER BY stock ASC LIMIT 5", (company_id,)).fetchall()

    # Profit this month (owner only): price minus cost, for sales whose
    # product had a cost price at the time.
    prof = conn.execute(
        "SELECT COALESCE(SUM(ii.quantity * (ii.unit_price_cents - ii.unit_cost_cents)), 0) AS profit, "
        "SUM(CASE WHEN ii.unit_cost_cents IS NULL THEN 1 ELSE 0 END) AS missing "
        "FROM invoice_items ii JOIN invoices ON invoices.id = ii.invoice_id "
        "WHERE invoices.company_id = ? AND invoices.status = 'active' AND invoices.currency = ? "
        "AND substr(invoices.date, 1, 7) = ?", (company_id, cur, this_month)).fetchone()
    stats["profit_cents"] = prof["profit"] or 0
    stats["profit_missing"] = prof["missing"] or 0
    stats["products_without_cost"] = scalar(
        "SELECT COUNT(*) FROM products WHERE company_id = ? AND cost_cents IS NULL")

    balance_sql = "(invoices.total_cents - " + PAID_SQL + ")"
    owed_where = ("FROM invoices WHERE company_id = ? AND status = 'active' AND currency = ? "
                  "AND " + balance_sql + " > 0")
    stats["owed_cents"] = scalar("SELECT SUM(" + balance_sql + ") " + owed_where, cur)
    stats["owed_invoices"] = scalar("SELECT COUNT(*) " + owed_where, cur)
    stats["overdue"] = scalar("SELECT COUNT(*) " + owed_where + " AND due_date < ?", cur, str(today))
    debtors = conn.execute(
        "SELECT customer_id, customer, SUM(" + balance_sql + ") AS owed_cents, COUNT(*) AS invoice_count, "
        "MIN(due_date) AS oldest_due " + owed_where +
        " GROUP BY COALESCE(customer_id, customer) ORDER BY owed_cents DESC LIMIT 5",
        (company_id, cur)).fetchall()

    checklist_dismissed = bool(conn.execute(
        "SELECT checklist_dismissed FROM users WHERE id = ?",
        (session["user_id"],)).fetchone()["checklist_dismissed"])

    # Show the welcome tour exactly once per account: flip the flag the
    # first time the dashboard loads, so it never comes back on the next
    # login, a refresh, or another device. "Replay the tour" still works.
    first_run = conn.execute(
        "UPDATE users SET onboarded = 1 WHERE id = ? AND onboarded = 0",
        (session["user_id"],)).rowcount == 1
    conn.commit()
    conn.close()

    return render_template("index.html", stats=stats, revenue=revenue,
                           top_products=top_products, recent=recent, low=low,
                           first_run=first_run, checklist_dismissed=checklist_dismissed,
                           debtors=debtors, today=str(today))

@app.route("/checklist/dismiss", methods=["POST"])
@login_required
def dismiss_checklist():
    conn = get_connection()
    conn.execute("UPDATE users SET checklist_dismissed = 1 WHERE id = ?", (session["user_id"],))
    conn.commit()
    conn.close()
    return redirect(url_for("home"))

# ---------- Settings (owner only) ----------

@app.route("/settings", methods=["GET", "POST"])
@login_required
@owner_required
def settings_page():
    conn = get_connection()
    company_id = session["company_id"]
    if request.method == "POST":
        currency = request.form.get("currency", "")
        if currency not in CURRENCIES:
            conn.close()
            flash("Pick a currency from the list.", "error")
            return redirect(url_for("settings_page"))
        conn.execute("UPDATE companies SET currency = ? WHERE id = ?", (currency, company_id))
        conn.commit()
        conn.close()
        flash(f"Currency set to {CURRENCIES[currency][1]}. New invoices will use "
              f"{CURRENCIES[currency][0]}; existing invoices keep their original currency.",
              "success")
        return redirect(url_for("settings_page"))

    invoice_count = conn.execute(
        "SELECT COUNT(*) FROM invoices WHERE company_id = ?", (company_id,)).fetchone()[0]
    company = get_company(conn, company_id)
    conn.close()
    return render_template("settings.html", invoice_count=invoice_count, company=company)

@app.route("/products")
@login_required
def products_page():
    conn = get_connection()
    products = conn.execute(
        "SELECT * FROM products WHERE company_id = ? ORDER BY id DESC",
        (session["company_id"],)).fetchall()
    conn.close()
    return render_template("products.html", products=products)

@app.route("/invoice/new")
@login_required
def new_invoice():
    conn = get_connection()
    products = conn.execute(
        "SELECT * FROM products WHERE company_id = ? ORDER BY name",
        (session["company_id"],)).fetchall()
    customers = conn.execute(
        "SELECT * FROM customers WHERE company_id = ? ORDER BY name",
        (session["company_id"],)).fetchall()
    conn.close()
    return render_template("invoice_new.html", products=products, customers=customers,
                           smtp_configured=is_smtp_configured())

# ---------- Customers ----------

@app.route("/customers")
@login_required
def customers_page():
    conn = get_connection()
    rows = conn.execute("""
        SELECT customers.*, COUNT(invoices.id) AS invoice_count,
               COALESCE(SUM(CASE WHEN invoices.status = 'active' AND invoices.currency = ?
                            THEN invoices.total_cents - """ + PAID_SQL + """ END), 0) AS owed_cents
        FROM customers
        LEFT JOIN invoices ON invoices.customer_id = customers.id AND invoices.company_id = customers.company_id
        WHERE customers.company_id = ?
        GROUP BY customers.id
        ORDER BY customers.name
    """, (current_currency(), session["company_id"])).fetchall()
    conn.close()
    return render_template("customers.html", customers=rows)

@app.route("/customers/add", methods=["POST"])
@login_required
def add_customer():
    name = request.form.get("name", "").strip()
    email = request.form.get("email", "").strip().lower()
    phone = request.form.get("phone", "").strip()

    if not name:
        flash("Customer name is required.", "error")
        return redirect(url_for("customers_page"))
    if email and not is_valid_email(email):
        flash("That email doesn't look right. Leave it empty if they don't have one.", "error")
        return redirect(url_for("customers_page"))

    conn = get_connection()
    conn.execute("INSERT INTO customers (name, email, phone, company_id) VALUES (?, ?, ?, ?)",
                 (name, email, phone, session["company_id"]))
    conn.commit()
    conn.close()
    flash(f"Customer '{name}' added.", "success")
    return redirect(url_for("customers_page"))

@app.route("/customers/<int:customer_id>")
@login_required
def customer_detail(customer_id):
    conn = get_connection()
    customer = conn.execute(
        "SELECT * FROM customers WHERE id = ? AND company_id = ?",
        (customer_id, session["company_id"])).fetchone()
    if customer is None:
        conn.close()
        return "Customer not found", 404
    invoices = conn.execute(
        "SELECT invoices.*, " + PAID_SQL + " AS paid_cents FROM invoices "
        "WHERE customer_id = ? AND company_id = ? ORDER BY id DESC",
        (customer_id, session["company_id"])).fetchall()
    conn.close()
    return render_template("customer_detail.html", customer=customer, invoices=invoices)

@app.route("/products/add", methods=["POST"])
@login_required
def add_product():
    name = request.form["name"].strip()
    try:
        price_dollars = float(request.form["price"])
        stock = int(request.form["stock"])
        threshold = int(request.form.get("low_stock_threshold") or 5)
    except ValueError:
        flash("Price and stock must be valid numbers.", "error")
        return redirect(url_for("products_page"))
    price_cents = round(price_dollars * 100)
    if not name or price_cents < 0 or stock < 0 or threshold < 0:
        flash("Name is required; price, stock and alert level can't be negative.", "error")
        return redirect(url_for("products_page"))
    cost_cents, err = read_cost(request.form.get("cost", ""))
    if err:
        flash(err, "error")
        return redirect(url_for("products_page"))

    conn = get_connection()
    conn.execute(
        "INSERT INTO products (name, price, price_cents, stock, low_stock_threshold, company_id, cost_cents) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (name, price_cents / 100.0, price_cents, stock, threshold, session["company_id"], cost_cents))
    conn.commit()
    conn.close()
    flash(f"Product '{name}' added.", "success")
    return redirect(url_for("products_page"))

@app.route("/products/<int:product_id>/edit", methods=["POST"])
@login_required
@owner_required
def edit_product(product_id):
    """Owner-only: change a product's name and price. Teammates see the
    change immediately (everyone reads the same product list). Invoices
    already created keep the name and price they were billed at."""
    name = request.form.get("name", "").strip()
    try:
        price_cents = round(float(request.form.get("price", "")) * 100)
    except ValueError:
        flash("Price must be a number.", "error")
        return redirect(url_for("products_page"))
    if not name or price_cents < 0:
        flash("Name is required and price can't be negative.", "error")
        return redirect(url_for("products_page"))
    cost_cents, err = read_cost(request.form.get("cost", ""))
    if err:
        flash(err, "error")
        return redirect(url_for("products_page"))

    conn = get_connection()
    cur = conn.execute(
        "UPDATE products SET name = ?, price = ?, price_cents = ?, cost_cents = ? WHERE id = ? AND company_id = ?",
        (name, price_cents / 100.0, price_cents, cost_cents, product_id, session["company_id"]))
    conn.commit()
    conn.close()
    if cur.rowcount == 0:
        flash("Product not found.", "error")
    else:
        flash(f"'{name}' updated. Your team sees the new details right away.", "success")
    return redirect(url_for("products_page"))

@app.route("/products/restock/<int:product_id>", methods=["POST"])
@login_required
def restock_product(product_id):
    # Restock can be done from Products or Low stock; go back to whichever.
    back = request.form.get("next") if request.form.get("next") in ("/products", "/low-stock") \
        else url_for("products_page")
    try:
        amount = int(request.form["amount"])
    except ValueError:
        flash("Restock amount must be a number.", "error")
        return redirect(back)
    if amount <= 0:
        flash("Restock amount must be positive.", "error")
        return redirect(back)

    conn = get_connection()
    cur = conn.execute(
        "UPDATE products SET stock = stock + ? WHERE id = ? AND company_id = ?",
        (amount, product_id, session["company_id"]))
    conn.commit()
    conn.close()
    if cur.rowcount == 0:
        flash("Product not found.", "error")
    else:
        flash(f"Restocked {amount} units.", "success")
    return redirect(back)

@app.route("/products/delete/<int:product_id>", methods=["POST"])  # POST, not GET
@login_required
@owner_required
def delete_product(product_id):
    conn = get_connection()
    product = conn.execute(
        "SELECT id FROM products WHERE id = ? AND company_id = ?",
        (product_id, session["company_id"])).fetchone()
    if product is None:
        conn.close()
        flash("Product not found.", "error")
        return redirect(url_for("products_page"))

    used = conn.execute(
        "SELECT COUNT(*) AS c FROM invoice_items WHERE product_id = ?",
        (product_id,)).fetchone()["c"]
    if used > 0:
        conn.close()
        flash("Cannot delete: product appears on existing invoices.", "error")
        return redirect(url_for("products_page"))

    try:
        conn.execute("DELETE FROM products WHERE id = ? AND company_id = ?",
                     (product_id, session["company_id"]))
        conn.commit()
        flash("Product deleted.", "success")
    except sqlite3.IntegrityError:
        conn.rollback()
        flash("Cannot delete: an invoice was just created for this product. Please try again.", "error")
    finally:
        conn.close()
    return redirect(url_for("products_page"))

# ---------- Invoices ----------

@app.route("/invoice/create", methods=["POST"])
@login_required
def create_invoice():
    company_id = session["company_id"]
    customer_choice = request.form.get("customer_id", "")
    conn = get_connection()

    if customer_choice == "__new__":
        name = request.form.get("new_customer_name", "").strip()
        email = request.form.get("new_customer_email", "").strip().lower()
        phone = request.form.get("new_customer_phone", "").strip()
        if not name:
            conn.close()
            flash("New customer name is required.", "error")
            return redirect(url_for("new_invoice"))
        if email and not is_valid_email(email):
            conn.close()
            flash("That email doesn't look right. Leave it empty if they don't have one.", "error")
            return redirect(url_for("new_invoice"))
        cur = conn.execute(
            "INSERT INTO customers (name, email, phone, company_id) VALUES (?, ?, ?, ?)",
            (name, email, phone, company_id))
        customer_id = cur.lastrowid
        customer, customer_email = name, email
    elif customer_choice:
        row = conn.execute(
            "SELECT * FROM customers WHERE id = ? AND company_id = ?",
            (customer_choice, company_id)).fetchone()
        if row is None:
            conn.close()
            flash("Selected customer not found.", "error")
            return redirect(url_for("new_invoice"))
        customer_id, customer, customer_email = row["id"], row["name"], row["email"]
    else:
        conn.close()
        flash("Choose a customer or add a new one.", "error")
        return redirect(url_for("new_invoice"))

    product_ids = request.form.getlist("product_id")
    quantities = request.form.getlist("quantity")

    # combine duplicate lines BEFORE checking stock
    wanted = {}
    for pid, qty in zip(product_ids, quantities):
        if not pid:
            continue
        try:
            qty = int(qty)
        except ValueError:
            conn.close()
            flash("Quantities must be whole numbers.", "error")
            return redirect(url_for("new_invoice"))
        if qty < 1:
            conn.close()
            flash("Quantities must be at least 1.", "error")
            return redirect(url_for("new_invoice"))
        wanted[pid] = wanted.get(pid, 0) + qty   # "3 then 3" becomes 6

    if not wanted:
        conn.close()
        flash("Add at least one product line before creating the invoice.", "error")
        return redirect(url_for("new_invoice"))

    try:
        checked = []
        for pid, qty in wanted.items():
            # company_id check here matters: without it, a crafted request
            # could invoice against another company's product entirely.
            product = conn.execute(
                "SELECT * FROM products WHERE id = ? AND company_id = ?",
                (pid, company_id)).fetchone()
            if product is None:
                flash(f"Product not found (id {pid}).", "error")
                return redirect(url_for("new_invoice"))
            if product["stock"] < qty:
                flash(f"Not enough stock for {product['name']}: "
                      f"only {product['stock']} left.", "error")
                return redirect(url_for("new_invoice"))
            checked.append((product, qty))

        total_cents = sum(p["price_cents"] * q for p, q in checked)

        # How much did they pay right now? (full / nothing yet / part)
        paid_now = request.form.get("paid_now", "full")
        method = request.form.get("pay_method", "cash")
        method = method if method in PAYMENT_METHODS else "cash"
        if paid_now == "full":
            paid_cents = total_cents
        elif paid_now == "part":
            try:
                paid_cents = parse_money(request.form.get("paid_amount", ""))
            except Exception:
                paid_cents = -1
            if not 0 < paid_cents < total_cents:
                flash("For a part payment, enter an amount above 0 and below the total.", "error")
                return redirect(url_for("new_invoice"))
        else:
            paid_cents = 0
        try:
            due_days = max(0, min(int(request.form.get("due_in_days", "7")), 365))
        except ValueError:
            due_days = 7
        due_date = str(date.today() + timedelta(days=due_days))

        cur = conn.execute(
            "INSERT INTO invoices (customer, customer_email, date, total, total_cents, "
            "customer_id, company_id, created_by, currency, due_date) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (customer, customer_email, str(date.today()), total_cents / 100.0, total_cents,
             customer_id, company_id, session["user_id"], current_currency(), due_date))
        invoice_id = cur.lastrowid
        if paid_cents > 0:
            conn.execute(
                "INSERT INTO payments (invoice_id, company_id, amount_cents, method, recorded_by) "
                "VALUES (?, ?, ?, ?, ?)", (invoice_id, company_id, paid_cents, method, session["user_id"]))

        for product, qty in checked:
            conn.execute(
                "INSERT INTO invoice_items (invoice_id, product_id, quantity, unit_price, unit_price_cents, "
                "product_name, unit_cost_cents) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (invoice_id, product["id"], qty, product["price_cents"] / 100.0, product["price_cents"],
                 product["name"], product["cost_cents"]))
            # Atomic, re-checked-at-write-time deduction: the WHERE clause is
            # what actually prevents overselling under concurrent requests.
            # The SELECT-based check above is just an early, friendly error —
            # it can go stale between then and now, this can't.
            cur = conn.execute(
                "UPDATE products SET stock = stock - ? WHERE id = ? AND company_id = ? AND stock >= ?",
                (qty, product["id"], company_id, qty))
            if cur.rowcount == 0:
                raise InsufficientStockError(product["name"])
        conn.commit()
    except InsufficientStockError as e:
        conn.rollback()
        flash(f"Not enough stock for {e}: someone else just used the last of it. "
              f"Nothing was charged — please try again.", "error")
        return redirect(url_for("new_invoice"))
    except Exception:
        conn.rollback()
        app.logger.exception("Error creating invoice")
        flash("Something went wrong creating the invoice. Please try again.", "error")
        return redirect(url_for("new_invoice"))
    finally:
        conn.close()

    if customer_email and send_invoice_email(invoice_id, company_id):
        flash(f"Invoice #{invoice_id} created and emailed to {customer_email}.", "success")
    else:
        flash(f"Invoice #{invoice_id} created. Share it on WhatsApp or print it below.", "success")
    return redirect(url_for("view_invoice", invoice_id=invoice_id))

def is_valid_email(value):
    address = parseaddr(value)[1]
    return bool(address and "@" in address and "." in address.rsplit("@", 1)[1])

def is_smtp_configured():
    return all(os.getenv(name) for name in
               ("SMTP_HOST", "SMTP_FROM", "SMTP_USERNAME", "SMTP_PASSWORD"))

def send_invoice_email(invoice_id, company_id):
    smtp_host = os.getenv("SMTP_HOST")
    sender = os.getenv("SMTP_FROM")
    smtp_username = os.getenv("SMTP_USERNAME")
    smtp_password = os.getenv("SMTP_PASSWORD")
    if not is_smtp_configured():
        return False

    conn = get_connection()
    # company_id is required here (not just checked by the caller) so this
    # function can never be called into emailing another company's invoice,
    # regardless of what any future caller does or forgets to check.
    invoice = conn.execute(
        "SELECT invoices.*, " + PAID_SQL + " AS paid_cents FROM invoices WHERE id = ? AND company_id = ?",
        (invoice_id, company_id)).fetchone()
    company = get_company(conn, company_id)
    share_link = external_url("public_invoice", token=ensure_share_token(conn, invoice_id)) if invoice else ""
    items = conn.execute(
        "SELECT COALESCE(invoice_items.product_name, products.name) AS name, "
        "invoice_items.quantity, invoice_items.unit_price_cents "
        "FROM invoice_items JOIN products ON products.id = invoice_items.product_id "
        "WHERE invoice_items.invoice_id = ?", (invoice_id,)).fetchall()
    conn.close()
    if not invoice or not invoice["customer_email"]:
        return False

    cur = invoice["currency"]
    balance = invoice["total_cents"] - invoice["paid_cents"]
    lines = [f"Invoice #{invoice['id']:05d} from {company['name']}", f"Date: {format_date(invoice['date'])}",
             f"Customer: {invoice['customer']}", ""]
    for item in items:
        amount_cents = item["quantity"] * item["unit_price_cents"]
        lines.append(f"{item['name']} x {item['quantity']}: {format_cents(amount_cents, cur)}")
    lines.extend(["", f"Total: {format_cents(invoice['total_cents'], cur)}"])
    if balance <= 0:
        lines.append("Status: PAID. Thank you!")
    else:
        if invoice["paid_cents"]:
            lines.append(f"Paid so far: {format_cents(invoice['paid_cents'], cur)}")
        lines.append(f"Balance due: {format_cents(balance, cur)}"
                     + (f" by {format_date(invoice['due_date'])}" if invoice["due_date"] else ""))
        if company["account_number"]:
            lines.extend(["", "Pay by bank transfer to:",
                          f"{company['bank_name']} · {company['account_number']} · {company['account_name']}"])
    lines.extend(["", f"View or download your invoice: {share_link}"])
    contact = " · ".join(x for x in (company["phone"], company["biz_email"], company["address"]) if x)
    if contact:
        lines.extend(["", contact])

    message = EmailMessage()
    message["Subject"] = f"Invoice #{invoice['id']}"
    message["From"] = sender
    message["To"] = invoice["customer_email"]
    message.set_content("\n".join(lines))
    port = int(os.getenv("SMTP_PORT", "587"))
    try:
        if port == 465:
            with smtplib.SMTP_SSL(smtp_host, port, timeout=10) as smtp:
                if smtp_username and smtp_password:
                    smtp.login(smtp_username, smtp_password)
                smtp.send_message(message)
        else:
            with smtplib.SMTP(smtp_host, port, timeout=10) as smtp:
                smtp.starttls()
                if smtp_username and smtp_password:
                    smtp.login(smtp_username, smtp_password)
                smtp.send_message(message)
    except (OSError, smtplib.SMTPException, ValueError):
        return False

    conn = get_connection()
    conn.execute("UPDATE invoices SET emailed_at = ? WHERE id = ? AND company_id = ?",
                 (datetime.now().strftime("%Y-%m-%d %H:%M"), invoice_id, company_id))
    conn.commit()
    conn.close()
    return True

@app.route("/invoice/<int:invoice_id>/email", methods=["POST"])
@login_required
def email_invoice(invoice_id):
    conn = get_connection()
    invoice = conn.execute(
        "SELECT id FROM invoices WHERE id = ? AND company_id = ?",
        (invoice_id, session["company_id"])).fetchone()
    conn.close()
    if invoice is None:
        return "Invoice not found", 404

    if send_invoice_email(invoice_id, session["company_id"]):
        flash("Invoice sent to the customer again.", "success")
    else:
        flash("The invoice could not be emailed. Check the recipient address and SMTP settings.", "error")
    return redirect(url_for("view_invoice", invoice_id=invoice_id))

@app.route("/invoice/<int:invoice_id>/cancel", methods=["POST"])
@login_required
@owner_required
def cancel_invoice(invoice_id):
    conn = get_connection()
    try:
        invoice = conn.execute(
            "SELECT * FROM invoices WHERE id = ? AND company_id = ?",
            (invoice_id, session["company_id"])
        ).fetchone()
        if invoice is None:
            return "Invoice not found", 404
        if conn.execute("SELECT 1 FROM payments WHERE invoice_id = ?", (invoice_id,)).fetchone():
            flash("This invoice has payments recorded. Remove them first, then cancel it.", "error")
            return redirect(url_for("view_invoice", invoice_id=invoice_id))
        # Flip the status first, and only if it's still active. If two
        # cancel requests race, only one gets rowcount 1, so stock can't be
        # restored twice.
        flipped = conn.execute(
            "UPDATE invoices SET status = 'cancelled' "
            "WHERE id = ? AND company_id = ? AND status = 'active'",
            (invoice_id, session["company_id"])).rowcount
        if flipped == 0:
            flash("This invoice is already cancelled.", "error")
            return redirect(url_for("view_invoice", invoice_id=invoice_id))

        # Get all line items before we change anything
        items = conn.execute(
            "SELECT product_id, quantity FROM invoice_items WHERE invoice_id = ?",
            (invoice_id,)
        ).fetchall()

        # Restore stock for each item. The company_id check is redundant
        # today (invoice_items can only ever point at a product from the
        # same company the invoice belongs to, since create_invoice enforces
        # that at write time) — but this stays explicit anyway, matching
        # every other write in this file, rather than relying on that never
        # changing.
        for item in items:
            conn.execute(
                "UPDATE products SET stock = stock + ? WHERE id = ? AND company_id = ?",
                (item["quantity"], item["product_id"], session["company_id"])
            )

        conn.commit()
        flash(f"Invoice #{invoice_id} cancelled and stock restored.", "success")
    except Exception:
        conn.rollback()
        app.logger.exception("Error cancelling invoice")
        flash("Something went wrong cancelling the invoice. Please try again.", "error")
    finally:
        conn.close()

    return redirect(url_for("view_invoice", invoice_id=invoice_id))

@app.route("/invoice/<int:invoice_id>/payments", methods=["POST"])
@login_required
def record_payment(invoice_id):
    """Any team member can record money received (they're often the one at
    the counter). Only the owner can remove a payment."""
    conn = get_connection()
    inv = conn.execute(
        "SELECT invoices.*, " + PAID_SQL + " AS paid_cents FROM invoices WHERE id = ? AND company_id = ?",
        (invoice_id, session["company_id"])).fetchone()
    if inv is None:
        conn.close()
        return "Invoice not found", 404
    back = redirect(url_for("view_invoice", invoice_id=invoice_id))
    if inv["status"] != "active":
        conn.close()
        flash("You can't record payments on a cancelled invoice.", "error")
        return back
    balance = inv["total_cents"] - inv["paid_cents"]
    try:
        amount = balance if request.form.get("full") else parse_money(request.form.get("amount", ""))
    except Exception:
        amount = 0
    if amount <= 0 or amount > balance:
        conn.close()
        flash(f"Enter an amount between {format_cents(1, inv['currency'])} and the balance of "
              f"{format_cents(balance, inv['currency'])}.", "error")
        return back
    method = request.form.get("method", "cash")
    method = method if method in PAYMENT_METHODS else "cash"
    note = request.form.get("note", "").strip()[:200]
    conn.execute("INSERT INTO payments (invoice_id, company_id, amount_cents, method, note, recorded_by) "
                 "VALUES (?, ?, ?, ?, ?, ?)",
                 (invoice_id, session["company_id"], amount, method, note, session["user_id"]))
    conn.commit()
    conn.close()
    left = balance - amount
    flash(f"Payment of {format_cents(amount, inv['currency'])} recorded. " +
          ("Invoice is now fully paid." if left == 0 else
           f"{format_cents(left, inv['currency'])} still owed."), "success")
    return back

@app.route("/payments/<int:payment_id>/delete", methods=["POST"])
@login_required
@owner_required
def delete_payment(payment_id):
    conn = get_connection()
    pay = conn.execute("SELECT * FROM payments WHERE id = ? AND company_id = ?",
                       (payment_id, session["company_id"])).fetchone()
    if pay is None:
        conn.close()
        return "Payment not found", 404
    conn.execute("DELETE FROM payments WHERE id = ?", (payment_id,))
    conn.commit()
    conn.close()
    flash("Payment removed.", "success")
    return redirect(url_for("view_invoice", invoice_id=pay["invoice_id"]))

@app.route("/invoice/<int:invoice_id>")
@login_required
def view_invoice(invoice_id):
    conn = get_connection()
    invoice = conn.execute("""
        SELECT invoices.*, users.email AS created_by_email, """ + PAID_SQL + """ AS paid_cents
        FROM invoices
        LEFT JOIN users ON users.id = invoices.created_by
        WHERE invoices.id = ? AND invoices.company_id = ?
    """, (invoice_id, session["company_id"])).fetchone()
    if invoice is None:
        conn.close()
        return "Invoice not found", 404
    items = conn.execute("""
        SELECT COALESCE(invoice_items.product_name, products.name) AS name,
               invoice_items.quantity, invoice_items.unit_price_cents
        FROM invoice_items
        JOIN products ON products.id = invoice_items.product_id
        WHERE invoice_items.invoice_id = ?
    """, (invoice_id,)).fetchall()
    payments = conn.execute("""
        SELECT payments.*, users.email AS recorded_by_email FROM payments
        LEFT JOIN users ON users.id = payments.recorded_by
        WHERE payments.invoice_id = ? ORDER BY payments.id
    """, (invoice_id,)).fetchall()
    company = get_company(conn, session["company_id"])
    token = ensure_share_token(conn, invoice_id) if invoice["status"] == "active" else None
    customer_phone = ""
    if invoice["customer_id"]:
        row = conn.execute("SELECT phone FROM customers WHERE id = ?", (invoice["customer_id"],)).fetchone()
        customer_phone = row["phone"] if row else ""
    conn.close()
    return render_template("invoice.html", invoice=invoice, items=items, payments=payments,
                           company=company, customer_phone=customer_phone,
                           share_url=external_url("public_invoice", token=token) if token else "",
                           smtp_configured=is_smtp_configured(), today=str(date.today()))

@app.route("/i/<token>")
def public_invoice(token):
    """Customer-facing copy of an invoice, opened from WhatsApp or email.
    No login: the long random token in the link is the key. Cancelled
    invoices stop showing."""
    if len(token) < 16:
        return render_template("public_invoice.html", invoice=None), 404
    conn = get_connection()
    invoice = conn.execute(
        "SELECT invoices.*, " + PAID_SQL + " AS paid_cents FROM invoices "
        "WHERE share_token = ? AND status = 'active'", (token,)).fetchone()
    if invoice is None:
        conn.close()
        return render_template("public_invoice.html", invoice=None), 404
    items = conn.execute(
        "SELECT COALESCE(invoice_items.product_name, products.name) AS name, "
        "invoice_items.quantity, invoice_items.unit_price_cents "
        "FROM invoice_items JOIN products ON products.id = invoice_items.product_id "
        "WHERE invoice_items.invoice_id = ?", (invoice["id"],)).fetchall()
    company = get_company(conn, invoice["company_id"])
    conn.close()
    resp = app.make_response(render_template("public_invoice.html", invoice=invoice, items=items,
                                             company=company, today=str(date.today())))
    resp.headers["X-Robots-Tag"] = "noindex"
    resp.headers["Referrer-Policy"] = "no-referrer"
    return resp

@app.route("/settings/business", methods=["POST"])
@login_required
@owner_required
def save_business_profile():
    f = request.form
    values = {
        "address": f.get("address", "").strip()[:200],
        "phone": f.get("phone", "").strip()[:40],
        "biz_email": f.get("biz_email", "").strip().lower()[:120],
        "bank_name": f.get("bank_name", "").strip()[:80],
        "account_name": f.get("account_name", "").strip()[:120],
        "account_number": "".join(ch for ch in f.get("account_number", "") if ch.isdigit())[:20],
    }
    if values["biz_email"] and not is_valid_email(values["biz_email"]):
        flash("That business email doesn't look right.", "error")
        return redirect(url_for("settings_page") + "#business")

    conn = get_connection()
    company = get_company(conn, session["company_id"])
    logo_file = company["logo_file"]

    upload = request.files.get("logo")
    if upload and upload.filename:
        data = upload.read()
        kind = sniff_image(data)
        if kind is None:
            conn.close()
            flash("Logo must be a PNG, JPG or WEBP image.", "error")
            return redirect(url_for("settings_page") + "#business")
        os.makedirs(LOGO_DIR, exist_ok=True)
        new_name = f"{session['company_id']}-{secrets.token_hex(6)}.{kind}"
        with open(os.path.join(LOGO_DIR, new_name), "wb") as fh:
            fh.write(data)
        _remove_logo_file(logo_file)
        logo_file = new_name
    elif f.get("remove_logo"):
        _remove_logo_file(logo_file)
        logo_file = ""

    conn.execute(
        "UPDATE companies SET address = ?, phone = ?, biz_email = ?, bank_name = ?, account_name = ?, "
        "account_number = ?, logo_file = ? WHERE id = ?",
        (values["address"], values["phone"], values["biz_email"], values["bank_name"],
         values["account_name"], values["account_number"], logo_file, session["company_id"]))
    conn.commit()
    conn.close()
    flash("Business details saved. They now appear on your invoices.", "success")
    return redirect(url_for("settings_page") + "#business")

def _remove_logo_file(name):
    if name and "/" not in name and "\\" not in name:
        try:
            os.remove(os.path.join(LOGO_DIR, name))
        except OSError:
            pass

@app.errorhandler(413)
def too_large(_err):
    flash("That file is too big. Please use an image under 2 MB.", "error")
    return redirect(url_for("settings_page") + "#business")

@app.route("/invoices")
@login_required
def invoice_history():
    conn = get_connection()
    invoices = conn.execute("""
        SELECT invoices.*, users.email AS created_by_email, """ + PAID_SQL + """ AS paid_cents
        FROM invoices
        LEFT JOIN users ON users.id = invoices.created_by
        WHERE invoices.company_id = ?
        ORDER BY invoices.id DESC
    """, (session["company_id"],)).fetchall()
    conn.close()
    return render_template("invoices.html", invoices=invoices)

# ---------- Reports ----------

@app.route("/low-stock")
@login_required
def low_stock():
    conn = get_connection()
    products = conn.execute(
        "SELECT * FROM products WHERE company_id = ? AND stock <= low_stock_threshold "
        "ORDER BY stock ASC", (session["company_id"],)).fetchall()
    conn.close()
    return render_template("low_stock.html", products=products)

if __name__ == "__main__":
    app.run(debug=os.getenv("FLASK_DEBUG", "0") == "1")
