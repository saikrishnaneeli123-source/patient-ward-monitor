# Deploying the Ward Monitor

## What this is, and what it is not

This is a **web application** — Python on a server, HTML in a browser. It is not
an Android app, and there is no APK in this repository to upload anywhere.

That gives you three ways to get it onto a phone or tablet, in increasing order
of effort:

| | What the user does | What you need | Effort |
|---|---|---|---|
| **1. Browser** | Opens a URL | A server with HTTPS | Lowest |
| **2. Installed web app** | "Add to Home screen" — real icon, no address bar | Same as above | None extra, already built |
| **3. Play Store listing** | Installs from the Play Store | The above, plus an Android wrapper and a Play developer account | Highest |

**Option 2 already works.** The app ships a web manifest, icons and a service
worker, so Android Chrome offers "Install app" and iOS Safari offers "Add to Home
Screen". It then runs full-screen with its own icon, and most ward staff cannot
tell it from a store-installed app. If you only need staff on a hospital tablet
to use it, stop after step 1 below.

Option 3 is the only route to an actual Play Store listing, and it still runs
this same web app inside a thin Android shell.

---

## Step 1 — put the server somewhere with HTTPS

Everything else depends on this, including the Play Store route. Android will
not accept a wrapper around an HTTP site.

```bash
git clone <this repo> && cd patient-ward-monitor

# Generate a session key once, and keep it.
python -c "import secrets; print('WARD_SECRET_KEY=' + secrets.token_urlsafe(48))" >> .env
echo "ANTHROPIC_API_KEY=sk-ant-..." >> .env    # optional; enables scan extraction

docker compose up -d --build
```

The app listens on port 8000. Put a TLS-terminating reverse proxy in front of it
— Caddy is the shortest path:

```
ward.example.org {
    reverse_proxy localhost:8000
}
```

Then create the first account:

```bash
docker compose exec ward python -m scripts.create_user \
  --username admin --name "Ward Admin" --role admin
```

The container refuses to start in production without `WARD_SECRET_KEY`, and
forces `Secure` on session cookies, so it cannot quietly run insecurely.

**Run one container, not several.** The audit log is a linear hash chain and the
login throttle is in-process; both assume a single writer. To scale beyond one
instance you need Postgres (`WARD_DATABASE_URL=postgresql://…`) and an advisory
lock around audit appends. Until then, one container serves a ward comfortably.

## Step 2 — install it on the phone

Open the site in Chrome on Android: the browser offers **Install app**, or use
⋮ → *Add to Home screen*. On iOS, Safari → Share → *Add to Home Screen*.

Two things worth knowing:

- **It does not work offline, on purpose.** The service worker caches the
  stylesheet, icons and an offline page — and nothing else. A nurse acting on a
  cached NEWS2 score from three hours ago is worse off than one told plainly that
  the device is offline, and a cached chart is patient data sitting on a shared
  tablet outside the server's control.
- **Installing does not bypass the login.** Sessions last one shift by default
  (`WARD_SESSION_MAX_AGE`).

## Step 3 — publish to the Play Store

Google Play accepts Android packages. To list a web app you wrap it in a
**Trusted Web Activity** (TWA): a minimal Android app that opens your site
full-screen with no browser UI. Google's own tool, Bubblewrap, generates it.

**What you need first**

- A public HTTPS domain serving the app (step 1) — not an IP, not localhost.
- A **Google Play Developer account** — a one-time registration fee (US$25 at
  time of writing).
- Node.js, JDK 17 and the Android SDK on your build machine.
- A **privacy policy at a public URL**. Play requires one, and an app handling
  patient data will not pass review without a real one.

**Building the wrapper**

```bash
npm install -g @bubblewrap/cli

bubblewrap init --manifest https://ward.example.org/manifest.webmanifest
# Accept the defaults; it reads the name, icons and colours from the manifest.

bubblewrap build
# Produces app-release-bundle.aab (upload this) and a signing key. Back the key
# up — losing it means you can never update the listing.
```

**Linking the app to the domain**

Android only drops the address bar if the site vouches for the app. Bubblewrap
prints an `assetlinks.json`; save it to `app/static/well-known/assetlinks.json`
and redeploy. This app already serves it at `/.well-known/assetlinks.json`.
Verify with:

```bash
curl https://ward.example.org/.well-known/assetlinks.json
```

Get this wrong and the app still works, but shows a browser bar at the top.

**Submitting**

Upload the `.aab` in the Play Console, then complete the Data Safety form, the
content rating, and the Health apps declaration. The Console will also tell you
its current testing requirements before a new account can publish to production
— these change, so read what it shows you rather than trusting a guide.

### Before you submit a patient-data app to a public store

Be aware of what listing this publicly commits you to:

- **Play's health and medical policies** apply. An app that displays patient
  records and an early-warning score will be assessed as a health app, and Google
  may ask who operates it and under what clinical governance.
- **A public listing means anyone can install it.** The app is only as private as
  the server behind it — publishing does not add access control. Most hospitals
  distribute internal tools through **Managed Google Play** (private, org-only)
  or MDM, not the public store. That is very likely what you actually want.
- **The regulatory question does not go away.** NEWS2 scoring plus automated
  transcription in a clinical workflow may bring this under medical-device
  software rules in your jurisdiction (UKCA/CE under MDR, FDA CDS guidance,
  CDSCO in India). Publishing it to a store is a stronger claim than running it
  on one ward, and it is worth taking advice before you do.

If the goal is "our nurses can tap an icon on the ward tablet", **step 2 gets you
there today** with none of this.

---

## Production checklist

| | |
|---|---|
| ☐ | `WARD_SECRET_KEY` set and backed up (a new one logs everyone out) |
| ☐ | HTTPS terminating in front of the app; `WARD_COOKIE_SECURE` left at `true` |
| ☐ | First admin created; the demo accounts from `seed_demo` **not** present |
| ☐ | `/data` on an encrypted volume — it holds scans and the database |
| ☐ | Backups of `/data`, tested by restoring one |
| ☐ | `ANTHROPIC_API_KEY` set only if your organisation has approved sending case sheets to the Claude API |
| ☐ | Audit chain verified after go-live: `GET /api/audit/verify` |
| ☐ | Someone named as responsible for reviewing unverified records daily |

## Environment variables

See `.env.example`. The ones that matter in production:

| Variable | Effect |
|---|---|
| `WARD_ENV=production` | Refuses unsafe defaults; forces secure cookies |
| `WARD_SECRET_KEY` | Signs session cookies. Required in production |
| `WARD_DATABASE_URL` | SQLite by default; Postgres for multi-instance |
| `WARD_UPLOAD_DIR` | Where scans are written |
| `ANTHROPIC_API_KEY` | Enables automatic case-sheet extraction |
| `WARD_SESSION_MAX_AGE` | Session lifetime, default one shift |
