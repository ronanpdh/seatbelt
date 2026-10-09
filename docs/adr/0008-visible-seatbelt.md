# 8. `seatbelt run` shows itself, and its refusals say what to do

- Status: accepted
- Date: 2026-10-08

## Context

Once Claude Code takes the screen, nothing says the session is recorded or under an org policy, and seatbelt's own lines look like the CLI's. Three things go wrong under a gateway policy ([issue #46](https://github.com/ronanpdh/seatbelt/issues/46)):

- Claude Code reads the gateway's 403 as a sign-in failure. It retries, then shows "Authentication error" without the gateway's message. A refused model does not say which models the policy allows, and no refusal says how to fix it in Claude Code.
- The gateway cannot stop a local tool ([ADR 0006](0006-recording-gateway.md)). A denied tool runs, the gateway refuses the next request, which carries its result, and then every request after it, so the session is stuck.
- A gateway that is down, or a revoked key, is found only after the CLI has started.

[ADR 0007](0007-launcher.md) rejected installing hooks into the CLI, because a hook is tool-specific and the user can turn it off. That still holds for enforcement. This ADR uses a hook only to save the conversation. The gateway's refusal is still the control.

## Decision

- **Every line `seatbelt run` prints starts with its badge**: `[seatbelt]`, or on a terminal a bold, reversed `seatbelt`. This covers its log warnings too.
- **A seatbelt buckles** as the CLI starts and unbuckles when it exits, drawn as issue #46 drew it, with seatbelt's lines below it. It is drawn only on a styled terminal: in full on one with room for it, as a line on a smaller one. `SEATBELT_NO_ANIMATION` turns it off.
- **`GET /seatbelt/policy`** returns the policy, the caller's principal id and the gateway version to an authenticated caller. Nothing is recorded. Before it starts the CLI, `seatbelt run` calls it through a gateway:
  - a 401 or 403, a gateway it cannot reach, or any other failure stops the run;
  - a 404, from a gateway older than this change, lets the run go on as before;
  - otherwise it prints the policy.
- **Claude Code gets `--settings`** from `seatbelt run claude`. Seatbelt's settings are merged into a `--settings` of the user's own, since Claude Code reads only the last one. They hold:
  - a status line: the user's own status line, then ` | `, the badge, and `recording`, or `recording · org policy` under a gateway's policy;
  - through a gateway whose policy denies tools, a PreToolUse hook that denies exactly those tools.
- **The gateway accepts the hook's refusal in the history.** A tool result for a denied call passes, and is recorded as an allowed `policy.check`, only when all of these hold:
  - it is an error;
  - its text is the hook's reason, or that reason after Claude Code's `PreToolUse:<tool> hook error: ` prefix;
  - it has nothing else in it.

  Any other result for a denied call is refused, as before.
- **A refusal says what to do.** After the reason recorded, the message names what the policy allows. For Claude Code (user-agent `claude-cli/`) it also names the fix: `/model`, `CLAUDE_CODE_MAX_OUTPUT_TOKENS`, or `/clear` and `/rewind` to leave a refused history. Claude Code gets the refusal as **422**, which it shows as sent and does not retry. Every other client still gets 403. The ledger records the reason alone, as before.

## Consequences

- The hook runs inside Claude Code, so the user can turn it off, e.g. with `disableAllHooks`, which also hides the status line. `seatbelt run` warns when it is set. Managed settings can block it too (`allowManagedHooksOnly`). In both cases the gateway refuses as before.
- A result that matches the hook's text exactly gives the model no output from the tool, whether the tool ran or not. So the guarantee is unchanged: no output of a denied tool reaches the model.
- The accepted text depends on how Claude Code reports a hook's denial, checked against 2.1.294. If a later version changes it, the gateway refuses those results again, as it did before this change: it fails closed.
- A run through a gateway that cannot be reached no longer starts the CLI.
- 422 is new for Claude Code callers. A script that called the gateway with Claude Code's user-agent and checked for 403 needs to change.

Sources and the checks made: [docs/plans/2026-10-08-visible-seatbelt-design.md](../plans/2026-10-08-visible-seatbelt-design.md).
