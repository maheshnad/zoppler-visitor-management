# Zoppler Systems Visitor Management — PostgreSQL edition

This package contains the visitor website **and its Python API**, which connects to your existing PostgreSQL installation. It does **not** install or bundle PostgreSQL. Do not double-click `index.html`: the app must run through the server.

## Setup on Windows

1. Install Python 3.11+ and make sure PostgreSQL is running.
2. In pgAdmin, create a database named `zoppler_visitors` and a dedicated login role `visitor_app` with a strong password. Make that role the database owner (or grant it schema CREATE/USAGE and table privileges). Do not use your postgres superuser in the application.
3. Extract this ZIP. Open a terminal in the extracted folder and run:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

4. In the same terminal, configure environment variables (replace placeholders; URL-encode special characters in the password):

```powershell
$env:DATABASE_URL = 'postgresql://visitor_app:YOUR_PASSWORD@localhost:5432/zoppler_visitors'
$env:SECRET_KEY = 'PASTE_A_RANDOM_SECRET_OF_AT_LEAST_32_CHARACTERS'
$env:COOKIE_SECURE = '0'
```

Generate a random secret with `py -c "import secrets; print(secrets.token_urlsafe(48))"`. `COOKIE_SECURE=0` is **only for local HTTP testing**. For HTTPS deployment use `COOKIE_SECURE=1`.

5. Create your management account and start the app:

```powershell
$env:FLASK_APP = 'server:app'
flask create-admin
python server.py
```

6. Open `http://127.0.0.1:5000`. Submit a visitor request and sign in under Management. Tables are created automatically at startup.

## Important deployment guidance

The app uses same-origin HTTP-only session cookies, CSRF tokens for authenticated changes, hashed passwords, parameterized SQL, server-side role enforcement, status transition checks, an audit log, and basic in-process submission/reset rate limits. The public status endpoint returns only status/date/time. The database stores personal data: set retention and access policies appropriate to your organization. For production, add reverse-proxy rate limiting and anti-bot controls, use HTTPS, back up PostgreSQL, and configure an appropriate production secret. Use `waitress-serve --listen=127.0.0.1:8000 server:app` behind your HTTPS proxy instead of the development server. Configure forwarded headers only for a trusted proxy.

The app creates its own `admins`, `visits`, and `visit_audit` tables in the database selected by `DATABASE_URL`. If you already have an existing visitor table/schema, share its table/column structure so the integration can be adapted instead of creating new tables. Do not share actual database passwords in chat.

## Password-reset email

Management password resets are limited to the five allowlisted addresses in `server.py`. Configure `SMTP_HOST`, `SMTP_PORT`, `SMTP_TLS`, `SMTP_USERNAME`, `SMTP_PASSWORD`, and `SMTP_FROM` in `.env` to enable delivery of 30-minute, one-time reset links. Until SMTP is configured, the existing management login continues to work but reset requests return a configuration message.

When SMTP is configured, every new visitor request sends an approval notification to all five authorized management addresses. All five management addresses and the visitor also receive an email whenever a request is approved, rejected, checked in, or checked out. Messages exclude Aadhaar and photo data. Email delivery failure is logged and does not discard an otherwise valid visitor request or status change.

Visitor Aadhaar numbers and photos are sensitive personal data. Photos are restricted to authenticated management users and Aadhaar is masked in the dashboard. Define and enforce an appropriate retention/deletion policy before production use.

## QR poster

After HTTPS deployment, generate a QR code pointing to the site's public URL. The existing poster QR will need replacement. Do not use `localhost` in a QR intended for visitor phones.

## Stable ngrok address

The free ngrok account reserves `https://laboring-grumpily-trembling.ngrok-free.dev/` for this application. Run `./run-stable.ps1` to start both Waitress on `http://127.0.0.1:5000` and the stable public tunnel. The computer, PostgreSQL, internet connection, and PowerShell process must remain running. `visitor-access-qr-latest.png` points to this stable address.

## Free cloud deployment

The repository includes a `Procfile` and Python version declaration for a Koyeb web-service deployment. A cloud deployment also requires a durable external PostgreSQL database such as Neon; never upload `.env` or local database credentials. Configure `DATABASE_URL`, `SECRET_KEY`, `COOKIE_SECURE=1`, and the SMTP settings as encrypted environment variables in the hosting dashboard. Free services can sleep when idle and are not intended for production availability guarantees.
