# OIDC / SSO Setup Guide

SixtyOps supports Single Sign-On via OpenID Connect (OIDC). This guide covers
setup with **Authentik** as the identity provider.

## Prerequisites

- Authentik instance accessible over HTTPS
- SixtyOps instance with the SSO/OIDC feature enabled

## Authentik Configuration

### 1. Create an Application and Provider

1. In Authentik Admin, go to **Applications > Providers**
2. Create a new **OAuth2/OpenID Provider**
   - Name: `sixtyops`
   - Client type: Confidential
   - Redirect URI: `https://<your-sixtyops-url>/auth/oidc/callback`
   - Post-Logout Redirect URI: `https://<your-sixtyops-url>/login`
3. Create a new **Application** linked to this provider
   - Slug: `sixtyops`

> The post-logout redirect URI must be registered for RP-Initiated Logout to
> redirect users back to the SixtyOps login page after signing out of Authentik.

### 2. Create Groups Scope Mapping

Authentik does not include user groups in the default OIDC scopes. You must
create a custom property mapping:

1. Go to **Customization > Property Mappings**
2. Click **Create** and select **Scope Mapping**
3. Configure:
   - Name: `OIDC Groups Scope`
   - Scope name: `groups`
   - Expression:
     ```python
     return {"groups": [group.name for group in request.user.ak_groups.all()]}
     ```
4. Go back to your **sixtyops provider** > Protocol Settings > Scopes
5. Add the new `OIDC Groups Scope` mapping to the provider's scopes

Without this mapping, group-based access control in SixtyOps will not work.

### 3. Create a User Group

1. Go to **Directory > Groups**
2. Create a group (e.g., `sixtyops-admins`)
3. Add users who should have access to SixtyOps

## SixtyOps Configuration

### Via the Settings UI

1. Navigate to **Settings > SSO / OIDC**
2. Fill in:
   - **Provider URL**: `https://<authentik>/application/o/sixtyops/`
   - **Client ID**: from the Authentik provider
   - **Client Secret**: from the Authentik provider
   - **Redirect URI**: `https://<sixtyops>/auth/oidc/callback`
   - **Allowed Group**: the Authentik group name (e.g., `sixtyops-admins`)
   - **Scopes**: `openid email profile groups` (this is the default)
3. Click **Save SSO**, then **Test** to verify discovery

> **Note:** The `groups` scope is not part of the OIDC standard — it is
> specific to Authentik (and Keycloak). If using a different identity provider,
> check its documentation for how to include group membership in the ID token
> and set the **Scopes** field accordingly. Behaviour for an unrecognized scope
> varies by provider: some ignore it, but **Microsoft Entra (Azure AD) rejects
> the entire authorization request** (`AADSTS650053`) rather than ignoring it —
> see the Entra section below.

## Microsoft Entra (Azure AD)

Entra does **not** accept `groups` as an OAuth scope. Requesting it aborts login
with `AADSTS650053: ... asked for scope 'groups' that doesn't exist`. Group
membership on Entra is delivered as a **token claim** configured on the app
registration, not requested as a scope.

1. **Provider URL** — use the v2.0 authority:
   `https://login.microsoftonline.com/<tenant-id>/v2.0`
   (its discovery document points at the `/oauth2/v2.0/token` endpoint).
2. **Scopes** — set to `openid email profile` (drop `groups`).
3. **Client type = confidential (Web).** Under **Authentication**, register
   `https://<sixtyops>/auth/oidc/callback` under the **Web** platform (exact
   scheme/host/path), and set **Allow public client flows** to **No**. SixtyOps
   is a server-side app that sends a client secret — if the callback is under
   the **Single-page application (SPA)** (or Mobile/desktop) platform, Entra
   treats it as a public client and the token exchange fails with `AADSTS700025:
   Client is public so neither 'client_assertion' nor 'client_secret' should be
   presented`. A redirect URI can only live under one platform; move it out of
   SPA and into Web.
4. **Group claim** — open **Token configuration > Add groups claim**, pick the
   group types (e.g. **Security groups**), then in **Edit groups claim** for the
   **ID** token:
   - select **Group ID** — the `sAMAccountName` / `NetBIOSDomain\…` formats only
     work for groups synced from on-prem AD; **cloud-only groups emit nothing**
     in those formats; and
   - **uncheck "Emit groups as role claims"** — that would deliver the values in
     the `roles` claim, but SixtyOps reads the `groups` claim.

   The result is `"groups": ["<guid>", …]` in the ID token.
5. **Allowed Group / Admin Group** — enter the group's **Object ID (GUID)**
   (Groups > All groups > the group > *Object Id*), because that is what the
   Group ID claim contains. (Group *names* only work with Authentik/Keycloak-
   style providers that emit names.)

### Entra troubleshooting

SixtyOps logs the provider's error body and the token's group claim, so most
failures name themselves in `docker compose logs sixtyops-mgmt`:

| Symptom | Cause | Fix |
| --- | --- | --- |
| `AADSTS650053 … scope 'groups' … doesn't exist` | `groups` requested as a scope | Set **Scopes** to `openid email profile` (step 2) |
| `AADSTS700025: Client is public …` | Callback registered under SPA / public client | Register the redirect URI under the **Web** platform (step 3) |
| Login succeeds, then `not in group '<guid>'` with `token groups: none` | Groups claim missing from the ID token, wrong format, or emitted as roles | Group claim = **Group ID**, on the **ID** token, **not** "emit as role claims" (step 4) |
| `not in group '<guid>'` but the log shows *other* GUIDs | Wrong Object ID in Allowed/Admin Group | Copy the group's **Object Id** (step 5) |

### Via Environment Variables

For deployment-time (GitOps) configuration, set these in your compose or env
file:

| Variable | Description |
|----------|-------------|
| `OIDC_ENABLED` | `true`/`false` — turn SSO on without touching the UI |
| `OIDC_PROVIDER_URL` | Full URL to the provider application / v2.0 authority |
| `OIDC_CLIENT_ID` | OAuth2 client ID |
| `OIDC_CLIENT_SECRET` | OAuth2 client secret |
| `OIDC_REDIRECT_URI` | Callback URL (`https://<sixtyops>/auth/oidc/callback`) |
| `OIDC_ALLOWED_GROUP` | Required group (name for Authentik; **GUID** for Entra) |
| `OIDC_ADMIN_GROUP` | Group granting the admin role (name for Authentik; **GUID** for Entra) |
| `OIDC_SCOPES` | Override default scopes (default: `openid email profile groups`) |

**Precedence is per field: an environment variable wins over the stored
database value for that field, and the others fall back to the database.** This
lets you pin, say, the provider URL and scopes from compose while still setting
the client secret from the UI — no container restart to change a DB-backed
field.

Any field set by an environment variable is **read-only in the Settings UI**
(greyed out with a note); to change it, edit the container configuration and
restart. A blank env var (e.g. `OIDC_SCOPES=`) is treated as *unset* and does
not lock the field. Env-locked fields are never written back to the database, so
the two sources can't drift.

## Self-Hosted OIDC Providers (LAN)

By default, SixtyOps rejects OIDC provider URLs that resolve to private or
loopback IP addresses (192.168.x, 10.x, 172.16-31.x, 127.x). This protects
against SSRF in cloud deployments.

If your Authentik instance runs on a private network, set:

```
OIDC_ALLOW_PRIVATE_IPS=true
```

This allows provider URLs resolving to private/loopback/reserved IPs. HTTPS
and DNS resolution are still enforced. This is an env-var-only setting (not
configurable via the UI) to prevent accidental changes.

## Default User Role

New users who authenticate via OIDC are automatically created with the
**viewer** role (least-privilege default). An admin must manually promote
users to `operator` or `admin` via the **Users** tab.

If you configure an **Admin Group** in the OIDC settings, that mapping takes
priority: members of that group become `admin`, and all other OIDC users stay
`viewer` even if `oidc_default_role` is set to something broader.

To change the default role for new OIDC users:

```
PUT /api/settings
{"oidc_default_role": "operator"}
```

Valid values: `viewer`, `operator`, `admin`. Changing this setting does not
affect existing users.

## Logout Behavior

When an OIDC user logs out of SixtyOps, the application will:

1. Delete the local session
2. Redirect to the OIDC provider's `end_session_endpoint` (RP-Initiated Logout)
3. After the provider completes logout, redirect back to the SixtyOps login page

If the provider's logout endpoint is unavailable, the user is redirected to
the SixtyOps login page directly. The local session is always cleared
regardless of the provider logout outcome.
