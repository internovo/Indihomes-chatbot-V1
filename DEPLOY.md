# Deploying the wati-webhook backend to Railway

Goal: give the backend a permanent public HTTPS URL so WATI no longer depends
on your laptop + ngrok. Data (SQLite) lives on a Railway persistent volume so
appointments survive redeploys.

---

## What changed in the code for deployment

- `appointments_db.py` — the DB path now comes from `APPOINTMENTS_DB_PATH`
  (set it to a file on the mounted volume). Falls back to the local folder when
  the env var is unset, so nothing changes for local dev.
- `calendar_service.py` — Google credentials now load from the
  `GOOGLE_SERVICE_ACCOUNT_JSON` env var (the full key JSON pasted in), falling
  back to the on-disk `service_account.json` locally. This is needed because the
  key file is git-ignored and must NOT be committed.
- `Procfile` — tells Railway how to start the app on its assigned port:
  `web: uvicorn app:app --host 0.0.0.0 --port $PORT`

`.env`, `service_account.json`, and `appointments.db` stay git-ignored — none of
them go to GitHub. Every secret is set directly in Railway instead.

---

## Step 1 — Commit and push the code

From `C:\Users\admin\Desktop\wati-webhook` in PowerShell:

```powershell
git add app.py email_service.py appointments_db.py calendar_service.py Procfile DEPLOY.md
git commit -m "Add advisor email + Railway deployment config"
git push
```

If `appointments.db` was ever committed in the past, stop tracking it (it should
never be in the repo):

```powershell
git rm --cached appointments.db
git commit -m "Stop tracking local SQLite DB"
git push
```

---

## Step 2 — Create the Railway service

1. Go to railway.app, sign in, and open (or create) a project.
2. **New → Deploy from GitHub repo** → pick the `wati-webhook` repo.
3. Railway detects Python, installs `requirements.txt`, and starts it with the
   Procfile. The first deploy will fail health checks until the variables and
   volume below are set — that's expected.

---

## Step 3 — Add the persistent volume (for SQLite)

1. In the service → **Settings → Volumes → New Volume**.
2. Mount path: `/data`
3. Save. This directory persists across every redeploy.

---

## Step 4 — Set the environment variables

Service → **Variables → Raw Editor**, paste the block below, then fill in the
real values (copy them from your local `.env`). Do NOT paste secrets into this
file or into GitHub.

```
GROQ_API_KEY=your_groq_key
GROQ_MODEL=llama-3.3-70b-versatile

GOOGLE_CALENDAR_ID=your_calendar_id
GOOGLE_SERVICE_ACCOUNT_JSON={"type":"service_account", ... full JSON on one line ... }
ADVISOR_EMAILS=arpit@internovo.in,aarti@internovo.in,tech@internovo.in
BUSINESS_HOURS_START=10
BUSINESS_HOURS_END=19
SLOT_MINUTES=60
TIMEZONE=Asia/Kolkata

SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=internovoventures@gmail.com
SMTP_APP_PASSWORD=your_16_char_app_password
SMTP_FROM_NAME=Indihomes Bookings
NOTIFY_CC=

APPOINTMENTS_DB_PATH=/data/appointments.db
```

### Getting `GOOGLE_SERVICE_ACCOUNT_JSON` right

The value must be the entire contents of `service_account.json` as a single
line. Easiest way to produce it, from PowerShell:

```powershell
Get-Content service_account.json -Raw | ConvertFrom-Json | ConvertTo-Json -Compress
```

Copy the output and paste it as the value of `GOOGLE_SERVICE_ACCOUNT_JSON`.
(Railway's Raw Editor handles long single-line values fine.)

---

## Step 5 — Generate the public domain

Service → **Settings → Networking → Generate Domain**. Railway gives you a URL
like `https://wati-webhook-production.up.railway.app`. That is your new base URL.

Optional: set **Settings → Deploy → Health Check Path** to `/health`.

---

## Step 6 — Verify

Open in a browser (replace with your real domain):

```
https://<your-app>.up.railway.app/health
```

You want to see:
- `"properties_loaded": 74`
- `"calendar": "connected"`
- `"email": "connected"`
- `"advisors_loaded": 3`

If `calendar` says NOT CONFIGURED → the `GOOGLE_SERVICE_ACCOUNT_JSON` value is
malformed (re-run the compress command). If `email` says NOT CONFIGURED → the
`SMTP_*` vars are missing.

---

## Step 7 — Point WATI at the new URL

In the WATI bot builder, update the URL in ALL FOUR webhook nodes — replace the
old ngrok base (`https://jawed-oven-climate.ngrok-free.dev`) with your Railway
base URL. The paths stay the same:

- `.../location`
- `.../search`
- `.../available-slots`
- `.../book-slot`

Test each webhook once in the builder so the mapping re-binds, then run a full
WhatsApp conversation end to end (search a location, book a slot, confirm the
advisor email arrives).

Once this works, ngrok and the local uvicorn server are no longer needed to keep
the bot running — Railway is always on.

---

## Notes

- Redeploys happen automatically on every `git push` to the tracked branch.
- The SQLite volume is a single-instance store. Keep the service at 1 replica
  (the default) — running multiple replicas would split the DB.
- To inspect bookings in production, use Railway's shell on the service:
  `sqlite3 /data/appointments.db "SELECT * FROM appointments;"`
