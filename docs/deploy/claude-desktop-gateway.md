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
| A per-user key read by a credential helper | `inferenceCredentialHelper` [1][3] | Each user is their own principal. Recommended. |

The gateway does not accept OIDC tokens (`inferenceGatewayOidc`) yet; that is planned for 0.3.0.

### Per-user keys with a credential helper

Issue each user a key (`seatbelt gateway keygen --user <email>`) and put it in their `~/.config/seatbelt/gateway.toml` (mode 0600), the same file `seatbelt run claude` reads:

```toml
url = "https://gw.corp.example"
key = "sbk_..."
```

Install this script on each Mac with your MDM, for example at `/usr/local/bin/seatbelt-gateway-key`, mode 0755:

```sh
#!/bin/sh
# Prints this user's seatbelt gateway key, for Claude Desktop's inferenceCredentialHelper.
exec sed -n 's/^key *= *"\(.*\)"$/\1/p' "$HOME/.config/seatbelt/gateway.toml"
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
3. Anthropic, Claude Desktop configuration reference (`inferenceProvider`, `inferenceCredentialHelper`, value types): https://claude.com/docs/third-party/claude-desktop/configuration
