# Deploy the recording gateway

The gateway is an HTTP service your organisation runs. Employees' clients send model traffic to it instead of to the provider. It checks the employee's issued key, swaps in the real provider key, forwards the request, relays the response, and records the exchange into one signed ledger per employee session. The ledgers, their attestations and the signing key stay on the gateway host.

It serves the Anthropic Messages API (`POST /v1/messages`, streamed or not), OpenAI Chat Completions (`POST /v1/chat/completions`) and the OpenAI Responses API (`POST /v1/responses`). It also answers the probes Claude Code and Claude Desktop make: `HEAD /api/hello` locally, and `POST /v1/messages/count_tokens` and `GET /v1/models` by forwarding them without recording (to Anthropic when the request is an Anthropic client's: it sends `anthropic-version` or `x-api-key`; otherwise to OpenAI). Design: [ADR 0006](../adr/0006-recording-gateway.md).

## Stand one up

The container image runs as uid 1000 and writes ledgers under `/var/lib/seatbelt`.

1. Make a directory for the config and the gateway's signing key. Keep `seatbelt.key` on the gateway host only; give `seatbelt.pub` to whoever verifies.

   ```sh
   seatbelt keygen gateway/keys
   ```

2. Write `gateway/gateway.yaml`. Relative paths resolve against the file, so `keys/seatbelt.key` works on the host and in the container alike:

   ```yaml
   listen: 0.0.0.0:8080
   signing_key: keys/seatbelt.key
   ledgers: /var/lib/seatbelt/runs
   session_idle: 900 # seconds of quiet before a session closes and is signed
   upstreams:
     anthropic: {url: https://api.anthropic.com, key_env: ANTHROPIC_API_KEY}
     openai: {url: https://api.openai.com, key_env: OPENAI_API_KEY}
     gemini: {url: https://generativelanguage.googleapis.com, key_env: GEMINI_API_KEY}
   policy: # optional; omit a key to leave it unrestricted
     models: [claude-sonnet-5, claude-opus-5]
     tools_denied: [run_shell]
     max_output_tokens: 16000
   ```

   `key_env` names the environment variable holding the provider key; the key itself never goes in the file. Unknown keys are rejected at startup. `models` is an exact match, so list every model id your clients send, including the smaller model Claude Code uses for background tasks, or those requests are refused.

3. Issue a key to each employee. The key is printed once; the config keeps only its SHA-256. `keygen` rewrites the file, so comments in it are lost. To revoke, delete the principal's entry. A running gateway picks up either change within 30 seconds ([Changing the config while it runs](#changing-the-config-while-it-runs)).

   ```sh
   seatbelt gateway keygen --user alice@corp --config gateway/gateway.yaml
   ```

4. Pull the released image, or build it, and run it with the config directory mounted read-only and the provider keys passed as environment. Each release from 0.3.0 publishes `ghcr.io/ronanpdh/seatbelt-gateway:<version>` (and `:latest`) with signed build provenance; check it before you run it:

   ```sh
   docker pull ghcr.io/ronanpdh/seatbelt-gateway:0.3.0
   gh attestation verify oci://ghcr.io/ronanpdh/seatbelt-gateway:0.3.0 --repo ronanpdh/seatbelt
   docker tag ghcr.io/ronanpdh/seatbelt-gateway:0.3.0 seatbelt-gateway
   # or build it from a checkout: docker build -f docker/Dockerfile.gateway -t seatbelt-gateway .
   docker volume create seatbelt-runs
   docker run -d --name seatbelt-gateway -p 8080:8080 --stop-timeout 70 \
     -v "$PWD/gateway:/etc/seatbelt:ro" \
     -v seatbelt-runs:/var/lib/seatbelt \
     -e ANTHROPIC_API_KEY -e OPENAI_API_KEY -e GEMINI_API_KEY \
     seatbelt-gateway
   ```

   Mount the directory, not the file: `keygen` replaces `gateway.yaml` rather than writing into it, and a container that mounted the file alone keeps reading the old one. `seatbelt keygen` writes both key files mode 0600, so each must be readable by uid 1000 inside the container: `chown 1000` the signing key on the host, or mount a copy owned by 1000; the public key can be `chmod 644`. `--stop-timeout 70` gives the shutdown drain (below) time to finish before Docker kills the process; Docker's default is 10 seconds. Put TLS in front of the gateway (a load balancer or reverse proxy); it serves plain HTTP.

5. Point a client at it with the employee's key. For Claude Code, the employee writes `~/.config/seatbelt/gateway.toml` (mode 0600):

   ```toml
   url = "https://gw.corp.example"
   key = "sbk_..."
   ```

   and runs `seatbelt run claude`, `seatbelt run codex` for Codex, or `seatbelt run gemini` for Gemini CLI. Any Anthropic SDK client works with `ANTHROPIC_BASE_URL=https://gw.corp.example` and the key as its API key; an OpenAI SDK client (Chat Completions or Responses, the OpenAI Agents SDK included) with `OPENAI_BASE_URL=https://gw.corp.example/v1` and the key as `OPENAI_API_KEY`; a Google Gen AI SDK client (Gemini API, not Vertex AI) with `GOOGLE_GEMINI_BASE_URL=https://gw.corp.example` and the key as its API key. Claude Desktop and Cowork: [claude-desktop-gateway.md](claude-desktop-gateway.md).

   `seatbelt run codex` gives Codex its own model provider on the command line (`-c model_provider="seatbelt"` and a `model_providers.seatbelt` table): the gateway URL plus `/v1`, the Responses API over HTTP, the key from the environment, and the run name as a header. Pointing Codex's built-in provider at the gateway with `openai_base_url` instead would have it try a WebSocket first, which the gateway does not serve. The OpenAI Agents SDK traces by default, straight to `api.openai.com` and not through `OPENAI_BASE_URL`, and authenticates with `OPENAI_API_KEY`: here the employee's gateway key, sent to OpenAI along with the traced prompts. Set `OPENAI_AGENTS_DISABLE_TRACING=1`, or give tracing its own key with `set_tracing_export_api_key`. Its Conversations API, `/responses/compact` and WebSocket transport are opt-in and not served, and a request with `background: true` is refused with 400: its output is fetched later with a request the gateway does not record.

   `seatbelt run gemini` sets `GEMINI_API_KEY` to the employee's key, `GOOGLE_GEMINI_BASE_URL` to the gateway and the run name as a header (through `GEMINI_CLI_CUSTOM_HEADERS`, which Gemini CLI reads but does not document), and strips `GOOGLE_API_KEY`, `GOOGLE_GENAI_USE_VERTEXAI` and `GOOGLE_GENAI_USE_GCA`. Gemini CLI uses the base URL only when it signs in with an API key, so its settings (`~/.gemini/settings.json`) need

   ```json
   {"security": {"auth": {"selectedType": "gemini-api-key"}}, "privacy": {"usageStatisticsEnabled": false}}
   ```

   `selectedType` makes it use the gateway: signed in with Google or through Vertex AI it goes to Google directly, unrecorded, and with nothing selected a non-interactive run refuses to start. An org can enforce it with `"enforcedType": "gemini-api-key"` under `security.auth` in Gemini CLI's system settings file. The second stops the usage statistics Gemini CLI sends straight to Google, which are on by default. `seatbelt run gemini` warns when either is missing. Its `countTokens` and embedding calls are forwarded without being recorded, like Claude Code's token counts; any other Gemini API method (batch generation, for one) is refused, so nothing reaches the model unrecorded. Voice input connects to Google directly. Sources: [docs/plans/2026-09-28-gemini-format.md](../plans/2026-09-28-gemini-format.md).

6. Report on what was recorded (with `gateway/keys/seatbelt.pub` readable by uid 1000, see step 4):

   ```sh
   docker run --rm --entrypoint seatbelt \
     -v seatbelt-runs:/var/lib/seatbelt:ro -v "$PWD/gateway/keys/seatbelt.pub:/seatbelt.pub:ro" \
     seatbelt-gateway report /var/lib/seatbelt/runs --pubkey /seatbelt.pub
   ```

### On a PaaS (Coolify and similar)

A host that runs images can run `ghcr.io/ronanpdh/seatbelt-gateway:<version>` (in Coolify, the Docker Image build pack), which pins a verified release rather than whatever the branch holds. A host that builds from git can use `docker/Dockerfile.gateway` as is: it builds seatbelt from the repository, no `uv build` first. Mount `gateway.yaml` at `/etc/seatbelt/gateway.yaml` (a directory mount at `/etc/seatbelt` where the host offers one, for the reason in step 4), give `/var/lib/seatbelt` a persistent volume, expose port 8080 and put the host's HTTPS domain in front.

Where you cannot control a mounted file's owner or mode, leave `signing_key` out of the config and pass the key in the environment instead, as `SEATBELT_SIGNING_KEY`: the PEM itself, or its base64 so it survives any env var editor (`base64 < gateway/keys/seatbelt.key | tr -d '\n'`). Setting both is refused. Treat the variable like the key file: anyone who can read the service's environment can sign ledgers.

Because the service is reachable only through the host's proxy, `FORWARDED_ALLOW_IPS=*` is safe there and makes `client.ip` the real client. Issue employee keys with `seatbelt gateway keygen` against a local copy of the config, then paste the new `principals` entry into the mounted file. Within 30 seconds the logs say `config reloaded`. If they say `config not reloaded`, fix what it names (a restart would fail on the same file). If they say neither, the container does not see the edit, as with a single-file mount the host replaces: restart the service.

## Sessions and runs

Requests from the same employee go into one ledger until `session_idle` seconds pass with no request; then the gateway writes `run.end`, signs the ledger, and the next request starts a new one. A client can name a run with the header `X-Seatbelt-Run: <name>` and close it with `X-Seatbelt-Run-End: true` or `POST /seatbelt/runs/<name>/end`. `seatbelt run` names each run and ends it with the POST when the CLI exits. `run.start` records `principal.id`, `principal.key_id`, `client.ip` and `client.user_agent`.

Behind a reverse proxy, `client.ip` is the proxy's address unless uvicorn trusts the proxy's `X-Forwarded-For`. uvicorn trusts forwarded headers only from `127.0.0.1` and `::1` by default; set `FORWARDED_ALLOW_IPS` in the container's environment to the address the proxy's connections arrive from. With `-p 8080:8080` that is usually the Docker bridge gateway (for example `172.17.0.1`), not the proxy's own address.

On shutdown (SIGTERM or Ctrl-C) the gateway stops accepting requests and waits up to 30 seconds for in-flight ones, then up to 30 more for any session still busy, and closes and signs every session it can. A session still busy after that is left open and closed on the next start. A second stop signal during that close is ignored, so a ledger is never left closed but unsigned; `docker stop` escalates to SIGKILL after its timeout (`--stop-timeout`, step 4). If it is killed instead, the next start closes each ledger left open with `run.ok: false` and `run.error: "gateway restarted"` and signs it. A ledger killed between its `run.end` and its signature is never signed at the next start, because it looks the same as one rewritten and its signature deleted: the start logs it as closed but not signed, for a person to check and sign with `seatbelt attest`. A ledger it cannot read, or whose chain does not verify, is left untouched and logged, never signed.

## Changing the config while it runs

The gateway checks its config file every 30 seconds and reloads it when the content has changed. `SIGHUP` reloads it at once: `docker kill -s HUP seatbelt-gateway`, or `kill -HUP <pid>` outside a container.

- `principals`, `policy` and `upstreams` apply from the next request; a request already in flight finishes under the config it started with. A new `session_idle` applies to every open session from the next idle sweep.
- When a principal's entry is deleted, or its key reissued, its key is refused from the reload on, and its open sessions are ended and signed (a session with a request in flight ends when that request finishes). Sessions are per issued key, so a reissued key starts a new ledger and each ledger's `principal.key_id` names the one key that wrote it.
- A change made while the gateway is starting is picked up at the first check.
- `listen`, `ledgers` and `signing_key` need a restart. A reload that changes them logs a warning, applies everything else, and keeps the running values.
- A file that fails to load (bad YAML, an unknown key, a missing file mid-edit) is logged once, and the running config stays until the file loads again.

Each reload is logged as `config reloaded:` with the number of principals, the ids given a new key and the ids whose key was withdrawn.

## Policy

- `models`: requests for any other model are refused with 403 before they leave the gateway. The match is exact, so list the model ids your clients send (for Gemini, the model in the URL, e.g. `gemini-2.5-pro`).
- `max_output_tokens`: a request whose `max_tokens`, `max_completion_tokens`, (Responses API) `max_output_tokens` or (Gemini) `generationConfig.maxOutputTokens` exceeds it is refused with 403. A request that sets none of them passes.
- `tools_denied`: when the model asks for one of these tools, the call is recorded as denied and the response is still relayed, because the gateway cannot stop a client running a tool on its own machine. Any later request that carries that tool's result is refused with 403 while the tool stays denied, so the result does not reach the model. The gateway remembers each employee's denied calls across sessions, for Responses clients that send a tool's output without the call (`previous_response_id`), until it restarts; after a restart such an output is refused only if the request names the tool. A Codex MCP tool is named `mcp__<server>__<tool>`, as in Claude Code. A Gemini tool result is refused by the tool name it carries. A config reload that removes the tool lets such results through, in open sessions too, and records that it did.

Every verdict is a `policy.check` event in the ledger. `seatbelt report` counts each denying check per employee (a request two rules deny counts twice; a denied tool call counts too) and, for refused requests, against the model asked for.

## Where ledgers live, and backups

Each session is `<ledgers>/<run id>.jsonl` with its signature beside it as `<run id>.attest.json`. Back up by copying the directory; keep each sidecar with its ledger. A closed ledger never changes again, so incremental copies are safe. `seatbelt verify`, `reconstruct`, `pack` and `verify-pack` work on these files unchanged, with nothing but the public key.

## Shipping ledgers to object storage

With a `sink` in the config, the gateway uploads each ledger and its signature to S3-compatible object storage as soon as the session is closed and signed, so the gateway host no longer holds the only copy. Uploads run in the background: a slow or unreachable store never delays a request. A failed upload is retried with backoff (5 s, 30 s, 2 min, then every 5 min); what has not shipped by shutdown, or while the store was down, is shipped at the next start. `<ledgers>/.shipped/<run id>` records each shipped run, with the object keys and the SHA-256 of what was sent. Open ledgers are never shipped, only closed ones.

```yaml
sink:
  url: https://fsn1.your-objectstorage.com   # the location's endpoint
  bucket: seatbelt-ledgers
  region: fsn1                                # for Hetzner, the location code
  prefix: runs/                               # optional, prepended to each object key
  # access_key_env / secret_key_env name the variables holding the keys;
  # defaults SEATBELT_SINK_ACCESS_KEY and SEATBELT_SINK_SECRET_KEY
```

The keys come from the environment, never the file; the gateway refuses to start with a `sink` and no keys. The sink is read at start only: a change to it waits for a restart.

On Hetzner Object Storage ([docs](https://docs.hetzner.com/storage/object-storage/)):

1. Create the bucket in the location you want (`fsn1`, `nbg1` or `hel1`) **with Object Lock enabled**, and give it a default retention (COMPLIANCE mode, in days or years, for as long as you must keep the evidence). Object Lock can only be enabled when the bucket is created, and a bucket with Object Lock always keeps every version: a shipped ledger's version cannot be deleted before its retention ends, by the gateway's key or anyone else's, and an upload under the same key adds a version beside it rather than replacing it. COMPLIANCE mode cannot be ended early.
2. Generate S3 credentials (Console → your project → Security → S3 Credentials) and set them as `SEATBELT_SINK_ACCESS_KEY` and `SEATBELT_SINK_SECRET_KEY` in the gateway's environment. A key can read and write every bucket of its project unless a bucket policy narrows it; the gateway only ever uploads objects to this one bucket, so narrow its key to that.
3. Point `url` at the location's endpoint (`https://<location>.your-objectstorage.com`) and `region` at the location code.

Requests are signed with AWS Signature Version 4 over the whole payload and address the bucket in the host name (`https://<bucket>.<location>.your-objectstorage.com/<key>`), as Hetzner documents for plain HTTP clients; no checksum headers are sent. Any S3-compatible service that accepts that works the same way. To check what arrived, download the objects and run `seatbelt verify <run id>.jsonl --pubkey seatbelt.pub` on them: the signature travels with each ledger.
