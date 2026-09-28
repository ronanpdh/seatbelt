# Gemini `generateContent` format and the `gemini` preset (0.3.0)

**Goal:** record Gemini API traffic (`generateContent` and `streamGenerateContent`) at the gateway, so Gemini CLI and Google's SDKs are recorded like Claude Code and Codex, and launch Gemini CLI with `seatbelt run gemini`.

**Architecture:** one more format module, `seatbelt.gateway.formats.gemini`, with the same `Format` protocol and an `assemble_sse`. The app routes `POST /{v1beta,v1,v1alpha}/models/{model}:{method}` by method: the two inference methods are recorded, `countTokens` and the embedding methods are forwarded unrecorded, and any other method is refused. The model is in the URL, not the body, so the gateway copies it into the body it records and checks policy against (the bytes sent upstream are unchanged).

Every wire fact below comes from the sources in the source map at the end, read on 2026-09-28. Where the sources leave something open, the design does not depend on it, and it is listed under "Open".

## Endpoints and auth

- Inference: `POST /v1beta/models/{model}:generateContent` and `:streamGenerateContent` [S1]. Both Google SDKs always stream with `?alt=sse` [S5]. The model is only in the path [S5].
- Also called by Gemini CLI: `:countTokens`, and embeddings as `:batchEmbedContents` [S5][S6]. `GET /v1beta/models` lists models [S1].
- The key goes in the `x-goog-api-key` header [S2][S5]; `?key=` is equivalent [S3]. The gateway accepts an issued seatbelt key either way, strips both, and sends the real key as `x-goog-api-key`. A `key` parameter is never forwarded.
- Errors are `{"error": {"code", "message", "status", "details"}}` [S4]. The gateway's own errors on Gemini paths (and on requests carrying `x-goog-api-key` or `?key=`) take that shape, with the `google.rpc.Code` name as `status`.

## Wire format, as recorded

Request: `contents`, `systemInstruction`, `tools`, `toolConfig`, `generationConfig`, `safetySettings`, `cachedContent`, `labels`, `serviceTier`, `store` [S1]. The ledger keeps those; the model is recorded on its own, from the URL.

Response: `candidates[]` (`content.parts`, `finishReason`, ...), `promptFeedback.blockReason`, `usageMetadata`, `modelVersion`, `responseId` [S1]. `modelVersion` is recorded as the response model. A response with no candidates and a `blockReason` is recorded with the error `prompt blocked: <reason>`.

Usage: `promptTokenCount` (which includes cached tokens) is `input_tokens` and `cachedContentTokenCount` is `input_tokens.cached_tokens`. The documented `totalTokenCount` is prompt + thoughts + candidates [S1], so thinking is counted apart from the answer. OpenAI's `output_tokens` includes its reasoning tokens, so `output_tokens` here is `candidatesTokenCount + thoughtsTokenCount`, and `thoughtsTokenCount` is also recorded as `output_tokens.reasoning_tokens`. `toolUsePromptTokenCount` is recorded as `tool_use_prompt_tokens` and `totalTokenCount` as `total_tokens`.

## Tool calls and their results

A call is a `functionCall` part, `{id?, name, args}`, where `args` is a JSON object; `id` is optional [S1]. A result is a `functionResponse` part, `{id?, name, response}`, where the client sets `id` to match the call's [S1].

Gemini CLI uses the API's id if there is one and makes one up otherwise, writes it back into the call kept in its history, and answers with that id. Its source prefixes the id with `<name>__` [S6]. Released 0.61.0 does not: it sent a made-up `list_directory_<time>_0` on both the call and the result [S8]. It reports a result as `response: {output}` and a failure as `response: {error}` [S6].

The gateway records each call under the API's id, or under a minted `<name>#<n>` when the API gave none. It links a result to its call in this order:

1. by id;
2. by id less the `<name>__` prefix;
3. otherwise (Gemini CLI's made-up ids [S8]), to the oldest open call to the same tool whose arguments match those of the history's call with that id. With no id at all, to the oldest open call to the same tool.

Matching arguments stops a result resent from an earlier session from claiming a call just made. Each result is recorded once, however often the history is resent. A result that has an id is known by the id; one without is known by its name, its content, and how many identical results come before it in the request. `{error}` without `output` is recorded as the result's error.

`tools_denied` refuses a request that carries a result for a denied tool. It goes by the result's `name`, which the wire requires [S1], because Gemini CLI's made-up ids are not the ids the gateway recorded.

A `functionCall` part arrives whole: the Gemini API does not support streaming partial arguments [S5]. Gemini CLI runs the calls in a stream that has no `finishReason` [S6]. So calls are recorded from a stream that broke after them, and that stream's response is recorded with its error.

## Streaming

With `alt=sse`, every event is a `data: ` line holding a whole `GenerateContentResponse` [S1][S5]. Each chunk carries the parts produced since the previous one. `assemble_sse` rebuilds the response the way Gemini CLI reads a stream [S6]:

- adjacent text parts of the same kind (thought or answer) are joined;
- other parts (function calls and the like) are kept in order;
- for every other field, the last value seen wins, including `finishReason` and `usageMetadata`.

On a failed request with `alt=sse`, Google returns HTTP 400 with `content-type: text/event-stream` and a plain JSON error body [S7]. The gateway records any status of 400 or above as an error before it treats the body as a stream.

Without `alt=sse`, `streamGenerateContent` returns a JSON array [S7]. The gateway reads that array whole, records it as one response, and relays it as sent.

## Gemini CLI

Gemini CLI reads `GOOGLE_GEMINI_BASE_URL` only in its API-key mode (`gemini-api-key`) [S6]. With no `security.auth.selectedType` in its settings, a base URL in the environment selects its `gateway` mode, and non-interactive runs refuse that mode [S6]. So Gemini CLI needs `selectedType: "gemini-api-key"` in its settings.

In interactive mode it offers that choice itself when `GEMINI_API_KEY` is set [S6]. An org can require it with `security.auth.enforcedType` in the system settings file [S6].

`seatbelt run gemini` sets the following:

- `GEMINI_API_KEY` (the employee's gateway key; the environment wins over a stored key [S6]);
- `GOOGLE_GEMINI_BASE_URL`;
- `GEMINI_CLI_CUSTOM_HEADERS="X-Seatbelt-Run: <run>"`, appended to any headers already set. This variable is undocumented; its comma-separated `Name: value` parser is in the source [S6].

It also strips the following from the environment:

- `GOOGLE_API_KEY`;
- `GOOGLE_GENAI_USE_VERTEXAI` and `GOOGLE_GENAI_USE_GCA`, which switch Gemini CLI to modes that do not use the base URL [S6].

A `.gemini/.env` file cannot override these variables, because it only sets what the environment lacks [S6].

Before it launches Gemini CLI, `seatbelt run gemini` reads the user's and the workspace's `settings.json`. The user's is under `$GEMINI_CLI_HOME` or the home directory [S6]; the workspace's wins when Gemini CLI trusts the folder [S6]. It warns in two cases:

- when `selectedType` is not `gemini-api-key`;
- when `privacy.usageStatisticsEnabled` is not `false`. Usage statistics go straight to `play.googleapis.com`, not through the gateway, and are on by default [S6].

A settings file with comments is not parsed, and no warning is given for it.

## Tested end to end

Gemini CLI 0.61.0 (`@google/gemini-cli` from npm) ran through `seatbelt run gemini` and the gateway, against a stand-in for the Gemini API that asked for one `list_directory` call and then answered [S8]. The upstream saw the real key in `x-goog-api-key`, no run header and no seatbelt key. The ledger recorded the request, the response, the call and its result linked, and a second request and response, and it verified as signed. With `list_directory` denied by a config reload, the request carrying its result was refused with 403, and Gemini CLI showed the gateway's message from `error.message`. A headless run in an untrusted folder needs `GEMINI_CLI_TRUST_WORKSPACE=true` [S8]; that is Gemini CLI's rule, not the gateway's.

## Open

- The live probes could not see a successful response [S7]. Unconfirmed:
  - the SSE separator. The gateway's SSE parser accepts `\n\n` and `\r\n\r\n` alike;
  - whether `usageMetadata` repeats on every chunk. The last one wins either way;
  - whether the API fills in `functionCall.id`. Both cases are handled.
- Whether `toolUsePromptTokenCount` is part of `promptTokenCount`. It is recorded apart and not added to anything.
- Vertex AI and Gemini CLI's Google login use other paths and body shapes [S6]. They are not served.
- Gemini CLI's voice transcription opens a WebSocket straight to Google with a hard-coded host [S6]. It does not go through the gateway.

## Source map

| Ref | Source | Used for |
|---|---|---|
| S1 | https://ai.google.dev/api/generate-content, /api/tokens, /api/models | endpoints; request and response fields; `FunctionCall` and `FunctionResponse`; `UsageMetadata` and `totalTokenCount`; "a stream of GenerateContentResponse instances" |
| S2 | https://ai.google.dev/api ("must include a x-goog-api-key header") | auth header |
| S3 | https://docs.cloud.google.com/apis/docs/system-parameters | `key` parameter ↔ `X-Goog-Api-Key` |
| S4 | https://docs.cloud.google.com/apis/design/errors | error shape, `google.rpc.Code` names |
| S5 | googleapis/js-genai 1.30.0: `src/_api_client.ts` (forces `alt=sse`; SSE `data:` parsing), `src/node/_node_auth.ts` (`x-goog-api-key`), `src/models.ts` (`embedContent` → `batchEmbedContents`), `src/types.ts` (partial function-call arguments "not supported in Gemini API") | client behaviour |
| S6 | google-gemini/gemini-cli at `2fe7c2d3f065dc40ad573d50b2091116f8a4aa18`: `core/src/core/contentGenerator.ts` (`getAuthTypeFromEnv`; key order), `cli/src/config/auth.ts` and `cli/src/validateNonInterActiveAuth.ts` (`gateway` refused), `cli/src/ui/auth/useAuth.ts`, `core/src/utils/customHeaderUtils.ts`, `core/src/core/turn.ts` and `core/src/core/geminiChat.ts` (`<name>__<id>`; last `finishReason` wins; calls without a `finishReason`), `core/src/utils/generateContentResponseUtilities.ts` and `core/src/scheduler/scheduler.ts` (`{output}` / `{error}`), `core/src/utils/tokenCalculation.ts` (`countTokens`), `cli/src/config/settingsSchema.ts` (`usageStatisticsEnabled` default, `enforcedType`), `core/src/telemetry/clearcut-logger/clearcut-logger.ts` (`play.googleapis.com`), `cli/src/config/settings.ts` (settings precedence; `.env` never overrides), `core/src/utils/paths.ts` (`GEMINI_CLI_HOME`), `core/src/voice/geminiLiveTranscriptionProvider.ts` | Gemini CLI behaviour |
| S7 | live probes of `generativelanguage.googleapis.com` with an invalid key, 2026-09-28 | an SSE request's error is HTTP 400, plain JSON; a non-SSE stream is a JSON array |
| S8 | end-to-end run on 2026-09-28: `@google/gemini-cli` 0.61.0 from npm through `seatbelt run gemini` and the gateway, against a local stand-in upstream | ids Gemini CLI 0.61.0 sends; the whole path, denial included |
