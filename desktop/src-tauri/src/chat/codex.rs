//! Codex's `app-server`: JSON-RPC 2.0 over stdio, one JSON object per line, without the
//! `"jsonrpc"` member. A thread is the chat; each message is a turn; approvals are requests
//! from the server. Check P4 in the design ran this against Codex 0.162.0; the message shapes
//! are those `codex app-server generate-ts` gives for that release.

use std::collections::HashMap;

use serde_json::{json, Value};

use super::protocol::{
    about_sign_in, content_text, cut, shown, str_of, ChatEvent, Driver, Step, ToolStatus,
    MAX_TOOL_TEXT,
};

const APP: &str = "seatbelt-desktop";

#[derive(Default)]
pub struct Codex {
    next_id: u64,
    /// Requests sent and not yet answered, by id: their method.
    pending: HashMap<u64, &'static str>,
    cwd: String,
    thread: Option<String>,
    turn: Option<String>,
    /// A message sent before the thread had started.
    queued: Vec<String>,
    signed_out: bool,
    /// Approvals waiting, by our id: the server's request id as it sent it.
    waiting: HashMap<String, Value>,
    /// What each file change item will touch, for its approval card.
    changes: HashMap<String, String>,
    /// Reasoning items whose summary has streamed: their whole text is not sent again.
    reasoned: std::collections::HashSet<String>,
}

impl Codex {
    fn request(&mut self, method: &'static str, params: Value) -> Value {
        self.next_id += 1;
        self.pending.insert(self.next_id, method);
        json!({"id": self.next_id, "method": method, "params": params})
    }

    fn turn_start(&mut self, thread: &str, text: &str) -> Value {
        self.request(
            "turn/start",
            json!({"threadId": thread, "input": [{"type": "text", "text": text, "text_elements": []}]}),
        )
    }

    fn response(&mut self, line: &Value, step: &mut Step) {
        let Some(method) = line
            .get("id")
            .and_then(Value::as_u64)
            .and_then(|id| self.pending.remove(&id))
        else {
            return;
        };
        if let Some(error) = line.get("error") {
            let message = cut(str_of(error, "message"), 2000);
            match method {
                "turn/start" | "thread/start" => {
                    if about_sign_in(&message) {
                        step.show(ChatEvent::SignIn {
                            reason: message.clone(),
                        });
                    }
                    step.show(ChatEvent::TurnEnd {
                        ok: false,
                        error: Some(message),
                    });
                }
                _ => step.show(ChatEvent::Log {
                    text: format!("{method}: {message}"),
                }),
            }
            return;
        }
        let result = line.get("result").cloned().unwrap_or_default();
        match method {
            "initialize" => {
                step.send(json!({"method": "initialized"}));
                let read = self.request("account/read", json!({}));
                step.send(read);
            }
            "account/read" => {
                let none = result.get("account").is_none_or(Value::is_null);
                let required =
                    result.get("requiresOpenaiAuth").and_then(Value::as_bool) == Some(true);
                if none && required {
                    self.signed_out = true;
                    step.show(ChatEvent::SignIn {
                        reason: "Codex is not signed in.".into(),
                    });
                } else {
                    let cwd = self.cwd.clone();
                    let start = self.request("thread/start", json!({"cwd": cwd}));
                    step.send(start);
                }
            }
            "thread/start" => {
                let thread = result.pointer("/thread/id").and_then(Value::as_str);
                if let Some(thread) = thread.map(str::to_string) {
                    for text in std::mem::take(&mut self.queued) {
                        let line = self.turn_start(&thread, &text);
                        step.send(line);
                    }
                    self.thread = Some(thread);
                }
            }
            "turn/start" => {
                if let Some(turn) = result.pointer("/turn/id").and_then(Value::as_str) {
                    self.turn = Some(turn.to_string());
                }
            }
            _ => {}
        }
    }

    fn item(&mut self, params: &Value, done: bool, step: &mut Step) {
        let item = params.get("item").cloned().unwrap_or_default();
        let id = str_of(&item, "id").to_string();
        let status = match str_of(&item, "status") {
            "failed" => ToolStatus::Failed,
            "declined" => ToolStatus::Declined,
            _ if done => ToolStatus::Done,
            _ => ToolStatus::Running,
        };
        let tool = |name: &str, detail: String| ChatEvent::Tool {
            id: id.clone(),
            name: name.to_string(),
            detail,
            status,
        };
        match str_of(&item, "type") {
            "agentMessage" if done => step.show(ChatEvent::Message {
                id: id.clone(),
                text: str_of(&item, "text").to_string(),
            }),
            "reasoning" if done && !self.reasoned.remove(&id) => {
                let parts = |key: &str| -> Vec<String> {
                    item.get(key)
                        .and_then(Value::as_array)
                        .into_iter()
                        .flatten()
                        .filter_map(|p| p.as_str().map(str::to_string))
                        .collect()
                };
                let mut text = parts("summary");
                if text.is_empty() {
                    text = parts("content");
                }
                if !text.is_empty() {
                    step.show(ChatEvent::Thinking {
                        id: id.clone(),
                        delta: text.join("\n\n"),
                    });
                }
            }
            "commandExecution" => {
                let command = str_of(&item, "command");
                step.show(tool("Shell", cut(command, 2000)));
                if done {
                    let mut output = str_of(&item, "aggregatedOutput").to_string();
                    if let Some(code) = item.get("exitCode").and_then(Value::as_i64) {
                        if code != 0 {
                            output.push_str(&format!("\n[exit code {code}]"));
                        }
                    }
                    step.show(ChatEvent::ToolOutput {
                        id: id.clone(),
                        output: cut(output.trim_start_matches('\n'), MAX_TOOL_TEXT),
                    });
                } else {
                    let cwd = str_of(&item, "cwd");
                    let input = if cwd.is_empty() {
                        command.to_string()
                    } else {
                        format!("{command}\n\nin {cwd}")
                    };
                    step.show(ChatEvent::ToolInput {
                        id: id.clone(),
                        input: cut(&input, MAX_TOOL_TEXT),
                    });
                }
            }
            "fileChange" => {
                let paths: Vec<&str> = item
                    .get("changes")
                    .and_then(Value::as_array)
                    .into_iter()
                    .flatten()
                    .map(|c| str_of(c, "path"))
                    .collect();
                let detail = cut(&paths.join("\n"), 2000);
                self.changes.insert(id.clone(), detail.clone());
                step.show(tool("Edit", detail));
                let diffs: Vec<String> = item
                    .get("changes")
                    .and_then(Value::as_array)
                    .into_iter()
                    .flatten()
                    .map(|c| {
                        format!(
                            "{} ({})\n{}",
                            str_of(c, "path"),
                            str_of(c, "kind"),
                            str_of(c, "diff")
                        )
                    })
                    .collect();
                if !done {
                    // what an edit is given is its diff
                    step.show(ChatEvent::ToolInput {
                        id: id.clone(),
                        input: cut(&diffs.join("\n\n"), MAX_TOOL_TEXT),
                    });
                }
            }
            "mcpToolCall" => {
                let name = format!("{}.{}", str_of(&item, "server"), str_of(&item, "tool"));
                let args = item.get("arguments").cloned().unwrap_or_default();
                step.show(tool(&name, cut(&args.to_string(), 2000)));
                if done {
                    let output = match (item.get("error"), item.pointer("/result/content")) {
                        (Some(e), _) if !e.is_null() => str_of(e, "message").to_string(),
                        (_, Some(content)) => content_text(content),
                        _ => String::new(),
                    };
                    step.show(ChatEvent::ToolOutput {
                        id: id.clone(),
                        output,
                    });
                } else {
                    step.show(ChatEvent::ToolInput {
                        id: id.clone(),
                        input: shown(&args),
                    });
                }
            }
            "webSearch" => step.show(tool("Web search", cut(str_of(&item, "query"), 2000))),
            _ => {}
        }
    }

    fn server_request(&mut self, line: &Value, step: &mut Step) {
        let rpc_id = line.get("id").cloned().unwrap_or_default();
        let params = line.get("params").cloned().unwrap_or_default();
        let card = match str_of(line, "method") {
            "item/commandExecution/requestApproval" => {
                let mut detail = str_of(&params, "command").to_string();
                let cwd = str_of(&params, "cwd");
                if !cwd.is_empty() {
                    detail.push_str(&format!("\nin {cwd}"));
                }
                Some(("Shell", detail, &params))
            }
            "item/fileChange/requestApproval" => {
                let item = str_of(&params, "itemId");
                let detail = self.changes.get(item).cloned().unwrap_or_default();
                Some(("Edit", detail, &params))
            }
            _ => None,
        };
        let Some((tool, mut detail, params)) = card else {
            // user input, permission profiles, MCP elicitations, token refresh: not offered
            step.send(json!({"id": rpc_id, "error": {"code": -32601,
                "message": "Seatbelt does not handle this request"}}));
            return;
        };
        let reason = str_of(params, "reason");
        if !reason.is_empty() {
            detail = format!("{reason}\n{detail}");
        }
        let id = format!("codex-{}", rpc_id.to_string().trim_matches('"'));
        self.waiting.insert(id.clone(), rpc_id);
        step.show(ChatEvent::Approval {
            id,
            tool: tool.to_string(),
            detail: cut(detail.trim(), 4000),
        });
    }
}

impl Driver for Codex {
    fn args(&self) -> Vec<String> {
        vec!["app-server".into()]
    }

    fn start(&mut self, cwd: &str) -> Step {
        self.cwd = cwd.to_string();
        let mut step = Step::default();
        let init = self.request(
            "initialize",
            json!({"clientInfo": {"name": APP, "title": "Seatbelt",
                "version": env!("CARGO_PKG_VERSION")}, "capabilities": null}),
        );
        step.send(init);
        step
    }

    fn read(&mut self, line: Value) -> Step {
        let mut step = Step::default();
        let has_id = line.get("id").is_some();
        let Some(method) = line
            .get("method")
            .and_then(Value::as_str)
            .map(str::to_string)
        else {
            if has_id {
                self.response(&line, &mut step);
            }
            return step;
        };
        if has_id {
            self.server_request(&line, &mut step);
            return step;
        }
        let params = line.get("params").cloned().unwrap_or_default();
        match method.as_str() {
            "item/agentMessage/delta" => step.show(ChatEvent::Text {
                id: str_of(&params, "itemId").to_string(),
                delta: str_of(&params, "delta").to_string(),
            }),
            "item/reasoning/summaryTextDelta" => {
                let id = str_of(&params, "itemId").to_string();
                self.reasoned.insert(id.clone());
                step.show(ChatEvent::Thinking {
                    id,
                    delta: str_of(&params, "delta").to_string(),
                });
            }
            "item/started" => self.item(&params, false, &mut step),
            "item/completed" => self.item(&params, true, &mut step),
            "turn/started" => {
                if let Some(turn) = params.pointer("/turn/id").and_then(Value::as_str) {
                    self.turn = Some(turn.to_string());
                }
            }
            "serverRequest/resolved" => {
                // answered, or withdrawn by the server: either way the card is done
                let rpc = params.get("requestId").cloned().unwrap_or_default();
                let found = self
                    .waiting
                    .iter()
                    .find(|(_, v)| **v == rpc)
                    .map(|(k, _)| k.clone());
                if let Some(id) = found {
                    self.waiting.remove(&id);
                    step.show(ChatEvent::Resolved { id });
                }
            }
            "turn/completed" => {
                let turn = params.get("turn").cloned().unwrap_or_default();
                self.turn = None;
                for (id, _) in self.waiting.drain() {
                    step.show(ChatEvent::Resolved { id });
                }
                let error = turn.pointer("/error/message").and_then(Value::as_str);
                let unauthorized = turn
                    .pointer("/error/codexErrorInfo")
                    .and_then(Value::as_str)
                    == Some("unauthorized");
                if unauthorized {
                    step.show(ChatEvent::SignIn {
                        reason: "Codex's sign-in was refused.".into(),
                    });
                }
                let ok = str_of(&turn, "status") == "completed";
                let error = match (ok, error) {
                    (true, _) => None,
                    (false, Some(e)) => Some(cut(e, 2000)),
                    (false, None) => Some(str_of(&turn, "status").to_string()),
                };
                step.show(ChatEvent::TurnEnd { ok, error });
            }
            "error" => {
                let message = params.pointer("/error/message").and_then(Value::as_str);
                if let Some(message) = message {
                    step.show(ChatEvent::Log {
                        text: cut(message, 2000),
                    });
                }
            }
            _ => {}
        }
        step
    }

    fn send(&mut self, text: &str) -> Result<Step, String> {
        if self.signed_out {
            return Err("Codex is not signed in: sign in, then start the chat again".into());
        }
        let mut step = Step::default();
        match self.thread.clone() {
            Some(thread) => {
                let line = self.turn_start(&thread, text);
                step.send(line);
            }
            None => self.queued.push(text.to_string()),
        }
        Ok(step)
    }

    fn answer(&mut self, id: &str, allow: bool) -> Result<Step, String> {
        let rpc_id = self
            .waiting
            .remove(id)
            .ok_or("that request is no longer waiting")?;
        let decision = if allow { "accept" } else { "decline" };
        let mut step = Step::default();
        step.send(json!({"id": rpc_id, "result": {"decision": decision}}));
        step.show(ChatEvent::Resolved { id: id.to_string() });
        Ok(step)
    }

    fn interrupt(&mut self) -> Step {
        let mut step = Step::default();
        let waiting: Vec<(String, Value)> = self.waiting.drain().collect();
        for (id, rpc_id) in waiting {
            step.send(json!({"id": rpc_id, "result": {"decision": "cancel"}}));
            step.show(ChatEvent::Resolved { id });
        }
        if let (Some(thread), Some(turn)) = (self.thread.clone(), self.turn.clone()) {
            let line = self.request(
                "turn/interrupt",
                json!({"threadId": thread, "turnId": turn}),
            );
            step.send(line);
        }
        step
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn line(s: &str) -> Value {
        serde_json::from_str(s).unwrap()
    }

    /// Start a session as P4 did: initialize, account, thread.
    fn started(account: &str) -> (Codex, Step) {
        let mut c = Codex::default();
        let mut all = c.start("/work");
        for l in [
            r#"{"id":1,"result":{"userAgent":"x","codexHome":"/h","platformFamily":"unix","platformOs":"linux"}}"#.to_string(),
            format!(r#"{{"id":2,"result":{account}}}"#),
            r#"{"id":3,"result":{"thread":{"id":"th-1"},"model":"gpt"}}"#.to_string(),
        ] {
            let step = c.read(line(&l));
            all.events.extend(step.events);
            all.write.extend(step.write);
        }
        (c, all)
    }

    #[test]
    fn a_session_initializes_checks_the_account_and_starts_a_thread() {
        let (c, step) = started(
            r#"{"account":{"type":"chatgpt","email":null,"planType":"plus"},"requiresOpenaiAuth":true}"#,
        );
        assert_eq!(
            step.write,
            vec![
                json!({"id": 1, "method": "initialize", "params": {"clientInfo": {"name": APP,
                    "title": "Seatbelt", "version": env!("CARGO_PKG_VERSION")}, "capabilities": null}}),
                json!({"method": "initialized"}),
                json!({"id": 2, "method": "account/read", "params": {}}),
                json!({"id": 3, "method": "thread/start", "params": {"cwd": "/work"}}),
            ]
        );
        assert!(step.events.is_empty());
        assert_eq!(c.thread.as_deref(), Some("th-1"));
    }

    #[test]
    fn no_account_where_one_is_needed_asks_for_sign_in() {
        let (mut c, step) = started(r#"{"account":null,"requiresOpenaiAuth":true}"#);
        assert!(matches!(step.events[..], [ChatEvent::SignIn { .. }]));
        assert_eq!(step.write.len(), 3); // no thread started
        assert!(c.send("hi").is_err());
    }

    #[test]
    fn a_message_sent_before_the_thread_is_ready_waits_for_it() {
        let mut c = Codex::default();
        c.start("/w");
        assert!(c.send("early").unwrap().write.is_empty());
        c.read(line(r#"{"id":1,"result":{}}"#));
        c.read(line(
            r#"{"id":2,"result":{"account":null,"requiresOpenaiAuth":false}}"#,
        ));
        let step = c.read(line(r#"{"id":3,"result":{"thread":{"id":"th-9"}}}"#));
        assert_eq!(
            step.write,
            vec![
                json!({"id": 4, "method": "turn/start", "params": {"threadId": "th-9",
            "input": [{"type": "text", "text": "early", "text_elements": []}]}})
            ]
        );
    }

    #[test]
    fn a_turn_with_a_command_approval_runs_as_p4_did() {
        let (mut c, _) = started(r#"{"account":null,"requiresOpenaiAuth":false}"#);
        c.send("please run it").unwrap();
        let lines = [
            r#"{"id":4,"result":{"turn":{"id":"tu-1","status":"inProgress"}}}"#,
            r#"{"method":"item/started","params":{"item":{"type":"commandExecution","id":"call_1","command":"/bin/bash -lc 'echo hi > out.txt'","cwd":"/work","status":"inProgress"},"threadId":"th-1","turnId":"tu-1"}}"#,
            r#"{"method":"item/commandExecution/requestApproval","id":0,"params":{"kind":"command","threadId":"th-1","turnId":"tu-1","itemId":"call_1","command":"/bin/bash -lc 'echo hi > out.txt'","cwd":"/work"}}"#,
        ];
        let mut events = vec![];
        for l in lines {
            events.extend(c.read(line(l)).events);
        }
        assert_eq!(
            events,
            vec![
                ChatEvent::Tool {
                    id: "call_1".into(),
                    name: "Shell".into(),
                    detail: "/bin/bash -lc 'echo hi > out.txt'".into(),
                    status: ToolStatus::Running
                },
                ChatEvent::ToolInput {
                    id: "call_1".into(),
                    input: "/bin/bash -lc 'echo hi > out.txt'\n\nin /work".into(),
                },
                ChatEvent::Approval {
                    id: "codex-0".into(),
                    tool: "Shell".into(),
                    detail: "/bin/bash -lc 'echo hi > out.txt'\nin /work".into()
                },
            ]
        );
        let step = c.answer("codex-0", true).unwrap();
        assert_eq!(
            step.write,
            vec![json!({"id": 0, "result": {"decision": "accept"}})]
        );
        let rest = [
            r#"{"method":"serverRequest/resolved","params":{"threadId":"th-1","requestId":0}}"#,
            r#"{"method":"item/completed","params":{"item":{"type":"commandExecution","id":"call_1","command":"x","status":"completed","aggregatedOutput":"wrote it\n","exitCode":0}}}"#,
            r#"{"method":"item/reasoning/summaryTextDelta","params":{"itemId":"r1","delta":"**Checking** the file"}}"#,
            r#"{"method":"item/completed","params":{"item":{"type":"reasoning","id":"r1","summary":["**Checking** the file"],"content":[]}}}"#,
            r#"{"method":"item/completed","params":{"item":{"type":"reasoning","id":"r2","summary":["Whole"],"content":[]}}}"#,
            r#"{"method":"item/agentMessage/delta","params":{"itemId":"m1","delta":"All "}}"#,
            r#"{"method":"item/completed","params":{"item":{"type":"agentMessage","id":"m1","text":"All done."}}}"#,
            r#"{"method":"turn/completed","params":{"threadId":"th-1","turn":{"id":"tu-1","status":"completed","error":null}}}"#,
        ];
        let mut events = vec![];
        for l in rest {
            events.extend(c.read(line(l)).events);
        }
        assert_eq!(
            events,
            vec![
                ChatEvent::Tool {
                    id: "call_1".into(),
                    name: "Shell".into(),
                    detail: "x".into(),
                    status: ToolStatus::Done
                },
                ChatEvent::ToolOutput {
                    id: "call_1".into(),
                    output: "wrote it\n".into(),
                },
                ChatEvent::Thinking {
                    id: "r1".into(),
                    delta: "**Checking** the file".into(),
                },
                ChatEvent::Thinking {
                    id: "r2".into(),
                    delta: "Whole".into(),
                },
                ChatEvent::Text {
                    id: "m1".into(),
                    delta: "All ".into()
                },
                ChatEvent::Message {
                    id: "m1".into(),
                    text: "All done.".into()
                },
                ChatEvent::TurnEnd {
                    ok: true,
                    error: None
                },
            ]
        );
    }

    #[test]
    fn declining_interrupting_and_unhandled_requests() {
        let (mut c, _) = started(r#"{"account":null,"requiresOpenaiAuth":false}"#);
        c.send("x").unwrap();
        c.read(line(r#"{"id":4,"result":{"turn":{"id":"tu-1"}}}"#));
        c.read(line(r#"{"method":"item/started","params":{"item":{"type":"fileChange","id":"fc","changes":[{"path":"a.rs","kind":"update","diff":""}],"status":"inProgress"}}}"#));
        let ask = c.read(line(r#"{"method":"item/fileChange/requestApproval","id":"s-1","params":{"itemId":"fc","reason":"needs write"}}"#));
        assert_eq!(
            ask.events.last(),
            Some(&ChatEvent::Approval {
                id: "codex-s-1".into(),
                tool: "Edit".into(),
                detail: "needs write\na.rs".into()
            })
        );
        assert_eq!(
            c.answer("codex-s-1", false).unwrap().write,
            vec![json!({"id": "s-1", "result": {"decision": "decline"}})]
        );
        c.read(line(r#"{"method":"item/commandExecution/requestApproval","id":7,"params":{"command":"rm -rf x"}}"#));
        let stop = c.interrupt();
        assert_eq!(
            stop.write,
            vec![
                json!({"id": 7, "result": {"decision": "cancel"}}),
                json!({"id": 5, "method": "turn/interrupt", "params": {"threadId": "th-1", "turnId": "tu-1"}}),
            ]
        );
        let other = c.read(line(
            r#"{"method":"item/tool/requestUserInput","id":8,"params":{}}"#,
        ));
        assert_eq!(other.write[0]["error"]["code"], -32601);
        let end = c.read(line(r#"{"method":"turn/completed","params":{"turn":{"id":"tu-1","status":"failed","error":{"message":"401 Unauthorized","codexErrorInfo":"unauthorized"}}}}"#));
        assert!(matches!(end.events[0], ChatEvent::SignIn { .. }));
        assert_eq!(
            end.events[1],
            ChatEvent::TurnEnd {
                ok: false,
                error: Some("401 Unauthorized".into())
            }
        );
    }
}
