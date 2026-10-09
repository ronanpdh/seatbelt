//! Claude Code's headless session: `-p` with stream-json in and out, permission prompts
//! over its control protocol (`--permission-prompt-tool stdio`, as the Agent SDK starts it).
//! Checks P1 to P3 in the design ran this against Claude Code 2.1.295.

use std::collections::{HashMap, HashSet};

use serde_json::{json, Value};

use super::protocol::{cut, str_of, summary, ChatEvent, Driver, Step, ToolStatus};

/// The message a denied tool use gets, which Claude reads.
const DENIED: &str = "The user denied this in Seatbelt.";

#[derive(Default)]
pub struct Claude {
    /// The assistant message being streamed, by our own id.
    message: Option<String>,
    streamed: bool,
    messages: u32,
    /// Approvals waiting: request id, then the tool's input and its tool use id.
    waiting: HashMap<String, (Value, String)>,
    declined: HashSet<String>,
    interrupts: u32,
    /// This turn was interrupted, by the user or after a sign-in failure.
    interrupted: bool,
    /// Sign-in failure seen this turn: said once, and the turn is interrupted, not retried.
    signin_said: bool,
}

impl Claude {
    fn next_message(&mut self) -> String {
        self.messages += 1;
        self.streamed = false;
        let id = format!("claude-{}", self.messages);
        self.message = Some(id.clone());
        id
    }

    fn interrupt_line(&mut self) -> Value {
        self.interrupts += 1;
        self.interrupted = true;
        json!({
            "type": "control_request",
            "request_id": format!("seatbelt-interrupt-{}", self.interrupts),
            "request": {"subtype": "interrupt"},
        })
    }

    fn control(&mut self, line: &Value, step: &mut Step) {
        let request_id = str_of(line, "request_id").to_string();
        let request = line.get("request").cloned().unwrap_or_default();
        if str_of(&request, "subtype") != "can_use_tool" {
            // hooks, MCP messages and the like: nothing this app registered for
            step.send(json!({
                "type": "control_response",
                "response": {
                    "subtype": "error",
                    "request_id": request_id,
                    "error": "Seatbelt does not handle this request",
                },
            }));
            return;
        }
        let input = request.get("input").cloned().unwrap_or_else(|| json!({}));
        let tool = match str_of(&request, "display_name") {
            "" => str_of(&request, "tool_name"),
            name => name,
        };
        let what = summary(&input);
        let why = str_of(&request, "description");
        let detail = match (why.is_empty(), what.is_empty()) {
            (false, false) if why != what => format!("{why}\n{what}"),
            (false, _) => why.to_string(),
            _ => what,
        };
        let tool_use = str_of(&request, "tool_use_id").to_string();
        self.waiting.insert(request_id.clone(), (input, tool_use));
        step.show(ChatEvent::Approval {
            id: request_id,
            tool: tool.to_string(),
            detail,
        });
    }

    fn deny_line(request_id: &str) -> Value {
        json!({
            "type": "control_response",
            "response": {
                "subtype": "success",
                "request_id": request_id,
                "response": {"behavior": "deny", "message": DENIED},
            },
        })
    }
}

impl Driver for Claude {
    fn args(&self) -> Vec<String> {
        [
            "-p",
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--verbose",
            "--include-partial-messages",
            "--permission-prompt-tool",
            "stdio",
        ]
        .map(String::from)
        .to_vec()
    }

    fn start(&mut self, _cwd: &str) -> Step {
        Step::default() // the session starts with the first message
    }

    fn read(&mut self, line: Value) -> Step {
        let mut step = Step::default();
        match str_of(&line, "type") {
            "stream_event" => {
                let event = line.get("event").cloned().unwrap_or_default();
                match str_of(&event, "type") {
                    "message_start" => {
                        self.next_message();
                    }
                    "content_block_delta" => {
                        let delta = event.get("delta").cloned().unwrap_or_default();
                        if str_of(&delta, "type") == "text_delta" {
                            let id = match &self.message {
                                Some(id) => id.clone(),
                                None => self.next_message(),
                            };
                            self.streamed = true;
                            step.show(ChatEvent::Text {
                                id,
                                delta: str_of(&delta, "text").to_string(),
                            });
                        }
                    }
                    _ => {}
                }
            }
            "assistant" => {
                let content = line.pointer("/message/content").cloned();
                for block in content
                    .as_ref()
                    .and_then(Value::as_array)
                    .into_iter()
                    .flatten()
                {
                    match str_of(block, "type") {
                        "text" if !self.streamed => {
                            let id = self.next_message();
                            step.show(ChatEvent::Message {
                                id,
                                text: str_of(block, "text").to_string(),
                            });
                        }
                        "tool_use" => step.show(ChatEvent::Tool {
                            id: str_of(block, "id").to_string(),
                            name: str_of(block, "name").to_string(),
                            detail: summary(block.get("input").unwrap_or(&Value::Null)),
                            status: ToolStatus::Running,
                        }),
                        _ => {}
                    }
                }
            }
            "user" => {
                let content = line.pointer("/message/content").cloned();
                for block in content
                    .as_ref()
                    .and_then(Value::as_array)
                    .into_iter()
                    .flatten()
                {
                    if str_of(block, "type") != "tool_result" {
                        continue;
                    }
                    let id = str_of(block, "tool_use_id").to_string();
                    let status = if self.declined.remove(&id) {
                        ToolStatus::Declined
                    } else if block.get("is_error").and_then(Value::as_bool) == Some(true) {
                        ToolStatus::Failed
                    } else {
                        ToolStatus::Done
                    };
                    step.show(ChatEvent::Tool {
                        id,
                        name: String::new(),
                        detail: String::new(),
                        status,
                    });
                }
                // what follows a tool's result is a new message
                self.message = None;
                self.streamed = false;
            }
            "control_request" => self.control(&line, &mut step),
            "system" => {
                let auth = str_of(&line, "error") == "authentication_failed"
                    || line.get("error_status").and_then(Value::as_u64) == Some(401);
                if str_of(&line, "subtype") == "api_retry" && auth && !self.signin_said {
                    self.signin_said = true;
                    step.show(ChatEvent::SignIn {
                        reason: "Claude Code could not sign in to Anthropic (401).".into(),
                    });
                    let stop = self.interrupt_line();
                    step.send(stop);
                }
            }
            "result" => {
                let failed = line.get("is_error").and_then(Value::as_bool) == Some(true)
                    || !matches!(str_of(&line, "subtype"), "success" | "");
                let error = failed.then(|| {
                    let said = str_of(&line, "result");
                    if !said.is_empty() {
                        cut(said, 2000)
                    } else if self.interrupted {
                        "Stopped.".to_string()
                    } else {
                        str_of(&line, "subtype").to_string()
                    }
                });
                self.interrupted = false;
                self.message = None;
                self.streamed = false;
                self.signin_said = false;
                for (id, _) in self.waiting.drain() {
                    step.show(ChatEvent::Resolved { id });
                }
                step.show(ChatEvent::TurnEnd { ok: !failed, error });
            }
            _ => {}
        }
        step
    }

    fn send(&mut self, text: &str) -> Result<Step, String> {
        let mut step = Step::default();
        step.send(json!({
            "type": "user",
            "message": {"role": "user", "content": text},
            "parent_tool_use_id": null,
        }));
        Ok(step)
    }

    fn answer(&mut self, id: &str, allow: bool) -> Result<Step, String> {
        let (input, tool_use) = self
            .waiting
            .remove(id)
            .ok_or("that request is no longer waiting")?;
        let mut step = Step::default();
        if allow {
            step.send(json!({
                "type": "control_response",
                "response": {
                    "subtype": "success",
                    "request_id": id,
                    "response": {"behavior": "allow", "updatedInput": input},
                },
            }));
        } else {
            self.declined.insert(tool_use);
            step.send(Self::deny_line(id));
        }
        step.show(ChatEvent::Resolved { id: id.to_string() });
        Ok(step)
    }

    fn interrupt(&mut self) -> Step {
        let mut step = Step::default();
        let waiting: Vec<String> = self.waiting.drain().map(|(id, _)| id).collect();
        for id in waiting {
            step.send(Self::deny_line(&id));
            step.show(ChatEvent::Resolved { id });
        }
        let stop = self.interrupt_line();
        step.send(stop);
        step
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Lines Claude Code 2.1.295 printed in check P1, cut to the fields that matter.
    fn p1() -> Vec<Value> {
        [
            r#"{"type":"system","subtype":"init","model":"claude-opus-5-5","permissionMode":"default"}"#,
            r#"{"type":"stream_event","event":{"type":"message_start","message":{"id":"msg_1"}}}"#,
            r#"{"type":"stream_event","event":{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Running "}}}"#,
            r#"{"type":"stream_event","event":{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"it."}}}"#,
            r#"{"type":"assistant","message":{"id":"msg_1","content":[{"type":"text","text":"Running it."}]}}"#,
            r#"{"type":"assistant","message":{"id":"msg_1","content":[{"type":"tool_use","id":"toolu_01","name":"Bash","input":{"command":"echo hi > out.txt","description":"Write a file"}}]}}"#,
            r#"{"type":"control_request","request_id":"req-1","request":{"subtype":"can_use_tool","tool_name":"Bash","display_name":"Bash","input":{"command":"echo hi > out.txt","description":"Write a file"},"description":"Write a file","tool_use_id":"toolu_01"}}"#,
        ]
        .iter()
        .map(|l| serde_json::from_str(l).unwrap())
        .collect()
    }

    fn feed(c: &mut Claude, lines: Vec<Value>) -> Step {
        let mut all = Step::default();
        for line in lines {
            let step = c.read(line);
            all.events.extend(step.events);
            all.write.extend(step.write);
        }
        all
    }

    fn line(s: &str) -> Value {
        serde_json::from_str(s).unwrap()
    }

    #[test]
    fn streamed_text_a_tool_and_its_approval_become_chat_events() {
        let mut c = Claude::default();
        let step = feed(&mut c, p1());
        assert!(step.write.is_empty());
        assert_eq!(
            step.events,
            vec![
                ChatEvent::Text {
                    id: "claude-1".into(),
                    delta: "Running ".into()
                },
                ChatEvent::Text {
                    id: "claude-1".into(),
                    delta: "it.".into()
                },
                ChatEvent::Tool {
                    id: "toolu_01".into(),
                    name: "Bash".into(),
                    detail: "echo hi > out.txt".into(),
                    status: ToolStatus::Running,
                },
                ChatEvent::Approval {
                    id: "req-1".into(),
                    tool: "Bash".into(),
                    detail: "Write a file\necho hi > out.txt".into(),
                },
            ]
        );
    }

    #[test]
    fn allowing_sends_the_input_unchanged_and_the_result_marks_the_tool_done() {
        let mut c = Claude::default();
        feed(&mut c, p1());
        let step = c.answer("req-1", true).unwrap();
        assert_eq!(
            step.write,
            vec![
                json!({"type": "control_response", "response": {"subtype": "success",
                "request_id": "req-1", "response": {"behavior": "allow",
                "updatedInput": {"command": "echo hi > out.txt", "description": "Write a file"}}}})
            ]
        );
        assert_eq!(
            step.events,
            vec![ChatEvent::Resolved { id: "req-1".into() }]
        );
        assert!(c.answer("req-1", true).is_err()); // answered once only
        let after = feed(
            &mut c,
            vec![
                line(
                    r#"{"type":"user","message":{"role":"user","content":[{"tool_use_id":"toolu_01","type":"tool_result","content":"(Bash completed with no output)","is_error":false}]}}"#,
                ),
                line(
                    r#"{"type":"stream_event","event":{"type":"message_start","message":{"id":"msg_1"}}}"#,
                ),
                line(
                    r#"{"type":"stream_event","event":{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Done."}}}"#,
                ),
                line(r#"{"type":"result","subtype":"success","is_error":false,"result":"Done."}"#),
            ],
        );
        assert_eq!(
            after.events,
            vec![
                ChatEvent::Tool {
                    id: "toolu_01".into(),
                    name: String::new(),
                    detail: String::new(),
                    status: ToolStatus::Done
                },
                ChatEvent::Text {
                    id: "claude-2".into(),
                    delta: "Done.".into()
                },
                ChatEvent::TurnEnd {
                    ok: true,
                    error: None
                },
            ]
        );
    }

    #[test]
    fn denying_sends_a_deny_and_the_tool_shows_declined() {
        let mut c = Claude::default();
        feed(&mut c, p1());
        let step = c.answer("req-1", false).unwrap();
        assert_eq!(
            step.write[0]["response"]["response"],
            json!({"behavior": "deny", "message": DENIED})
        );
        let after = c.read(line(r#"{"type":"user","message":{"role":"user","content":[{"tool_use_id":"toolu_01","type":"tool_result","content":"The user denied this in Seatbelt.","is_error":true}]}}"#));
        assert_eq!(
            after.events,
            vec![ChatEvent::Tool {
                id: "toolu_01".into(),
                name: String::new(),
                detail: String::new(),
                status: ToolStatus::Declined
            }]
        );
    }

    #[test]
    fn a_401_asks_for_sign_in_once_and_interrupts_the_retries() {
        let mut c = Claude::default();
        let retry = r#"{"type":"system","subtype":"api_retry","attempt":1,"max_retries":10,"retry_delay_ms":508,"error_status":401,"error":"authentication_failed"}"#;
        let step = feed(&mut c, vec![line(retry), line(retry)]);
        assert_eq!(step.events.len(), 1);
        assert!(matches!(step.events[0], ChatEvent::SignIn { .. }));
        assert_eq!(
            step.write,
            vec![json!({"type": "control_request",
            "request_id": "seatbelt-interrupt-1", "request": {"subtype": "interrupt"}})]
        );
        let end = c.read(line(
            r#"{"type":"result","subtype":"error_during_execution","is_error":true,"result":""}"#,
        ));
        assert_eq!(
            end.events,
            vec![ChatEvent::TurnEnd {
                ok: false,
                error: Some("Stopped.".into())
            }]
        );
    }

    #[test]
    fn interrupting_denies_what_waits_then_interrupts() {
        let mut c = Claude::default();
        feed(&mut c, p1());
        let step = c.interrupt();
        assert_eq!(step.write.len(), 2);
        assert_eq!(step.write[0]["response"]["request_id"], "req-1");
        assert_eq!(step.write[1]["request"]["subtype"], "interrupt");
        assert_eq!(
            step.events,
            vec![ChatEvent::Resolved { id: "req-1".into() }]
        );
    }

    #[test]
    fn other_control_requests_get_an_error_not_silence() {
        let mut c = Claude::default();
        let step = c.read(line(
            r#"{"type":"control_request","request_id":"r9","request":{"subtype":"hook_callback"}}"#,
        ));
        assert_eq!(step.write[0]["response"]["subtype"], "error");
        assert_eq!(step.write[0]["response"]["request_id"], "r9");
        assert!(step.events.is_empty());
    }

    #[test]
    fn unstreamed_text_arrives_as_a_whole_message() {
        let mut c = Claude::default();
        let step = c.read(line(
            r#"{"type":"assistant","message":{"id":"m","content":[{"type":"text","text":"Hi"}]}}"#,
        ));
        assert_eq!(
            step.events,
            vec![ChatEvent::Message {
                id: "claude-1".into(),
                text: "Hi".into()
            }]
        );
    }

    #[test]
    fn a_message_is_a_user_line_of_stream_json() {
        let mut c = Claude::default();
        assert_eq!(
            c.send("hi").unwrap().write,
            vec![
                json!({"type": "user", "message": {"role": "user", "content": "hi"}, "parent_tool_use_id": null})
            ]
        );
        assert!(c.args().contains(&"--permission-prompt-tool".to_string()));
    }
}
