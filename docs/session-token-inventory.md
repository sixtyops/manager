# Session and token storage inventory

## Key points

**Key points:** This inventory describes current `main` at `f1a9644`. It covers Manager sessions, API tokens, OIDC ID tokens, and Tachyon device session tokens. It does not claim that normal authentication logs expose token values.

**Detail:** PRs #558 and #271 are pending changes. Their behavior is separate from the current-main findings below.

## Manager sessions

**Key points:** Local login and the OIDC callback create the same 24-hour Manager session. Current `main` registers new and successfully read session IDs with `register_secret`.

**Detail:** `auth.create_session` makes a UUID, registers it, and stores the ID, user name, IP address, and expiry in SQLite `sessions`. The ID is returned in an `HttpOnly`, `SameSite=Lax` cookie. The `Secure` flag depends on the request scheme or trusted proxy. The cookie max age is 24 hours. A new login creates a new session; requests do not extend its expiry. `database.get_session` returns only a non-expired row and registers its stored ID. HTTP and WebSocket authentication use this read. Logout deletes the session and cookie. Startup and hourly cleanup delete expired sessions and the related OIDC ID token setting. Password setup can delete all sessions. No session ID is written in the nearby login, logout, or authentication audit messages reviewed here.

The session ID is stored as plaintext in SQLite. Registration protects it only in sanitized log output in the running process. PR #518 added registration at creation and successful stored-session reads; this behavior is already in current `main`.

## API tokens

**Key points:** Manager API tokens are stored as SHA-256 hashes. The full token is returned once at creation. Current `main` does not register the full token with `register_secret`.

**Detail:** `generate_api_token` creates a `tach_` token from 32 URL-safe random bytes, then returns the full value, its SHA-256 hash, and a display prefix. `POST /api/tokens` stores the hash, prefix, owner, scopes, and optional expiry in SQLite. It returns the full token in the response. The default expiry is none; callers can set 1–365 days. There is no refresh flow. Authentication hashes the presented Bearer token for lookup, rejects expired or disabled-owner tokens, and updates `last_used_at`. Daily housekeeping deletes expired rows. Listing returns the prefix and metadata, not the hash or full token. Deletion revokes the row. The nearby audit log records token ID and name, not the full token. No inspected authentication log call records the presented Bearer value.

The database does not store the API token in plaintext, but the creation path does not register its returned full value for log redaction. This is a source-coverage gap; this inventory did not reproduce a normal log leak.

## OIDC ID tokens

**Key points:** The OIDC callback stores the ID token in the encrypted settings store for logout. It removes that value on logout or session cleanup.

**Detail:** The setting key includes the Manager session ID. The `oidc_id_token_` prefix marks it for encryption at rest. Secret settings register the clear value when read. Logout reads the value for the provider logout request, then deletes it. Expired-session cleanup and user-session deletion also remove the setting. This token is separate from the Manager session ID and from API tokens. PR #271 has a separate OIDC authentication change; it is not part of current-main behavior in this inventory.

## Tachyon device session tokens

**Key points:** Tachyon login tokens are held by the client in process memory. They are not stored in the Manager database. Current `main` does not register them with `register_secret`.

**Detail:** `TachyonClient.login` reads the device's `token` cookie from a temporary cookie file into `self._token`, then removes the file. Authenticated curl requests send the token as a cookie header. Poller clients cache authenticated sessions in memory. CPE clients expire from that cache after ten minutes. Other clients expire ten minutes after last use. Polling also removes clients after stale authentication, failures, or device removal. The client checks whether a token still works and logs in again when it is rejected. The device controls the token's own expiry; Manager does not refresh it with a refresh token.

Nearby current-main calls log curl stderr on login and firmware-upload failures, a login error response excerpt, and upload response or device error data. The login and upload subprocess arguments also include credentials or the session cookie. These paths make device-token registration and safe request diagnostics relevant. This inventory does not show that a normal device response echoes a token.

## Pending changes and limits

**Key points:** Do not attribute pending PR behavior to current `main`.

**Detail:** PR #558 proposes moving Tachyon curl options from process arguments to a private stdin pipe and removing raw curl stderr and config error details. Its published evidence and code are not included in this `main` inventory. PR #271 changes a separate OIDC authentication hunk; its parked push denial remains in force. Recheck both PRs before any follow-up implementation. This inventory uses source inspection and synthetic examples only. It does not use live credentials, devices, or host data.
