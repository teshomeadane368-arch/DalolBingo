DALOL BINGO — DEMO RELEASE

Project files:
- server.py: Python/FastAPI + Telegram bot backend
- index.html: Telegram Mini App frontend
- requirements.txt: Python dependencies

IMPORTANT: This package is a DEMO / TEST-POINTS prototype only.
Cash deposits and cash withdrawals are disabled in both the interface and API.
Do not enable real-money play or payments until Ethiopian legal requirements,
payment-provider approval, security review, and independent testing are complete.

Required environment variables:
- BOT_TOKEN: Telegram bot token
- ADMIN_ID: numeric Telegram admin user ID
- APP_URL: public HTTPS URL of this deployment
- CORS_ORIGINS: allowed origins; normally APP_URL
- DB_PATH: persistent SQLite path, e.g. /var/data/dalol_bingo.db
- SUPPORT_USERNAME: support Telegram username
- MIN_PLAYERS: optional minimum players (default 2)

Run with exactly ONE worker/process because active game rooms are in memory:
  uvicorn server:app --host 0.0.0.0 --port 8000 --workers 1

Deploy server.py and index.html in the same working directory.
Mount persistent storage and set DB_PATH to that disk.
Set the Telegram bot Web App URL to APP_URL.

Before launch:
1. Replace the placeholder APP_URL with your actual HTTPS deployment URL.
2. Set BOT_TOKEN and ADMIN_ID as private environment variables.
3. Set SUPPORT_USERNAME to a real support account you control.
4. Test only with demo points.
5. Keep the app in demo mode until all legal and technical approvals are confirmed.

Known technical limitations:
- Active game rooms remain in process memory; a restart or multiple server instances can interrupt a live round.
- SQLite wallet columns use REAL; production-grade accounting would require integer minor units and an auditable ledger.
- Payment-provider integration, penetration testing, load testing, and live payment testing are not included.
