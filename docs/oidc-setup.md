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
> check its documentation for how to include group membership in the ID token.
> Providers that don't recognize the `groups` scope will silently ignore it.

### Via Environment Variables

For deployment-time configuration, set these in your compose or env file:

| Variable | Description |
|----------|-------------|
| `OIDC_PROVIDER_URL` | Full URL to the Authentik application |
| `OIDC_CLIENT_ID` | OAuth2 client ID |
| `OIDC_CLIENT_SECRET` | OAuth2 client secret |
| `OIDC_REDIRECT_URI` | Callback URL (`https://<sixtyops>/auth/oidc/callback`) |
| `OIDC_ALLOWED_GROUP` | Required group name for access |
| `OIDC_SCOPES` | Override default scopes (default: `openid email profile groups`) |

Database settings take precedence over environment variables once saved.

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
