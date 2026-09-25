# EggSort+ Google sign-in setup

EggSort+ uses Google OpenID Connect for the administrator and invite-only
password accounts for staff.

## 1. Create Google credentials

1. Open Google Cloud Console and select or create a project.
2. Configure the OAuth consent screen.
3. Create an **OAuth client ID** with application type **Web application**.
4. Add this exact authorized redirect URI for local use:

   `http://127.0.0.1:5000/auth/google/callback`

If you open EggSort+ using `localhost`, add this separately:

   `http://localhost:5000/auth/google/callback`

## 2. Install the added dependency

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## 3. Start EggSort+

Use the client ID and secret shown in Google Cloud. Do not commit the secret to
Git or paste it into a source file.

```powershell
$env:GOOGLE_CLIENT_ID = "your-client-id.apps.googleusercontent.com"
$env:GOOGLE_CLIENT_SECRET = "your-client-secret"
$env:SECRET_KEY = "replace-with-a-long-random-secret"
.\.venv\Scripts\python.exe app.py
```

Then open `http://127.0.0.1:5000`.

The administrator is `capstonecutie1@gmail.com`. On the first Google sign-in,
the admin creates a password. Future sign-ins can use either Google or the
shared username/email and password form. The admin can create staff invitations
from User Management. Each invitation link works once and expires after 24
hours. Staff choose their own password and then use the same login form.

## Access rules

- Admin: Google verification on first use, then either Google or password
  sign-in, with access to all pages and User Management.
- Staff: invite-only username/password sign-in and all operational pages, but
  no User Management page or user APIs.
- Staff cannot self-register, and staff Google sign-in is rejected.
- Invitation links are single-use and expire after 24 hours.

For a deployed site, use HTTPS and add the deployed callback URL to the same
Google OAuth client.
