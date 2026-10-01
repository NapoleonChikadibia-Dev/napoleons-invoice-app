"""
Standalone SMTP test. Run this on its own, outside the Flask app, so you get
the real error message instead of just "email delivery is not configured yet."

    python test_email.py you@example.com

Sends a one-line test email to the address you pass in, using SMTP_HOST,
SMTP_PORT, SMTP_FROM, SMTP_USERNAME, SMTP_PASSWORD from .env.
"""
import os
import smtplib
import sys
from email.message import EmailMessage

from env import load_local_env

load_local_env()

required = ["SMTP_HOST", "SMTP_FROM", "SMTP_USERNAME", "SMTP_PASSWORD"]
missing = [name for name in required if not os.getenv(name)]
if missing:
    print(f"Missing from .env: {', '.join(missing)}")
    sys.exit(1)

if len(sys.argv) != 2:
    print("Usage: python test_email.py your-email@example.com")
    sys.exit(1)

to_address = sys.argv[1]
host = os.getenv("SMTP_HOST")
port = int(os.getenv("SMTP_PORT", "587"))
sender = os.getenv("SMTP_FROM")
username = os.getenv("SMTP_USERNAME")
password = os.getenv("SMTP_PASSWORD")

print(f"Host: {host}")
print(f"Port: {port}")
print(f"From: {sender}")
print(f"Username: {username}")
print(f"Sending to: {to_address}")
print("-" * 40)

message = EmailMessage()
message["Subject"] = "Ledger SMTP test"
message["From"] = sender
message["To"] = to_address
message.set_content("This is a test email from your invoice app's SMTP setup. If you got this, it works.")

try:
    if port == 465:
        print("Connecting over SSL...")
        smtp = smtplib.SMTP_SSL(host, port, timeout=10)
    else:
        print("Connecting with STARTTLS...")
        smtp = smtplib.SMTP(host, port, timeout=10)
        smtp.starttls()

    print("Connected. Logging in...")
    smtp.login(username, password)
    print("Logged in. Sending...")
    smtp.send_message(message)
    smtp.quit()
    print("-" * 40)
    print("SUCCESS. Check the inbox at", to_address)

except smtplib.SMTPAuthenticationError as e:
    print("-" * 40)
    print("AUTH FAILED. The server rejected the username/password.")
    print(f"Server said: {e}")
    print()
    print("If this is Gmail: you almost certainly need an App Password, not")
    print("your normal Gmail password. Your regular password will not work")
    print("here even if it's correct, once 2-Step Verification is on (and")
    print("Gmail requires 2-Step Verification to even generate an App")
    print("Password). Go to myaccount.google.com > Security > 2-Step")
    print("Verification > App passwords, generate one, and put that")
    print("16-character value in SMTP_PASSWORD instead.")

except (smtplib.SMTPConnectError, OSError) as e:
    print("-" * 40)
    print("COULD NOT CONNECT to the SMTP server.")
    print(f"Details: {e}")
    print()
    print("Double check SMTP_HOST and SMTP_PORT, and that this machine's")
    print("network allows outbound connections on that port.")

except smtplib.SMTPException as e:
    print("-" * 40)
    print("SMTP ERROR while sending.")
    print(f"Details: {e}")

except Exception as e:
    print("-" * 40)
    print("UNEXPECTED ERROR.")
    print(f"{type(e).__name__}: {e}")
