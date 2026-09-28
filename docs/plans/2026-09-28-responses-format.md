# OpenAI Responses format and the `codex` preset (0.3.0)

**Goal:** record OpenAI Responses API traffic (`POST /v1/responses`, streamed or not) at the gateway, so current Codex and the OpenAI Agents SDK are recorded like Claude Code, and launch Codex with `seatbelt run codex`.

**Architecture:** one more format module, `seatbelt.gateway.formats.openai_responses`, beside `anthropic` and `openai_chat`, with the same `Format` protocol (`begin`, `finish`, `model`, `tool_result_calls`) and an `assemble_sse`. The app routes `/v1/responses` to the `openai` upstream. The launcher gains presets that pass command-line arguments as well as environment.

Every wire fact below comes from the sources in the source map at the end, read on 2026-09-28. Where the sources leave something open, the design does not depend on it, and it is listed under "Open".

## Wire format, as recorded

Request: `model`, `input` (a string or a list of items), `instructions`, `prompt` (a stored prompt and its variables), `conversation`, `tools`, `tool_choice`, `max_output_tokens`, `max_tool_calls`, `reasoning`, `text`, `previous_response_id`, `store`, `include`, `parallel_tool_calls`, `truncation`, `temperature`, `top_p`, `background`, `stream` [S1][S2]. The ledger keeps those fields except `model` (recorded on its own) and `stream`, as the other formats keep theirs.

Tool calls come back as output items and their results go back as input items, matched by `call_id` [S1][S2]:

| Call (output item) | Name recorded | Arguments recorded | Result (input item) |
|---|---|---|---|
| `function_call` | `name` | `arguments`, a JSON string, parsed | `function_call_output` |
| `custom_tool_call` | `name` | `{"input": input}` | `custom_tool_call_output` |
| `local_shell_call` | `local_shell` | `action` | `function_call_output` (Codex) [S4], or `local_shell_call_output` keyed by `call_id` (the Agents SDK) [S5] or `id` (the spec's type) [S1] |
| `shell_call` | `shell` | `action` | `shell_call_output` |
| `apply_patch_call` | `apply_patch` | `operation` | `apply_patch_call_output` |
| `computer_call` | `computer` | `action` and/or `actions`, whichever it has | `computer_call_output` |
| `tool_search_call` with `execution: "client"` | `tool_search` | `arguments` | `tool_search_output` (its result is `tools`) [S1][S4] |

A `function_call` or `custom_tool_call` may carry a `namespace` [S1]; Codex puts each MCP server's tools under `mcp__<server>` and names them `mcp__<server>__<tool>` in its hooks (`McpHandler::hook_tool_name`) [S4]. The gateway names MCP tools the same way, so one cannot pass for a built-in of the same bare name. Any other namespace keeps the bare name, as Codex's hooks do for its own namespaced tools (`spawn_agent` in `multi_agent_v1`) [S4], and as a deferred top-level tool needs: the API gives it a namespace equal to its name (the Agents SDK's `is_reserved_synthetic_tool_namespace`) [S5].

Hosted tools the provider runs itself (`web_search_call`, `file_search_call`, `code_interpreter_call`, `image_generation_call`, `mcp_call`, and a `tool_search_call` it executes) have no client result [S1]; they stay in the recorded response and are not tool calls.

Response: `id`, `object: "response"`, `status` (`completed`, `failed`, `in_progress`, `cancelled`, `queued`, `incomplete`), `error`, `incomplete_details`, `model`, `output`, `usage` [S1]. `usage` is `input_tokens`, `output_tokens`, `total_tokens`, `input_tokens_details.cached_tokens` and `.cache_write_tokens`, `output_tokens_details.reasoning_tokens` [S1]; every integer is recorded, the details flattened (`input_tokens.cached_tokens`), as the Chat Completions format records its details. `output_text` is an SDK convenience, not on the wire [S1].

A response whose `status` is `failed` is recorded with its `error` as the call's error. Tool calls are recorded only from a completed item (`status: completed`) or, where an item has no status, a completed response: a cut-off item's arguments may be truncated, the rule the other formats follow.

## Streaming

Every event is JSON with a required `type` and `sequence_number`; SDKs and Codex dispatch on `type` [S3]. `response.created`, `response.in_progress`, `response.completed`, `response.incomplete` and `response.failed` each carry a whole `response` [S3]. The Responses docs never mention a `data: [DONE]` terminator [S3]; the existing SSE parser skips it either way.

`assemble_sse` rebuilds the final response:

1. A terminal event (`response.completed`, `response.incomplete`, `response.failed`) gives the response, recorded as sent. If its `output` is missing or empty, fill it from the `response.output_item.done` items in `output_index` order, as openai-python and the Agents SDK do [S3].
2. With no terminal event, rebuild it: the last `response.created` / `response.in_progress` snapshot, the finished items by `output_index` marked completed (a `custom_tool_call` has no status field [S1], but one that arrived by `output_item.done` is whole), and the unfinished items as their `output_item.added` form marked in progress, with what arrived of their text, arguments or input (`response.output_text.delta`, `response.function_call_arguments.delta`, `response.custom_tool_call_input.delta`) [S3][S4]. `status` stays as the snapshot had it, so `finish` records the call as not completed. Codex treats a stream that closes before `response.completed` as an error too [S4].
3. An `error` event is recorded as the error. The spec's shape is flat (`code`, `message`); Codex's fixtures nest it under `error` [S3]; both are read.

## Gateway changes

- `/v1/responses` → `openai` upstream, `Authorization: Bearer` [S5].
- A session keeps one format instance per format, not per upstream: Chat Completions and Responses both go to `openai`.
- The stream assembler comes from the route's format, not from the provider.
- Policy: `max_output_tokens` also reads the Responses field of that name; `models` and `tools_denied` work through `model` and `tool_result_calls` unchanged.
- A client that chains `previous_response_id` sends a tool's output without the call it answers [S2][S5], possibly after the session that saw the call closed on idle. Denied calls are therefore remembered per employee across sessions (the newest 10,000), until a restart.
- `background: true` is refused with 400 before the upstream: the client fetches a background response later with `GET /v1/responses/{id}` [S2], which the gateway would not record.

## `seatbelt run codex`

Codex speaks only the Responses API; `wire_api = "chat"` was removed [S4]. The built-in `openai` provider cannot be given extra headers and `[model_providers.openai]` is reserved, so the preset defines its own provider on the command line with `-c`, which Codex parses as TOML [S4]:

```
codex -c model_provider="seatbelt" \
      -c 'model_providers.seatbelt={name="seatbelt", base_url="<gateway>/v1", env_key="SEATBELT_GATEWAY_KEY", wire_api="responses", http_headers={"X-Seatbelt-Run"="<run>"}}'
```

Codex posts to `{base_url}/responses` with `Authorization: Bearer` from `env_key` [S4]. A custom provider does not use WebSockets unless `supports_websockets` is set [S4], so every turn is an HTTP POST the gateway sees. Codex sends `store: false`, `stream: true`, and the whole history each turn [S4].

## Not recorded (documented, not built)

- The Agents SDK's tracing goes straight to `https://api.openai.com/v1/traces/ingest`, not through `OPENAI_BASE_URL`, authenticated with `OPENAI_API_KEY`, which behind the gateway is the employee's gateway key [S5]. Disable it (`OPENAI_AGENTS_DISABLE_TRACING=1`) or give it its own key (`set_tracing_export_api_key`).
- Opt-in Agents SDK paths the gateway does not serve: the Conversations API, `/responses/compact`, and the WebSocket transport [S5].
- Codex pointed at the gateway through `openai_base_url` (the built-in provider) tries a WebSocket first [S4]; use the preset's custom provider.

## Open

- Whether the live API writes `event:` lines and ends a stream with `data: [DONE]` [S3]. The parser needs neither.
- Whether `response.incomplete` / `response.failed` events carry partial `output` [S3]. Step 1 falls back to `output_item.done` items either way.
- Whether the live API accepts `local_shell_call_output` by `call_id` (what the Agents SDK sends [S5]) or `id` (the SDK type [S1]). Both are matched.

## Source map

| Ref | Source | Used for |
|---|---|---|
| S1 | openai/openai-python @68b173a2, `src/openai/types/responses/*.py` (generated from the OpenAPI spec); openai/openai-openapi @116c78f2 `openapi.json` (`CreateResponse`, `Response`, `ErrorResponse`) | request and response fields, item types, `call_id` pairing, usage, `output_text` SDK-only |
| S2 | developers.openai.com API reference and function-calling guide (`/api/docs/...`, fetched as Markdown) | request fields, function tool shape, `function_call_output` |
| S3 | openai-python `api_reference/openapi.transformed.yml` (`ResponseStreamEvent`), `src/openai/lib/streaming/responses/`; openai-agents-python `src/agents/models/openai_responses.py`; developers.openai.com streaming guide | stream event types, terminal events, reconstruction, `[DONE]` absent, error event shapes |
| S4 | openai/codex @4fd5745e, `codex-rs` (`model_provider_info.rs`, `codex-api/src/sse/responses.rs`, `ResponsesApiRequest`, tool specs), discussion #7782 | provider config keys, `-c` parsing, reserved `openai` id, Responses-only `wire_api`, request fields, stream handling, WebSocket default |
| S5 | openai/openai-agents-python @e80737c6 (`OpenAIResponsesModel`, `OpenAIProvider`, `BackendSpanExporter` and its `api_key`, `run_internal/tool_actions.py` local shell output, `oai_conversation.py` `prepare_input`, `tracing/__init__.py`); openai-python `_client.py` | endpoint, base URL and auth, tracing bypass and key, opt-in endpoints, `local_shell_call_output`, chained requests |

Claims were collected by four research agents from these sources and re-checked one by one against them by a fifth: 148 of 150 confirmed; the two it corrected (which Codex stream events carry items, and where `[DONE]` is documented) are stated here in their corrected form. A review of the implementation against the same sources then found the `local_shell_call_output`, namespace, client-run `tool_search_call`, `cache_write_tokens`, chained-output and background-mode cases, added above.
