# Public access for EggSort+

`127.0.0.1` and `localhost` point to the device opening the link. They cannot
be used in an invitation sent to somebody on another computer or network.
EggSort+ uses `PUBLIC_BASE_URL` when creating invitation links and Google OAuth
callbacks.

Because EggSort+ connects to hardware on the administrator's computer, a tunnel
is a better fit than moving only the Flask application to a cloud server.

## Temporary public URL for testing

Cloudflare Quick Tunnels provide a temporary HTTPS URL. They are intended only
for testing and the URL changes whenever the tunnel is recreated.

1. Install `cloudflared` for Windows using Cloudflare's official instructions:
   <https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/>
2. Start EggSort+ in the project terminal:

   ```powershell
   .\.venv\Scripts\python.exe app.py
   ```

3. In a second PowerShell window, start the tunnel:

   ```powershell
   cloudflared tunnel --url http://localhost:5000
   ```

4. Copy the printed HTTPS address. It will resemble:

   `https://random-words.trycloudflare.com`

5. Put that origin in `.env`, with no trailing slash:

   ```dotenv
   PUBLIC_BASE_URL=https://random-words.trycloudflare.com
   ```

6. In the Google OAuth web client, add this exact **Authorized redirect URI**:

   `https://random-words.trycloudflare.com/auth/google/callback`

   The **Authorized JavaScript origins** field can remain empty.

7. Restart EggSort+ so it reloads `.env`. Leave both EggSort+ and
   `cloudflared` running.
8. In User Management, create a new invitation or use the reinvite button.
   Previously generated localhost links do not change automatically.

The recipient can now open the emailed `https://.../accept-invite/...` URL from
any Internet connection while both processes are running.

## Stable URL for real use

Do not send long-lived invitations through a Quick Tunnel because its random
address changes after restart. Create a named Cloudflare Tunnel and map a
hostname that you control, for example:

- Public hostname: `eggsort.example.com`
- Local service: `http://localhost:5000`
- `.env`: `PUBLIC_BASE_URL=https://eggsort.example.com`
- Google redirect URI:
  `https://eggsort.example.com/auth/google/callback`

Cloudflare documents the named-tunnel and published-application setup here:
<https://developers.cloudflare.com/tunnel/setup/>. A tunnel uses an outbound
connection, so it does not require opening an inbound router port.

The computer running EggSort+, its Internet connection, the Flask process, and
the tunnel must remain online. Use a stable `SECRET_KEY` in `.env`; changing it
signs out existing sessions.
