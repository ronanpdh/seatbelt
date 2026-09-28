# Deploy the recording gateway

The gateway is an HTTP service your organisation runs. Employees' clients send model traffic to it instead of to the provider. It checks the employee's issued key, swaps in the real provider key, forwards the request, relays the response, and records the exchange into one signed ledger per employee session. The ledgers, their attestations and the signing key stay on the gateway host.

It serves the Anthropic Messages API (`POST /v1/messages`, streamed or not) and OpenAI Chat Completions (`POST /v1/chat/completions`). It also answers the probes Claude Code and Claude Desktop make: `HEAD /api/hello` locally, and `POST /v1/messages/count_tokens` and `GET /v1/models` by forwarding them without recording. Design: [ADR 0006](../adr/0006-recording-gateway.md).

## Stand one up

The container image runs as uid 1000 and writes ledgers under `/var/lib/seatbelt`.

1. Make the gateway's signing key. Keep `seatbelt.key` on the gateway host only; give `seatbelt.pub` to whoever verifies.

   ```sh
   seatbelt keygen keys
   ```

2. Write `gateway.yaml`. Relative paths resolve against the file, so in the container use absolute ones:

   ```yaml
   listen: 0.0.0.0:8080
   signing_key: /etc/seatbelt/seatbelt.key
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

   `key_env` names the environment variable holding the provider key; the key itself never goes in the file. Unknown keys are rejected at startup.

3. Issue a key to each employee. The key is printed once; the config keeps only its SHA-256. To revoke, delete the principal's entry and restart the gateway.

   ```sh
   seatbelt gateway keygen --user alice@corp --config gateway.yaml
   ```

4. Build the image and run it, with the config and key mounted read-only and the provider keys passed as environment:

   ```sh
   rm -rf dist && uv build && docker build -f docker/Dockerfile.gateway -t seatbelt-gateway .
   docker volume create seatbelt-runs
   docker run -d --name seatbelt-gateway -p 8080:8080 \
     -v "$PWD/gateway.yaml:/etc/seatbelt/gateway.yaml:ro" \
     -v "$PWD/keys/seatbelt.key:/etc/seatbelt/seatbelt.key:ro" \
     -v seatbelt-runs:/var/lib/seatbelt \
     -e ANTHROPIC_API_KEY -e OPENAI_API_KEY \
     seatbelt-gateway
   ```

   The signing key file is mode 0600; it must be readable by uid 1000 inside the container (`chown 1000` it on the host, or mount a copy owned by 1000). Put TLS in front of the gateway (a load balancer or reverse proxy); it serves plain HTTP.

5. Point a client at it with the employee's key. For Claude Code, the employee writes `~/.config/seatbelt/gateway.toml` (mode 0600):

   ```toml
   url = "https://gw.corp.example"
   key = "sbk_..."
   ```

   and runs `seatbelt run claude`. Any Anthropic SDK client works with `ANTHROPIC_BASE_URL=https://gw.corp.example` and the key as its API key; an OpenAI Chat Completions client with `OPENAI_BASE_URL=https://gw.corp.example/v1`. Claude Desktop and Cowork: [claude-desktop-gateway.md](claude-desktop-gateway.md).

6. Report on what was recorded:

   ```sh
   docker run --rm --entrypoint seatbelt \
     -v seatbelt-runs:/var/lib/seatbelt:ro -v "$PWD/keys/seatbelt.pub:/seatbelt.pub:ro" \
     seatbelt-gateway report /var/lib/seatbelt/runs --pubkey /seatbelt.pub
   ```

## Sessions and runs

Requests with the same key go into one ledger until `session_idle` seconds pass with no request; then the gateway writes `run.end`, signs the ledger, and the next request starts a new one. A client can name a run with the header `X-Seatbelt-Run: <name>` and close it with `X-Seatbelt-Run-End: true` or `POST /seatbelt/runs/<name>/end`; `seatbelt run` does both. `run.start` records `principal.id`, `principal.key_id`, `client.ip` and `client.user_agent`.

Behind a reverse proxy, `client.ip` is the proxy's address unless uvicorn trusts the proxy's `X-Forwarded-For`. uvicorn trusts forwarded headers only from `127.0.0.1` and `::1` by default; set `FORWARDED_ALLOW_IPS` in the container's environment to your proxy's address to trust it.

On shutdown the gateway waits up to 30 seconds for in-flight requests, then closes and signs every session. If it is killed instead, the next start closes each ledger left open with `run.ok: false` and `run.error: "gateway restarted"` and signs it. A ledger it cannot read, or whose chain does not verify, is left untouched and logged, never signed.

## Policy

- `models`: requests for any other model are refused with 403 before they leave the gateway. The match is exact, so list the model ids your clients send.
- `max_output_tokens`: a request whose `max_tokens` or `max_completion_tokens` exceeds it is refused with 403. A request that sets neither passes.
- `tools_denied`: when the model asks for one of these tools, the call is recorded as denied and the response is still relayed, because the gateway cannot stop a client running a tool on its own machine. Any later request that carries that tool's result is refused with 403, so the result never reaches the model.

Every verdict is a `policy.check` event in the ledger. `seatbelt report` counts the refusals per employee and per model.

## Where ledgers live, and backups

Each session is `<ledgers>/<run id>.jsonl` with its signature beside it as `<run id>.attest.json`. Back up by copying the directory; keep each sidecar with its ledger. A closed ledger never changes again, so incremental copies are safe. `seatbelt verify`, `reconstruct`, `pack` and `verify-pack` work on these files unchanged, with nothing but the public key.
