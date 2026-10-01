# What changed

## Backend (app.py and friends)
- **Dashboard data**: `home()` now sends counts, this month vs last month, 6-month revenue, top 5 products, recent invoices and low stock.
- **Open-redirect fix**: `next=` after login only accepts paths on this site (`safe_next`).
- **Cancel race fix**: the invoice is flipped to cancelled *before* stock is restored, and only if it was still active, so a double-click can't return stock twice.
- **Forgot password was broken**: the route reads `email`, but the old form never sent it, so it always failed. The form now has an email field.
- **Low-stock alert level** can be set when adding a product (defaults to 5).
- **Money**: thousands separators, and `CURRENCY_SYMBOL` in `.env` (e.g. `₦`). New `money_short` filter for charts.
- **reset_password.py** used to run `DELETE FROM users`, wiping every company's logins. It now resets only the account in `ADMIN_EMAIL`.
- Closed a DB connection leak on invalid quantities.
- `setup.html` removed: no `/setup` route exists, so it could never render.

## Frontend
- Dark sidebar layout (drawer on mobile), KPI cards, CSS revenue chart, top products, recent invoices, running-low list.
- Unified login with Owner / Team tabs, 3-step signup, shared `auth_base.html`.
- First-login guided tour, setup checklist, one-time page hints, "Replay the tour" in the sidebar.
- Theme: Plus Jakarta Sans + JetBrains Mono, ledger green, automatic dark mode.
- `script.js` (GSAP) still runs on auth pages; guarded so a CDN failure doesn't throw.

## Not changed yet
- Company passcode and recovery passphrase flows still exist.
- No login rate limiting (signup has it, login doesn't).
- Invoices still lack seller details, due date, tax and paid/unpaid status.

## Install
Copy these files over your project. Your `.env` and `inventory.db` are not included and don't need to change,
apart from adding `SECRET_KEY` and optionally `CURRENCY_SYMBOL` (see `.env.example`).
