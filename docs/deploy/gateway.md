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

4. Build the image and run it, with the config directory mounted read-only and the provider keys passed as environment:

   ```sh
   docker build -f docker/Dockerfile.gateway -t seatbelt-gateway .   # builds the wheel itself
   docker volume create seatbelt-runs
   docker run -d --name seatbelt-gateway -p 8080:8080 --stop-timeout 70 \
     -v "$PWD/gateway:/etc/seatbelt:ro" \
     -v seatbelt-runs:/var/lib/seatbelt \
     -e ANTHROPIC_API_KEY -e OPENAI_API_KEY \
     seatbelt-gateway
   ```

   Mount the directory, not the file: `keygen` replaces `gateway.yaml` rather than writing into it, and a container that mounted the file alone keeps reading the old one. `seatbelt keygen` writes both key files mode 0600, so each must be readable by uid 1000 inside the container: `chown 1000` the signing key on the host, or mount a copy owned by 1000; the public key can be `chmod 644`. `--stop-timeout 70` gives the shutdown drain (below) time to finish before Docker kills the process; Docker's default is 10 seconds. Put TLS in front of the gateway (a load balancer or reverse proxy); it serves plain HTTP.

5. Point a client at it with the employee's key. For Claude Code, the employee writes `~/.config/seatbelt/gateway.toml` (mode 0600):

   ```toml
   url = "https://gw.corp.example"
   key = "sbk_..."
   ```

   and runs `seatbelt run claude`, or `seatbelt run codex` for Codex. Any Anthropic SDK client works with `ANTHROPIC_BASE_URL=https://gw.corp.example` and the key as its API key; an OpenAI SDK client (Chat Completions or Responses, the OpenAI Agents SDK included) with `OPENAI_BASE_URL=https://gw.corp.example/v1` and the key as `OPENAI_API_KEY`. Claude Desktop and Cowork: [claude-desktop-gateway.md](claude-desktop-gateway.md).

   `seatbelt run codex` gives Codex its own model provider on the command line (`-c model_provider="seatbelt"` and a `model_providers.seatbelt` table): the gateway URL plus `/v1`, the Responses API over HTTP, the key from the environment, and the run name as a header. Pointing Codex's built-in provider at the gateway with `openai_base_url` instead would have it try a WebSocket first, which the gateway does not serve. The OpenAI Agents SDK sends its traces straight to `api.openai.com`, not through `OPENAI_BASE_URL`; set `OPENAI_AGENTS_DISABLE_TRACING=1` where prompts must not leave by that route. Its Conversations API, `/responses/compact` and WebSocket transport are opt-in and not served.

6. Report on what was recorded (with `gateway/keys/seatbelt.pub` readable by uid 1000, see step 4):

   ```sh
   docker run --rm --entrypoint seatbelt \
     -v seatbelt-runs:/var/lib/seatbelt:ro -v "$PWD/gateway/keys/seatbelt.pub:/seatbelt.pub:ro" \
     seatbelt-gateway report /var/lib/seatbelt/runs --pubkey /seatbelt.pub
   ```

### On a PaaS (Coolify and similar)

A host that builds from git can use `docker/Dockerfile.gateway` as is: it builds seatbelt from the repository, no `uv build` first. Mount `gateway.yaml` at `/etc/seatbelt/gateway.yaml` (a directory mount at `/etc/seatbelt` where the host offers one, for the reason in step 4), give `/var/lib/seatbelt` a persistent volume, expose port 8080 and put the host's HTTPS domain in front.

Where you cannot control a mounted file's owner or mode, leave `signing_key` out of the config and pass the key in the environment instead, as `SEATBELT_SIGNING_KEY`: the PEM itself, or its base64 so it survives any env var editor (`base64 < gateway/keys/seatbelt.key | tr -d '\n'`). Setting both is refused. Treat the variable like the key file: anyone who can read the service's environment can sign ledgers.

Because the service is reachable only through the host's proxy, `FORWARDED_ALLOW_IPS=*` is safe there and makes `client.ip` the real client. Issue employee keys with `seatbelt gateway keygen` against a local copy of the config, then paste the new `principals` entry into the mounted file. Within 30 seconds the logs say `config reloaded`. If they say `config not reloaded`, fix what it names (a restart would fail on the same file). If they say neither, the container does not see the edit, as with a single-file mount the host replaces: restart the service.

## Sessions and runs

Requests from the same employee go into one ledger until `session_idle` seconds pass with no request; then the gateway writes `run.end`, signs the ledger, and the next request starts a new one. A client can name a run with the header `X-Seatbelt-Run: <name>` and close it with `X-Seatbelt-Run-End: true` or `POST /seatbelt/runs/<name>/end`. `seatbelt run` names each run and ends it with the POST when the CLI exits. `run.start` records `principal.id`, `principal.key_id`, `client.ip` and `client.user_agent`.

Behind a reverse proxy, `client.ip` is the proxy's address unless uvicorn trusts the proxy's `X-Forwarded-For`. uvicorn trusts forwarded headers only from `127.0.0.1` and `::1` by default; set `FORWARDED_ALLOW_IPS` in the container's environment to the address the proxy's connections arrive from. With `-p 8080:8080` that is usually the Docker bridge gateway (for example `172.17.0.1`), not the proxy's own address.

On shutdown (SIGTERM or Ctrl-C) the gateway stops accepting requests and waits up to 30 seconds for in-flight ones, then up to 30 more for any session still busy, and closes and signs every session it can. A session still busy after that is left open and closed on the next start. If it is killed instead, the next start closes each ledger left open with `run.ok: false` and `run.error: "gateway restarted"` and signs it, and signs any ledger it had closed but not yet signed. A ledger it cannot read, or whose chain does not verify, is left untouched and logged, never signed.

## Changing the config while it runs

The gateway checks its config file every 30 seconds and reloads it when the content has changed. `SIGHUP` reloads it at once: `docker kill -s HUP seatbelt-gateway`, or `kill -HUP <pid>` outside a container.

- `principals`, `policy` and `upstreams` apply from the next request; a request already in flight finishes under the config it started with. A new `session_idle` applies to every open session from the next idle sweep.
- When a principal's entry is deleted, or its key reissued, its key is refused from the reload on, and its open sessions are ended and signed (a session with a request in flight ends when that request finishes). Sessions are per issued key, so a reissued key starts a new ledger and each ledger's `principal.key_id` names the one key that wrote it.
- A change made while the gateway is starting is picked up at the first check.
- `listen`, `ledgers` and `signing_key` need a restart. A reload that changes them logs a warning, applies everything else, and keeps the running values.
- A file that fails to load (bad YAML, an unknown key, a missing file mid-edit) is logged once, and the running config stays until the file loads again.

Each reload is logged as `config reloaded:` with the number of principals, the ids given a new key and the ids whose key was withdrawn.

## Policy

- `models`: requests for any other model are refused with 403 before they leave the gateway. The match is exact, so list the model ids your clients send.
- `max_output_tokens`: a request whose `max_tokens` or `max_completion_tokens` exceeds it is refused with 403. A request that sets neither passes.
- `tools_denied`: when the model asks for one of these tools, the call is recorded as denied and the response is still relayed, because the gateway cannot stop a client running a tool on its own machine. Any later request that carries that tool's result is refused with 403, so the result never reaches the model.

Every verdict is a `policy.check` event in the ledger. `seatbelt report` counts each denying check per employee (a request two rules deny counts twice; a denied tool call counts too) and, for refused requests, against the model asked for.

## Where ledgers live, and backups

Each session is `<ledgers>/<run id>.jsonl` with its signature beside it as `<run id>.attest.json`. Back up by copying the directory; keep each sidecar with its ledger. A closed ledger never changes again, so incremental copies are safe. `seatbelt verify`, `reconstruct`, `pack` and `verify-pack` work on these files unchanged, with nothing but the public key.
