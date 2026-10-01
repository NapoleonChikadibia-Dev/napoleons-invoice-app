# Deploying to PythonAnywhere

PythonAnywhere keeps files between requests, so the SQLite database (`inventory.db`) is safe there.
Don't use Vercel for this app: it wipes files, and you'd lose your data.

## 1. Before you start
- **Make a new Gmail app password** (Google Account → Security → App passwords) and revoke the old one.
- Generate a secret key on your laptop:
  ```
  python -c "import secrets; print(secrets.token_hex(32))"
  ```

## 2. Upload the code
On PythonAnywhere, open a **Bash console**:
```
git clone https://github.com/V1ru5-001/<your-repo>.git invoice
cd invoice
python3.11 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```
(Or upload the files with the **Files** tab. Never upload `.env` to GitHub.)

## 3. Create `.env` on the server
In the **Files** tab, create `invoice/.env`:
```
SECRET_KEY=<the 64-character key you generated>
APP_ENV=production
TRUST_PROXY=1
APP_BASE_URL=https://<yourname>.pythonanywhere.com

SMTP_HOST=smtp.gmail.com
SMTP_PORT=465
SMTP_FROM=<your gmail>
SMTP_USERNAME=<your gmail>
SMTP_PASSWORD=<your NEW app password>
```

## 4. Create the web app
**Web** tab → **Add a new web app** → **Manual configuration** → Python 3.11.

- **Virtualenv:** `/home/<yourname>/invoice/venv`
- **WSGI configuration file:** open it, delete everything, and paste:
  ```python
  import sys
  path = "/home/<yourname>/invoice"
  if path not in sys.path:
      sys.path.insert(0, path)
  from wsgi import app as application
  ```
- **Static files:** URL `/static/` → Directory `/home/<yourname>/invoice/static`
- **Force HTTPS:** turn it on.

Click **Reload**, then open `https://<yourname>.pythonanywhere.com`.

## 5. Check it works
- The landing page loads.
- Sign up with a real email. The verification link should start with your `https://` address, not `127.0.0.1`.
- If you see an error page, open **Web → Error log**. A message about `SECRET_KEY` means step 3 wasn't saved correctly.

## Uploaded logos
Logos are saved in `static/uploads/logos/`. They're already served by the `/static/` mapping above.
That folder is in `.gitignore`, so `git pull` never overwrites it. Back it up along with `inventory.db`.

## Updating later
```
cd ~/invoice && git pull && source venv/bin/activate && pip install -r requirements.txt
```
Then press **Reload** on the Web tab. Database changes apply on their own when the app starts.

## Running the tests
```
pip install pytest
python -m pytest -q tests
```
