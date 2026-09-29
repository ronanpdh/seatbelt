# Record Claude Desktop and Cowork through the gateway

Claude Desktop, which hosts Cowork, can send its model traffic to an organisation's gateway instead of to Anthropic. Point it at the seatbelt gateway and every Desktop and Cowork session is recorded like any other client's. Set it up with your MDM, or per machine in the app.

Everything below about Claude Desktop's configuration comes from Anthropic's documentation, cited at the end. Check those pages for the app version your fleet runs.

## What the gateway provides

Claude Desktop requires a gateway to serve `POST /v1/messages` with streaming and tool use; `GET /v1/models` is optional and, when present, lets the app discover the available models [1]. The seatbelt gateway serves both. It forwards `GET /v1/models` to Anthropic when the request carries `anthropic-version` or `x-api-key`, as Anthropic clients' requests do.

The gateway forwards request bodies byte for byte, so the `cache_control` breakpoints Cowork and Code sessions send reach Anthropic unchanged and prompt caching keeps working [1]. `anthropic-beta` and `anthropic-version` headers are forwarded too. To check, `seatbelt reconstruct` a Desktop session: after the first request, `cache_read_input_tokens` in the recorded usage should be above zero on most requests [1].

## Choose how users authenticate

The gateway knows who a request came from by the key it carries. How that key reaches the app decides what the ledgers can attribute:

| Approach | Claude Desktop key | Attribution in the ledger |
|---|---|---|
| One key in a fleet-wide profile | `inferenceGatewayApiKey` [1] | Everyone shares one principal. Use only for a pilot, or issue the key to a team id such as `finance@corp`. |
| A per-user key read by a credential helper | `inferenceCredentialHelper` [1][3] | Each user is their own principal. |
| Sign-in with your identity provider (OIDC) | `inferenceCredentialKind` + `inferenceGatewayOidc` [1][3] | Each user is their own principal, by the provider's user id; no keys to issue or revoke. Recommended where you have Entra ID, Okta or another OIDC provider. From 0.3.0. |

### Sign-in with your identity provider (OIDC)

Claude Desktop signs the user in with your provider in the system browser (authorization code with PKCE, a public client, no secret) and sends the provider's token, unchanged, as `Authorization: Bearer` on every inference request: the ID token by default, or the access token with `bearerTokenType: "access_token"` [1][3]. The app refreshes it before a turn when it has expired, and once more if the gateway answers 401 [3]. The gateway checks each token offline against the provider's published keys: its signature, `iss`, `aud` and `exp` [1][4]. Checking `aud` matters: without it any token your tenant issues, for any app, would be accepted [1].

1. Register Claude Desktop with the provider as a public client.
   - Entra ID: an app registration with the redirect URI `http://127.0.0.1/callback` under "Mobile and desktop applications" (any port is allowed) [1].
   - Okta: a Native app, with the exact loopback port you set as `redirectPort` [1].
   - Auth0: see [Auth0](#auth0) below.
2. Point the gateway at the provider, in `gateway.yaml`:

   ```yaml
   oidc:
     issuer: https://login.microsoftonline.com/<tenant id>/v2.0   # Okta: https://<org>.okta.com
     audience: <the app's client id>        # the ID token's aud
     principal_claim: oid                   # Entra's immutable user id; Okta and most others: sub
     name_claim: email                      # recorded for readers, never used to authorize
     # allow: [<oid>, ...]                  # optional: only these users
   ```

   The keys are found from the issuer's discovery document (`<issuer>/.well-known/openid-configuration`), kept for 5 minutes, and fetched again early when a token names a key the gateway does not hold, which is how a provider's key rotation shows. `jwks_url` overrides discovery. Only asymmetric signature algorithms can be configured (`algorithms`, default `[RS256]`), so an unsigned or HMAC-signed token is never accepted; `leeway` allows up to 60 seconds of clock skew by default.
3. Configure the app (MDM, or the in-app configuration) [1][3]. To use the in-app configuration on one machine, enable **Help → Troubleshooting → Enable Developer Mode** in the menu bar (on Windows, the ☰ menu of the sign-in screen). Then open **Developer → Configure Third-Party Inference…**, fill in **Connection**, and choose **Apply Changes** [5]. It is not in the Settings window.

   | Key | Value |
   |---|---|
   | `inferenceProvider` | `gateway` |
   | `inferenceGatewayBaseUrl` | the gateway URL |
   | `inferenceCredentialKind` | `interactive` |
   | `inferenceGatewayOidc` | one JSON object, e.g. `{"issuer":"https://login.microsoftonline.com/<tenant id>/v2.0","clientId":"<client id>"}`. In a plist or the registry it is a JSON string; dotted keys such as `inferenceGatewayOidc.clientId` are not read [1][3]. |

   From Desktop 2.7032.0 the same settings are also spelled `inferenceCredentialKind: "external-idp"` with `inferenceIdpOidc`; the older spelling keeps working [1][3].

Users are identified by `principal_claim`, not by e-mail: the provider's own guidance is to key on the immutable id (Entra `oid`, Okta `sub`), because `email` and `preferred_username` can change or be absent [1][4]. A signed-in user's sessions are keyed by issuer and user id, so a token refresh continues the same ledger; `run.start` records `principal.auth: oidc`, `principal.issuer` and, if the token has it, `principal.name`. Revocation is the provider's: disable the user there, or take them off `allow` (a config reload applies it to their next request). A token already issued stays valid until it expires.

#### Auth0

Tested on 2026-09-29 with an Auth0 dev tenant, Claude Desktop 2.9939.4 on macOS, and this gateway: sign-in, a reply, and a ledger recording the user as `principal.auth: oidc` with their Auth0 `sub` and email.

1. In the Auth0 dashboard, create an application of type **Native**. Native is a public client, and its token endpoint authentication method is **None**. A Regular Web application expects a client secret, which Claude Desktop does not send [1].
   - **Allowed Callback URLs:** `http://127.0.0.1:53180/callback`. Auth0 allows wildcards only for subdomains [6], so the port is fixed, as for Okta. Any free port works if the same one goes in `redirectPort`.
   - **Grant types:** Authorization Code and Refresh Token.
   - **ID token expiration:** the gateway cannot revoke a token before it expires, so keep it short, for example 3600 seconds, and let the app refresh it.
2. In `gateway.yaml`, the issuer is the tenant domain **with a trailing slash**, exactly as the tenant's discovery document gives it (`https://<tenant>/.well-known/openid-configuration`) [7]. The gateway compares `iss` exactly, so without the slash every token is refused.

   ```yaml
   oidc:
     issuer: https://<tenant>.<region>.auth0.com/
     audience: <the app's client id>
     principal_claim: sub        # e.g. auth0|abc123, or google-oauth2|… for a social login
     name_claim: email
   ```

3. In Claude Desktop, set **Issuer URL** to the same value (trailing slash included; it worked in the test), **Client ID** to the app's client ID, **Redirect port** to `53180`, and leave **Scopes** empty. As one MDM value: `{"issuer":"https://<tenant>.<region>.auth0.com/","clientId":"<client id>","redirectPort":53180}`.

Not yet confirmed with Auth0: that the app refreshes the ID token silently. Auth0's docs also mention enabling offline access on an API for refresh tokens [8], and this setup requests none. If users are asked to sign in again when the ID token expires, that is the place to look.

Keep `offline_access` in the scopes (it is in the default) so the app can refresh the ID token silently; with explicit `scopes` in `id_token` mode it is not added for you [1]. Google Workspace does not return an ID token on refresh, so Anthropic's docs recommend `access_token` mode there [1]; whether Google's access tokens can be checked offline as above is not established, so Google is not a tested provider for this gateway yet.

### Per-user keys with a credential helper

Issue each user a key (`seatbelt gateway keygen --user <email>`) and put it in their `~/.config/seatbelt/config.toml` (mode 0600), the same file `seatbelt run claude` reads:

```toml
gateway = "https://gw.corp.example"
key = "sbk_..."
```

A 0.2.0 `~/.config/seatbelt/gateway.toml` (`url` and `key`) is read only when there is no `config.toml`; the script below reads it in that case too.

Install this script on each Mac with your MDM, for example at `/usr/local/bin/seatbelt-gateway-key`, mode 0755:

```sh
#!/bin/sh
# Prints this user's seatbelt gateway key, for Claude Desktop's inferenceCredentialHelper.
# The file `seatbelt run` reads: config.toml, else 0.2.0's gateway.toml.
config="$HOME/.config/seatbelt/config.toml"
[ -e "$config" ] || config="$HOME/.config/seatbelt/gateway.toml"
exec sed -n 's/^key *= *"\(.*\)"$/\1/p' "$config"
```

Claude Desktop runs the helper, reads the key from its standard output, and caches it for `inferenceCredentialHelperTtlSec` seconds (default 3600). When a helper is set, the static key fields are ignored [3]. On Windows, point `inferenceCredentialHelperWindows` at an equivalent script that prints the key [3]; this repository does not ship one.

## Configure the app

The documented way to build the profile is in the app: **Help → Troubleshooting → Enable Developer Mode**, then **Developer → Configure Third-Party Inference…**. In **Connection**, set **Inference provider** to **Gateway**, fill in the gateway URL and credentials, then **Export** a `.mobileconfig` (macOS) or `.reg` (Windows) file for your MDM [1][2]. The keys that matter for the seatbelt gateway are:

| Key | Value |
|---|---|
| `inferenceProvider` | `gateway` [3] |
| `inferenceGatewayBaseUrl` | the gateway URL, e.g. `https://gw.corp.example` [1] |
| `inferenceCredentialHelper` | `/usr/local/bin/seatbelt-gateway-key` (per-user keys) [3] |
| `inferenceGatewayApiKey` | an issued `sbk_...` key (shared key only) [1] |
| `inferenceGatewayAuthScheme` | leave unset: the default, `bearer`, sends `Authorization: Bearer`; `x-api-key` also works [1] |

Every value is written as a string, even numbers and booleans [3].

### macOS

A profile delivered by MDM lands in `/Library/Managed Preferences/com.anthropic.claudefordesktop.plist` (machine) or `/Library/Managed Preferences/<user>/com.anthropic.claudefordesktop.plist` (per user, which wins where both set a key) [2]. Its keys, for per-user keys:

```xml
<key>inferenceProvider</key>
<string>gateway</string>
<key>inferenceGatewayBaseUrl</key>
<string>https://gw.corp.example</string>
<key>inferenceCredentialHelper</key>
<string>/usr/local/bin/seatbelt-gateway-key</string>
```

### Windows

Values sit directly under `HKLM\SOFTWARE\Policies\Claude` (machine policy, recommended) or `HKCU\SOFTWARE\Policies\Claude`, as `REG_SZ` [2]. The app ignores user policy entirely when any machine policy is present, so deploy the whole configuration to one hive [2]. For a shared key:

```reg
Windows Registry Editor Version 5.00

[HKEY_LOCAL_MACHINE\SOFTWARE\Policies\Claude]
"inferenceProvider"="gateway"
"inferenceGatewayBaseUrl"="https://gw.corp.example"
"inferenceGatewayApiKey"="sbk_..."
```

A key in machine policy is readable by anyone who can read that registry key on the device; prefer the credential helper.

## What the ledgers show

Desktop does not name its runs, so each user's Desktop and Cowork traffic goes into sessions that close after the gateway's `session_idle` window of quiet. `run.start` records the client's `client.user_agent` as sent. `seatbelt report` counts sessions per user.

## Sources

1. Anthropic, "Deploy Claude Desktop on 3P with an LLM gateway": https://claude.com/docs/third-party/claude-desktop/gateway
2. Anthropic, "Deploy with MDM": https://claude.com/docs/third-party/claude-desktop/mdm
3. Anthropic, Claude Desktop configuration reference (`inferenceProvider`, `inferenceCredentialHelper`, `inferenceGatewayOidc`, value types): https://claude.com/docs/third-party/claude-desktop/configuration
4. Anthropic, bootstrap server guidance (checking `iss`, `aud`, `exp` against the provider's keys; keying on immutable ids): https://claude.com/docs/third-party/claude-desktop/bootstrap; Microsoft, ID token claims reference: https://learn.microsoft.com/en-us/entra/identity-platform/id-token-claims-reference
5. Anthropic, Claude Desktop in-app configuration (Developer Mode, **Configure Third-Party Inference…**, **Apply Changes**): https://claude.com/docs/third-party/claude-desktop/in-app-configuration
6. Auth0, Application Settings ("Allowed Callback URLs … Wildcards: Use * for subdomains"): https://auth0.com/docs/get-started/applications/application-settings
7. Auth0's sample tenant discovery document, read 2026-09-29 (https://samples.auth0.com/.well-known/openid-configuration): `"issuer": "https://samples.auth0.com/"`; the tenant in the test gave the same form
8. Auth0, Get Refresh Tokens ("you must include the offline_access scope … Be sure to initiate Offline Access in your API"): https://auth0.com/docs/secure/tokens/refresh-tokens/get-refresh-tokens
