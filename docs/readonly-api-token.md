# Read-only API token

**Bottom line:** create the token through an authenticated Manager request, store
it only in private secret configuration, verify its scope, and revoke the old
token after a successful replacement.

This procedure connects a reporting or monitoring service to Manager. A
read-only token can read safe API data. It cannot change devices, settings, or
other data. The token value is shown only when it is created.

## Before you start

Make sure that:

- You can sign in to Manager as an administrator or an operator. A viewer
  cannot create tokens.
- You know the Manager URL and the service that will use the token.
- You can edit that service's private environment file.
- The environment file is not in Git and has file mode `0600`.

Never put a token in Git, chat, a ticket, a shell command, or a log. Do not use
a direct database insert. The Manager API records the owner and the audit event.

## Create the token

1. Sign in to Manager as an administrator or an operator.
2. Open the browser developer console on the Manager page. Send this request
   from the same authenticated browser session. Replace the name with the name
   of the service. The script copies the token to the clipboard and shows only
   the token `id`. It does not write the token to the console.

   ```javascript
   fetch("/api/tokens", {
     method: "POST",
     headers: {"Content-Type": "application/json"},
     body: JSON.stringify({name: "reporting-service", scopes: "read"})
   }).then(response => response.json()).then(data => {
     if (!data.token) { console.log("Token not created:", data.detail); return; }
     copy(data.token);
     console.log("Token copied to clipboard. Token id:", data.id);
   })
   ```

   The `copy()` function is a browser developer console utility. It is not
   available in page scripts.

3. Record the token `id` for later revocation. Paste the token from the
   clipboard into the service secret store. Then clear the clipboard.

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

Run these checks in `bash` from a host that can reach Manager. Enter the token
when prompted. The `read` command does not echo it. The `printf` built-in
sends the header to `curl` through standard input (stdin). The token does not
appear in the process arguments. The `-H @-` option needs curl 7.55.0 or later.

```bash
read -rs MANAGER_TOKEN
printf '\n'

printf 'Authorization: Bearer %s\n' "$MANAGER_TOKEN" |
  curl -sS -o /dev/null -w '%{http_code}\n' -H @- \
    "https://manager.example/api/aps"

printf 'Authorization: Bearer %s\n' "$MANAGER_TOKEN" |
  curl -sS -o /dev/null -w '%{http_code}\n' -H @- \
    -X POST "https://manager.example/api/updates/check"

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
5. Delete the old token from an authenticated session. The token owner or an
   administrator can delete a token:

   ```javascript
   fetch("/api/tokens/OLD_ID", {
     method: "DELETE"
   }).then(response => response.json()).then(console.log)
   ```

If a token is exposed, revoke it immediately. Then create a replacement and
restart the service.
