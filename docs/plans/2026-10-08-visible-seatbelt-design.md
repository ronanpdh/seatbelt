# A visible seatbelt, and refusals that say what to do (#46): design

**Goal:** `seatbelt run` shows that a session is recorded. When the gateway refuses a request, the person sees why and what to do about it. A tool the policy denies stops before it runs, instead of leaving the session stuck.

**Status:** implemented. Facts come from the checks (C) and docs (D) in the source map, run and read 2026-10-08. Decisions that are ours are marked "(ours)". The decision record is [ADR 0008](../adr/0008-visible-seatbelt.md).

## What Claude Code does today

- **A 403 reads as a sign-in failure.** Claude Code retries it, then shows "Authentication error · This may be a temporary network issue, please try again", without the gateway's message. Given a 422, it shows "API Error: 422 <message>" once and does not retry [C3].
- **A PreToolUse hook can stop a tool.** It prints `hookSpecificOutput.permissionDecision: "deny"` with a `permissionDecisionReason`. The tool does not run, and the next request carries a `tool_result` with `is_error: true` and content `PreToolUse:<tool> hook error: <reason>` [C1, D2].
  - A hook that exits 2 instead puts its own command path in that content [C2], so seatbelt's hook prints JSON (ours).
- **Claude Code reads only the last `--settings`.** With two, a hook in the first did not run [C4]. `--settings` takes inline JSON or a file path, and is accepted before a subcommand and as `--settings=<path>` [C5, D1].
- **Settings precedence:** managed settings, then `--settings`, then `.claude/settings.local.json`, `.claude/settings.json`, and `~/.claude/settings.json` [D1]. So a status line given in `--settings` replaces the user's own, and seatbelt's has to run theirs.
- **A matcher of only letters, digits, `_`, `-`, spaces, `,` and `|` is a list of exact tool names,** separated by `|` or `,`. Any other character makes it an unanchored JavaScript regex [D2]. Seatbelt joins names of only letters, digits, `_` and `-` with `|`, and anchors and escapes anything else as a regex.
- **`disableAllHooks` turns off hooks and a custom status line** [D3]. `allowManagedHooksOnly`, a managed setting, blocks hooks and status lines that are not managed [D2, D4].
- **A status line command** gets the session's JSON on stdin. Each line it prints is a row, ANSI colours show, and its output is shown only when it exits 0 [D4].
- **What the hints name:**
  - `CLAUDE_CODE_MAX_OUTPUT_TOKENS` sets the maximum output tokens for most requests [D5];
  - `/model` switches the model;
  - `/clear` starts a new conversation [D6];
  - `/rewind` goes back to an earlier point in the conversation [D7].

## Design (ours)

1. **Badge.** `seatbelt.gateway.badge.say` prints every line with `[seatbelt]` in front. On a TTY it is a bold, reversed ` seatbelt `, except under `NO_COLOR` or `TERM=dumb` [G1]. While a run is on, seatbelt's and uvicorn's log warnings go through it too. The local recorder's uvicorn gets `log_config=None`: with its own logging config it would replace seatbelt's handler, and its warnings would print without the badge.
2. **Buckle.** The belt is the issue's own drawing, read cell by cell from its recording [G1, C8] into `seatbelt/gateway/buckle.txt`: 34 rows by 121 columns, the buckled belt and 11 frames, each with the time the recording shows it.
   - Buckling plays them in place, 2.11 seconds: the tongue slides into the buckle, then CLICK (dim) and a jolt. The belt stays on the screen, and seatbelt's lines follow below it, as in the recording.
   - Unbuckling, when the CLI exits, plays the sliding frames backwards, 0.78 seconds.
   - It is drawn on a styled terminal wider than 121 columns and taller than 34 rows. A smaller one that can show `━▶▐█▌` gets a line of belt, about 1.4 seconds buckling and 0.8 unbuckling. `SEATBELT_NO_ANIMATION` turns both off. Ctrl-C while it draws gives the cursor back.
   - Rows end `\r\n`, so a terminal that does not turn `\n` into `\r\n` still starts the next row in its first column.
3. **Preflight.** `GET /seatbelt/policy` (authenticated, not recorded) answers `{"principal", "version", "policy": {"models", "tools_denied", "max_output_tokens"}}`.
   - Through a gateway, `seatbelt run` calls it before the CLI starts and prints the policy, a line per rule.
   - A 404 is an older gateway, and the run goes on.
   - A 401 or 403, any other status, an answer that is not the policy, or a gateway it cannot reach stops the run with exit 1.
4. **`--settings` for Claude Code**, built by `seatbelt.gateway.claude_code.launch_arguments`:
   - a `statusLine` that runs `python -m seatbelt.gateway.claude_code statusline --note <note> [<the user's command>]`. It prints the user's line, ` | `, the badge and the note: `recording · org policy` when the gateway's policy sets a rule [G2], `recording` otherwise (ours). It keeps the user's `padding`, `refreshInterval` and `hideVimModeIndicator`. The user's command is the statusLine in effect across `~/.claude/settings.json`, `.claude/settings.json`, `.claude/settings.local.json` and their own `--settings` (a managed statusLine overrides seatbelt's anyway);
   - through a gateway whose policy denies tools, a PreToolUse hook with a matcher for exactly those tools. It runs `python -m seatbelt.gateway.claude_code hook <tools>`, which denies only a listed tool and gives no decision for anything else, or for input it cannot read;
   - a `--settings` of the user's own, before any `--`, is merged in: their keys and hooks stay, and seatbelt's hook is added beside theirs. If it cannot be read, it is left as given, with a warning;
   - when `disableAllHooks` is in effect, `seatbelt run` warns.
5. **The gateway accepts the hook's report.** A tool result for a denied call is refused unless every result for that call is an error whose text, trimmed, is the hook's reason (`[seatbelt] <tool> did not run: the org's policy denies it`) or that reason after `PreToolUse:<tool> hook error: `. Text blocks are joined. Such a result is let through and recorded as an allowed `denylist` check, "stopped before it ran". Only the Anthropic format can carry one.
6. **Refusals.** The reason recorded is unchanged. The message adds what the policy allows and, for Claude Code (user-agent starting `claude-cli/`), what to do there. Claude Code gets 422; any other client 403.

## Checks

| Ref | Check | Result |
|---|---|---|
| C1 | Claude Code 2.1.294 in `-p` mode against a mock Anthropic API that asks for `Bash`, with a PreToolUse hook in `--settings` that prints a JSON deny | `Bash` did not run. The next request's `tool_result`: `{"content": "PreToolUse:Bash hook error: <reason>", "is_error": true}` |
| C2 | The same, with a hook that writes to stderr and exits 2 | content `PreToolUse:Bash hook error: [<hook command path>]: <stderr>\n` |
| C3 | The mock answering every request with 403, then with 422, and an error message | 403: "Authentication error · This may be a temporary network issue, please try again", 3 requests. 422: "API Error: 422 <message>", 1 request |
| C4 | Two `--settings` flags, the hook in the first | the hook did not run; `Bash` ran |
| C5 | `claude --settings '{…}' mcp list`, `claude --settings=s.json --version` | both accepted |
| C6 | `seatbelt gateway serve` with `tools_denied: [Bash]` and `models`, `seatbelt run claude -- -p …` in a clean environment | policy printed before the CLI started; `Bash` did not run; the hook's report reached the upstream; the ledger has `policy.check denylist True "… (Bash): stopped before it ran"` and verifies. A disallowed model: "API Error: 422 model … is not allowed. The org's policy allows …; pick one with /model". With `disableAllHooks` set: the warning, `Bash` ran, then "API Error: 422 tool result for denied call … run /clear …". `curl` with `X-Seatbelt-Run: triage-42`: `x-seatbelt-run-id: alice_corp-triage-42-<hex>` |
| C7 | `seatbelt run claude` in a pseudo-terminal, with `statusLine` `echo my-own-line` in `~/.claude/settings.json` | Claude Code's status line: `my-own-line \| ` and the reversed badge; the belt drawn before Claude Code and after it exited |
| C8 | `buckle()` written to a 130×40 screen of the `pyte` terminal emulator, from the built wheel, each frame compared with the issue's recording [G1] | all 11 frames the same, cell for cell, shown for the same milliseconds |
| C9 | C7 again on 2026-10-09 (Claude Code 2.1.295), a 140×45 pseudo-terminal, through a gateway that denies `Bash`, rendered with `pyte` | the belt and CLICK, then seatbelt's three lines below it; Claude Code's status line `my-own-line \|  seatbelt  recording · org policy`, the badge bold and reversed; the belt unbuckled after `/exit` |

## Docs

| Ref | Page | Used for |
|---|---|---|
| D1 | https://code.claude.com/docs/en/settings, https://code.claude.com/docs/en/cli-reference | precedence; `--settings` takes JSON or a path |
| D2 | https://code.claude.com/docs/en/hooks | matcher rules; a deny's reason goes to Claude; `allowManagedHooksOnly` |
| D3 | https://code.claude.com/docs/en/settings-reference | `disableAllHooks` turns off hooks and a custom status line |
| D4 | https://code.claude.com/docs/en/statusline | stdin JSON, rows, ANSI, exit 0; hidden under `disableAllHooks` or `allowManagedHooksOnly` |
| D5 | https://code.claude.com/docs/en/env-vars | `CLAUDE_CODE_MAX_OUTPUT_TOKENS` |
| D6 | https://code.claude.com/docs/en/commands | `/model`, `/clear` |
| D7 | https://code.claude.com/docs/en/checkpointing | `/rewind` |

## Issue #46's drawings

| Ref | Attachment | Used for |
|---|---|---|
| G1 | the recording `seatbelt-buckle.gif` (1653×1242, 32 frames): a terminal with 13×31-pixel cells, read by the shape of each cell's ink | the belt's rows and frames, their milliseconds, CLICK's place and dimmer grey, the badge (the terminal's colours swapped, bold) and the lines below the belt |
| G2 | the screenshot `statusline.png` (1884×68) | the status line: the user's line, ` \| `, the badge, `recording · org policy` |

## Not done

- **Setting `CLAUDE_CODE_MAX_OUTPUT_TOKENS` from the policy's cap.** Claude Code's default output limit may exceed a cap. In that case every request is refused until the person sets it, which the refusal now tells them to do. Setting it automatically would change Claude Code's behaviour beyond what #46 asked.
- **`POST /seatbelt/runs/{name}/end` returning the run id** (asked in #44). `Sessions.end` would have to return it; left until someone needs it.
