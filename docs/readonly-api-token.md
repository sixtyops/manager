# Read-only API token

**Bottom line:** create the token through an authenticated Manager request, store
it only in private secret configuration, verify its scope, and revoke the old
token after a successful replacement.

This procedure connects a reporting or monitoring service to Manager. A
read-only token can read safe API data. It cannot change devices, settings, or
other data. The token value is shown only when it is created.

## Before you start

Make sure that:

- You can sign in to Manager as an administrator.
- You know the Manager URL and the service that will use the token.
- You can edit that service's private environment file.
- The environment file is not in Git and has file mode `0600`.

Never put a token in Git, chat, a ticket, a shell command, or a log. Do not use
a direct database insert. The Manager API records the owner and the audit event.

## Create the token

1. Sign in to Manager as an administrator.
2. Send this request from the same authenticated browser session. Replace the
   name with the name of the service. Keep the returned token private.

   ```javascript
   fetch("/api/tokens", {
     method: "POST",
     headers: {"Content-Type": "application/json"},
     body: JSON.stringify({name: "reporting-service", scopes: "read"})
   }).then(response => response.json()).then(console.log)
   ```

3. Record the returned `id` for later revocation. Copy the returned `token` to
   the service secret store. Do not save the browser output.

The API accepts an optional `expires_days` value from `1` to `365`. Use an
expiry only when the service has a tested rotation schedule.

## Place and activate the token

1. Add the token to the service's private environment file. Use the variable
   name required by that service.
2. Check the file mode:

   ```bash
   chmod 600 /path/to/service.env
   ```

3. Validate the compose file without printing its values:

   ```bash
   docker compose --env-file /path/to/service.env config --quiet
   ```

4. Restart only the service that needs the token. Use its normal deployment
   procedure. Record the deployment time and the service revision.

## Verify the connection

Run these checks from a host that can reach Manager. Enter the token when
prompted. The `read` command does not echo it.

```bash
read -rs MANAGER_TOKEN
printf '\n'

curl -fsS \
  -H "Authorization: Bearer $MANAGER_TOKEN" \
  "https://manager.example/api/aps" >/dev/null

curl -sS -o /dev/null -w '%{http_code}\n' \
  -X POST "https://manager.example/api/updates/check" \
  -H "Authorization: Bearer $MANAGER_TOKEN"

curl -sS -o /dev/null -w '%{http_code}\n' \
  "https://manager.example/api/aps"
unset MANAGER_TOKEN
```

Confirm these results:

- The read request returns `200`.
- The write request returns `403`.
- The request without a token returns `401`.
- The service reports a healthy connection.

If a check fails, restore the previous environment file and restart the
service. Keep the old token until the replacement passes all checks.

## Rotate or revoke a token

1. Create a replacement token with a new name.
2. Place it in the service secret store.
3. Validate the compose file and restart the service.
4. Repeat the three verification checks.
5. Delete the old token from an authenticated administrator session:

   ```javascript
   fetch("/api/tokens/OLD_ID", {
     method: "DELETE"
   }).then(response => response.json()).then(console.log)
   ```

If a token is exposed, revoke it immediately. Then create a replacement and
restart the service.
