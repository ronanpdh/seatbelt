//! Claude Code's headless session: `-p` with stream-json in and out, permission prompts
//! over its control protocol (`--permission-prompt-tool stdio`, as the Agent SDK starts it).
//! Checks P1 to P3 in the design ran this against Claude Code 2.1.295.

use std::collections::{HashMap, HashSet};

use serde_json::{json, Value};

use super::protocol::{
    checked, content_text, cut, shown, str_of, summary, ChatEvent, Choice, Driver, Mode, Model,
    Models, Modes, Step, ToolStatus,
};

/// The message a denied tool use gets, which Claude reads.
const DENIED: &str = "The user denied this in Seatbelt.";

#[derive(Default)]
pub struct Claude {
    /// The assistant message being streamed, by our own id.
    message: Option<String>,
    streamed: bool,
    messages: u32,
    /// The reasoning being streamed in this message, by our own id.
    thinking: Option<String>,
    thinking_streamed: bool,
    thoughts: u32,
    /// Approvals waiting: request id, then the tool's input and its tool use id.
    waiting: HashMap<String, (Value, String)>,
    declined: HashSet<String>,
    interrupts: u32,
    /// This turn was interrupted, by the user or after a sign-in failure.
    interrupted: bool,
    /// Sign-in failure seen this turn: said once, and the turn is interrupted, not retried.
    signin_said: bool,
    /// The conversation to resume, and the one this session is in, by Claude Code's own id.
    resume: Option<String>,
    conversation: Option<String>,
    /// A message has been sent: a result before one is Claude Code refusing to resume.
    sent: bool,
    models: Models,
    /// Control requests this app sent, by number: what each asked.
    requests: u32,
    asked: HashMap<String, Asked>,
    /// Messages held until the model and mode chosen at the start are set.
    queued: Vec<String>,
    modes: Modes,
    /// The plan each ExitPlanMode call gave, by its tool use id, for its approval card: the
    /// approval request itself comes with no input.
    plans: HashMap<String, String>,
}

/// A control request this app sent, for its response.
#[derive(Clone, Debug, PartialEq)]
enum Asked {
    Initialize,
    Settings,
    Model,
    Effort,
    /// A permission mode, and the one before it, to go back to if it is refused.
    Mode(Option<String>),
}

/// The permission modes offered: Claude Code's, but `bypassPermissions`, which skips every
/// approval. Named and described as Claude Code's documentation names them.
fn modes() -> Vec<Mode> {
    vec![
        Mode::new("default", "Manual", "Runs without asking: reads only."),
        Mode::new(
            "acceptEdits",
            "Accept edits",
            "Runs without asking: reads, file edits, and common filesystem commands (mkdir, touch, mv, cp).",
        ),
        Mode::new(
            "plan",
            "Plan",
            "Researches and proposes changes without making them: edits stay blocked until you approve the plan.",
        ),
        Mode::new(
            "auto",
            "Auto",
            "Runs without asking: everything, with background safety checks by a classifier.",
        ),
        Mode::new(
            "dontAsk",
            "Don't ask",
            "Runs without asking: reads and pre-approved tools; anything that would ask is denied.",
        ),
    ]
}

/// Claude Code's models, from its answer to `initialize`.
fn models_of(answer: &Value) -> Vec<Model> {
    answer
        .get("models")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter_map(|m| {
            let id = str_of(m, "value");
            (!id.is_empty()).then(|| Model {
                id: id.to_string(),
                name: match str_of(m, "displayName") {
                    "" => id.to_string(),
                    name => name.to_string(),
                },
                description: str_of(m, "description").to_string(),
                efforts: if m.get("supportsEffort").and_then(Value::as_bool) == Some(true) {
                    m.get("supportedEffortLevels")
                        .and_then(Value::as_array)
                        .into_iter()
                        .flatten()
                        .filter_map(|e| e.as_str().map(str::to_string))
                        .collect()
                } else {
                    vec![]
                },
                default_effort: None,
            })
        })
        .collect()
}

impl Claude {
    /// A session that resumes conversation `resume` (already checked as a plain id), if any,
    /// on the model and effort `choice` and the permission mode `mode`, if they are offered.
    pub fn new(resume: Option<&str>, choice: Option<Choice>, mode: Option<String>) -> Self {
        Self {
            resume: resume.map(str::to_string),
            models: Models::wanting(choice),
            modes: Modes::wanting(mode),
            ..Self::default()
        }
    }

    /// Set permission mode `mode`; the window shows it at once, and goes back if refused.
    fn set_mode_line(&mut self, mode: &str, step: &mut Step) {
        let before = self.modes.current.replace(mode.to_string());
        let line = self.ask(
            Asked::Mode(before),
            json!({"subtype": "set_permission_mode", "mode": mode}),
        );
        step.send(line);
    }

    fn ask(&mut self, asked: Asked, request: Value) -> Value {
        self.requests += 1;
        let id = format!("seatbelt-{}", self.requests);
        self.asked.insert(id.clone(), asked);
        json!({"type": "control_request", "request_id": id, "request": request})
    }

    fn user_line(text: &str) -> Value {
        json!({
            "type": "user",
            "message": {"role": "user", "content": text},
            "parent_tool_use_id": null,
        })
    }

    /// Set the model and effort, as far as they differ from what was chosen before; the
    /// effort is reset to the model's own default with null.
    fn apply(&mut self, choice: Choice, step: &mut Step) {
        let before = self.models.chosen.take();
        if before.as_ref().map(|c| c.model.as_str()) != Some(choice.model.as_str()) {
            let line = self.ask(
                Asked::Model,
                json!({"subtype": "set_model", "model": choice.model}),
            );
            step.send(line);
        }
        if before.and_then(|c| c.effort) != choice.effort {
            let line = self.ask(
                Asked::Effort,
                json!({"subtype": "apply_flag_settings", "settings": {"effortLevel": choice.effort}}),
            );
            step.send(line);
        }
        self.models.chosen = Some(choice);
    }

    /// Claude Code's answer to a control request this app sent.
    fn answered(&mut self, line: &Value, step: &mut Step) {
        let response = line.get("response").cloned().unwrap_or_default();
        let Some(asked) = self.asked.remove(str_of(&response, "request_id")) else {
            return;
        };
        let answer = response.get("response").cloned().unwrap_or_default();
        let failed = str_of(&response, "subtype") == "error";
        match asked {
            Asked::Initialize => {
                let list = if failed { vec![] } else { models_of(&answer) };
                if let Some(choice) = self.models.listed(list, step) {
                    self.apply(choice, step);
                }
                self.modes.using(str_of(&answer, "current_permission_mode"));
                if let Some(mode) = self.modes.listed(modes(), step) {
                    self.set_mode_line(&mode, step);
                }
                for text in std::mem::take(&mut self.queued) {
                    step.send(Self::user_line(&text));
                }
                if !self.models.list.is_empty() {
                    step.show(self.models.event());
                }
                step.show(self.modes.event());
            }
            Asked::Mode(before) if failed => {
                // refused: the window shows the mode Claude Code is still in, and says why
                self.modes.current = before;
                step.show(ChatEvent::Notice {
                    text: format!(
                        "Claude Code did not change the permission mode: {}",
                        cut(str_of(&response, "error"), 2000)
                    ),
                });
                step.show(self.modes.event());
            }
            Asked::Settings if !failed => {
                let using = answer
                    .pointer("/applied/model")
                    .and_then(Value::as_str)
                    .unwrap_or_default();
                if self.models.using(using) && self.models.known {
                    step.show(self.models.event());
                }
            }
            Asked::Model | Asked::Effort if failed => step.show(ChatEvent::Log {
                text: format!(
                    "Claude Code did not change the {}: {}",
                    if asked == Asked::Model {
                        "model"
                    } else {
                        "effort"
                    },
                    cut(str_of(&response, "error"), 2000)
                ),
            }),
            _ => {}
        }
    }

    fn next_message(&mut self) -> String {
        self.messages += 1;
        self.streamed = false;
        let id = format!("claude-{}", self.messages);
        self.message = Some(id.clone());
        id
    }

    fn next_thought(&mut self) -> String {
        self.thoughts += 1;
        let id = format!("claude-thinking-{}", self.thoughts);
        self.thinking = Some(id.clone());
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
        // leaving plan mode asks with no input: the card shows the plan the call gave, if any
        let detail = if str_of(&request, "tool_name") == "ExitPlanMode" {
            let said = "Allow approves the plan: Claude Code leaves plan mode and starts on it.";
            match self.plans.remove(&tool_use) {
                Some(plan) if detail.is_empty() => format!("{plan}\n\n{said}"),
                _ if detail.is_empty() => said.to_string(),
                _ => detail,
            }
        } else {
            detail
        };
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
        .into_iter()
        .chain(
            self.resume
                .iter()
                .flat_map(|id| ["--resume".to_string(), id.clone()]),
        )
        .collect()
    }

    fn start(&mut self, _cwd: &str) -> Step {
        // the conversation starts with the first message; these ask for the models it offers
        // and the one it is set to use
        let mut step = Step::default();
        let init = self.ask(Asked::Initialize, json!({"subtype": "initialize"}));
        step.send(init);
        let settings = self.ask(Asked::Settings, json!({"subtype": "get_settings"}));
        step.send(settings);
        step
    }

    fn read(&mut self, line: Value) -> Step {
        let mut step = Step::default();
        // the permission mode it is in, said as each turn starts and when it changes (as when
        // a plan is approved): the window follows it
        if str_of(&line, "type") == "system"
            && self.modes.known
            && self.modes.using(str_of(&line, "permissionMode"))
        {
            step.show(self.modes.event());
        }
        // a subagent's own messages: its tools show, its words stay inside it
        let subagent = line.get("parent_tool_use_id").is_some_and(|p| !p.is_null());
        match str_of(&line, "type") {
            "stream_event" if subagent => {}
            "stream_event" => {
                let event = line.get("event").cloned().unwrap_or_default();
                match str_of(&event, "type") {
                    "message_start" => {
                        self.next_message();
                        self.thinking = None;
                        self.thinking_streamed = false;
                    }
                    "content_block_start"
                        if event.pointer("/content_block/type").and_then(Value::as_str)
                            == Some("thinking") =>
                    {
                        self.next_thought();
                    }
                    "content_block_delta" => {
                        let delta = event.get("delta").cloned().unwrap_or_default();
                        if str_of(&delta, "type") == "thinking_delta" {
                            let id = match &self.thinking {
                                Some(id) => id.clone(),
                                None => self.next_thought(),
                            };
                            self.thinking_streamed = true;
                            step.show(ChatEvent::Thinking {
                                id,
                                delta: str_of(&delta, "thinking").to_string(),
                            });
                        } else if str_of(&delta, "type") == "text_delta" {
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
                        "thinking" if !self.thinking_streamed && !subagent => {
                            let id = self.next_thought();
                            step.show(ChatEvent::Thinking {
                                id,
                                delta: str_of(block, "thinking").to_string(),
                            });
                        }
                        "text" if !self.streamed && !subagent => {
                            let id = self.next_message();
                            step.show(ChatEvent::Message {
                                id,
                                text: str_of(block, "text").to_string(),
                            });
                        }
                        "tool_use" => {
                            let id = str_of(block, "id").to_string();
                            let input = block.get("input").unwrap_or(&Value::Null);
                            if str_of(block, "name") == "ExitPlanMode" {
                                let plan = str_of(input, "plan");
                                if !plan.is_empty() {
                                    self.plans.insert(id.clone(), cut(plan, 4000));
                                }
                            }
                            step.show(ChatEvent::Tool {
                                id: id.clone(),
                                name: str_of(block, "name").to_string(),
                                detail: summary(input),
                                status: ToolStatus::Running,
                            });
                            step.show(ChatEvent::ToolInput {
                                id,
                                input: shown(input),
                            });
                        }
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
                    step.show(ChatEvent::ToolOutput {
                        id: id.clone(),
                        output: content_text(block.get("content").unwrap_or(&Value::Null)),
                    });
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
                if !subagent {
                    self.message = None;
                    self.streamed = false;
                }
            }
            "control_request" => self.control(&line, &mut step),
            "control_response" => self.answered(&line, &mut step),
            "system" if str_of(&line, "subtype") == "init" => {
                let id = str_of(&line, "session_id");
                if !id.is_empty() && self.conversation.as_deref() != Some(id) {
                    self.conversation = Some(id.to_string());
                    step.show(ChatEvent::Conversation { id: id.to_string() });
                }
                if self.models.using(str_of(&line, "model")) && self.models.known {
                    step.show(self.models.event());
                }
            }
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
            "result" if !self.sent && self.resume.is_some() => {
                // "No conversation found": the process ends; the window starts a new one
                self.resume = None;
                step.show(ChatEvent::Resumed { ok: false });
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
        self.sent = true;
        if self.models.holds() || self.modes.holds() {
            self.queued.push(text.to_string());
        } else {
            step.send(Self::user_line(text));
        }
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

    fn choose(&mut self, choice: Choice) -> Result<Step, String> {
        checked(&self.models.list, &choice)?;
        let mut step = Step::default();
        self.apply(choice, &mut step);
        step.show(self.models.event());
        Ok(step)
    }

    fn set_mode(&mut self, mode: &str) -> Result<Step, String> {
        self.modes.offered(mode)?;
        let mut step = Step::default();
        self.set_mode_line(mode, &mut step);
        step.show(self.modes.event());
        Ok(step)
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
                ChatEvent::ToolInput {
                    id: "toolu_01".into(),
                    input: "{\n  \"command\": \"echo hi > out.txt\",\n  \"description\": \"Write a file\"\n}"
                        .into(),
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
                ChatEvent::ToolOutput {
                    id: "toolu_01".into(),
                    output: "(Bash completed with no output)".into(),
                },
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
            vec![
                ChatEvent::ToolOutput {
                    id: "toolu_01".into(),
                    output: DENIED.into(),
                },
                ChatEvent::Tool {
                    id: "toolu_01".into(),
                    name: String::new(),
                    detail: String::new(),
                    status: ToolStatus::Declined
                }
            ]
        );
    }

    #[test]
    fn thinking_streams_or_comes_whole_and_a_subagents_words_stay_inside_it() {
        let mut c = Claude::default();
        let step = feed(
            &mut c,
            vec![
                line(
                    r#"{"type":"stream_event","event":{"type":"message_start","message":{"id":"m"}}}"#,
                ),
                line(
                    r#"{"type":"stream_event","event":{"type":"content_block_start","index":0,"content_block":{"type":"thinking","thinking":""}}}"#,
                ),
                line(
                    r#"{"type":"stream_event","event":{"type":"content_block_delta","index":0,"delta":{"type":"thinking_delta","thinking":"Look at "}}}"#,
                ),
                line(
                    r#"{"type":"stream_event","event":{"type":"content_block_delta","index":0,"delta":{"type":"thinking_delta","thinking":"the test."}}}"#,
                ),
                line(
                    r#"{"type":"assistant","message":{"id":"m","content":[{"type":"thinking","thinking":"Look at the test.","signature":"s"}]}}"#,
                ),
                line(
                    r#"{"type":"assistant","parent_tool_use_id":"task_1","message":{"id":"sub","content":[{"type":"text","text":"subagent chatter"}]}}"#,
                ),
                line(
                    r#"{"type":"assistant","message":{"id":"n","content":[{"type":"thinking","thinking":"Whole."}]}}"#,
                ),
            ],
        );
        assert_eq!(
            step.events,
            vec![
                ChatEvent::Thinking {
                    id: "claude-thinking-1".into(),
                    delta: "Look at ".into()
                },
                ChatEvent::Thinking {
                    id: "claude-thinking-1".into(),
                    delta: "the test.".into()
                },
            ]
        );
        let whole = feed(
            &mut c,
            vec![
                line(
                    r#"{"type":"stream_event","event":{"type":"message_start","message":{"id":"n"}}}"#,
                ),
                line(
                    r#"{"type":"assistant","message":{"id":"n","content":[{"type":"thinking","thinking":"Whole."}]}}"#,
                ),
            ],
        );
        assert_eq!(
            whole.events,
            vec![ChatEvent::Thinking {
                id: "claude-thinking-2".into(),
                delta: "Whole.".into()
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
    fn a_conversation_is_resumed_by_its_id_and_a_refusal_is_said_once() {
        let mut c = Claude::new(Some("s-1"), None, None);
        assert_eq!(c.args()[c.args().len() - 2..], ["--resume", "s-1"]);
        assert!(!Claude::default().args().contains(&"--resume".to_string()));
        let gone = c.read(line(r#"{"type":"result","subtype":"error_during_execution","is_error":true,"session_id":"s-1"}"#));
        assert_eq!(gone.events, vec![ChatEvent::Resumed { ok: false }]);
        let mut c = Claude::new(Some("s-1"), None, None);
        c.send("hi").unwrap();
        let init = feed(
            &mut c,
            vec![
                line(r#"{"type":"system","subtype":"init","session_id":"s-1"}"#),
                line(r#"{"type":"system","subtype":"init","session_id":"s-1"}"#),
            ],
        );
        assert_eq!(
            init.events,
            vec![ChatEvent::Conversation { id: "s-1".into() }]
        );
    }

    /// Claude Code 2.1.295's answer to `initialize`, cut to the fields that matter.
    const MODELS: &str = r#"{"type":"control_response","response":{"subtype":"success","request_id":"seatbelt-1","response":{"models":[
        {"value":"default","displayName":"Default (recommended)","description":"Opus 5.5","supportsEffort":true,"supportedEffortLevels":["low","medium","high","xhigh","max"]},
        {"value":"haiku","displayName":"Haiku","description":"Fastest","supportsEffort":true,"supportedEffortLevels":["low","medium","high","xhigh","max"]},
        {"value":"old","displayName":"Old","supportsEffort":false}
        ],"current_permission_mode":"default"}}}"#;

    fn pick(model: &str, effort: Option<&str>) -> Choice {
        Choice {
            model: model.into(),
            effort: effort.map(str::to_string),
        }
    }

    #[test]
    fn the_models_are_asked_for_and_a_choice_is_set_by_control_requests() {
        let mut c = Claude::default();
        let start = c.start("/w");
        assert_eq!(
            start.write,
            vec![
                json!({"type": "control_request", "request_id": "seatbelt-1", "request": {"subtype": "initialize"}}),
                json!({"type": "control_request", "request_id": "seatbelt-2", "request": {"subtype": "get_settings"}}),
            ]
        );
        assert!(c.choose(pick("haiku", None)).is_err()); // not before the list is known
        let listed = c.read(line(MODELS));
        let [ChatEvent::Models {
            models,
            model: None,
            effort: None,
            current: None,
        }, ChatEvent::Modes { .. }] = &listed.events[..]
        else {
            panic!("{:?}", listed.events);
        };
        assert_eq!(models.len(), 3);
        assert_eq!(models[0].name, "Default (recommended)");
        assert_eq!(models[1].efforts.len(), 5);
        assert!(models[2].efforts.is_empty()); // no effort to choose
        let settings = c.read(line(r#"{"type":"control_response","response":{"subtype":"success","request_id":"seatbelt-2","response":{"effective":{},"applied":{"model":"claude-opus-5-5","effort":"medium"}}}}"#));
        assert!(matches!(&settings.events[..],
            [ChatEvent::Models { current: Some(m), model: None, .. }] if m == "claude-opus-5-5"));

        assert!(c.choose(pick("sonnet-9", None)).is_err());
        assert!(c.choose(pick("old", Some("low"))).is_err());
        let chosen = c.choose(pick("haiku", Some("low"))).unwrap();
        assert_eq!(
            chosen.write,
            vec![
                json!({"type": "control_request", "request_id": "seatbelt-3",
                    "request": {"subtype": "set_model", "model": "haiku"}}),
                json!({"type": "control_request", "request_id": "seatbelt-4",
                    "request": {"subtype": "apply_flag_settings", "settings": {"effortLevel": "low"}}}),
            ]
        );
        assert!(matches!(&chosen.events[..],
            [ChatEvent::Models { model: Some(m), effort: Some(e), .. }] if m == "haiku" && e == "low"));
        // back to the model's own effort: null, which Claude Code takes as its default
        let reset = c.choose(pick("haiku", None)).unwrap();
        assert_eq!(
            reset.write,
            vec![
                json!({"type": "control_request", "request_id": "seatbelt-5",
                "request": {"subtype": "apply_flag_settings", "settings": {"effortLevel": null}}})
            ]
        );
        let refused = c.read(line(r#"{"type":"control_response","response":{"subtype":"error","request_id":"seatbelt-5","error":"no"}}"#));
        assert!(
            matches!(&refused.events[..], [ChatEvent::Log { text }] if text.contains("effort: no"))
        );
        let init = c.read(line(
            r#"{"type":"system","subtype":"init","session_id":"s","model":"claude-haiku-5-5"}"#,
        ));
        assert!(matches!(&init.events[1],
            ChatEvent::Models { current: Some(m), .. } if m == "claude-haiku-5-5"));
    }

    #[test]
    fn a_choice_from_before_is_set_before_the_first_message() {
        let mut c = Claude::new(None, Some(pick("haiku", Some("high"))), None);
        c.start("/w");
        assert!(c.send("hi").unwrap().write.is_empty()); // held until the model is set
        let step = c.read(line(MODELS));
        let asked: Vec<&Value> = step
            .write
            .iter()
            .map(|l| l.get("request").unwrap_or(&l["message"]))
            .collect();
        assert_eq!(
            asked,
            [
                &json!({"subtype": "set_model", "model": "haiku"}),
                &json!({"subtype": "apply_flag_settings", "settings": {"effortLevel": "high"}}),
                &json!({"role": "user", "content": "hi"}),
            ]
        );

        let mut gone = Claude::new(None, Some(pick("opus-3", None)), None);
        gone.start("/w");
        gone.send("hi").unwrap();
        let step = gone.read(line(MODELS));
        assert!(matches!(&step.events[0], ChatEvent::Log { text } if text.contains("opus-3")));
        assert_eq!(step.write.len(), 1);
        assert_eq!(step.write[0]["message"]["content"], "hi");
    }

    #[test]
    fn a_permission_mode_is_set_followed_and_a_refusal_said() {
        let mut c = Claude::default();
        c.start("/w");
        let listed = c.read(line(MODELS));
        let Some(ChatEvent::Modes {
            modes,
            mode,
            current,
        }) = listed.events.last()
        else {
            panic!("{:?}", listed.events);
        };
        assert_eq!(
            modes.iter().map(|m| m.id.as_str()).collect::<Vec<_>>(),
            ["default", "acceptEdits", "plan", "auto", "dontAsk"]
        );
        assert_eq!(
            (mode.as_deref(), current.as_deref()),
            (Some("default"), Some("default"))
        );
        // the mode that skips every approval is not offered, whatever is asked
        assert!(c.set_mode("bypassPermissions").is_err());
        let plan = c.set_mode("plan").unwrap();
        assert_eq!(
            plan.write,
            vec![
                json!({"type": "control_request", "request_id": "seatbelt-3",
                "request": {"subtype": "set_permission_mode", "mode": "plan"}})
            ]
        );
        assert!(
            matches!(&plan.events[..], [ChatEvent::Modes { mode: Some(m), .. }] if m == "plan")
        );
        // Claude Code says so; that is no news
        let said = c.read(line(
            r#"{"type":"system","subtype":"status","status":null,"permissionMode":"plan"}"#,
        ));
        assert!(said.events.is_empty());
        let refused = c.read(line(r#"{"type":"control_response","response":{"subtype":"error","request_id":"seatbelt-3","error":"not now"}}"#));
        assert!(
            matches!(&refused.events[0], ChatEvent::Notice { text } if text.contains("not now"))
        );
        assert!(
            matches!(&refused.events[1], ChatEvent::Modes { mode: Some(m), .. } if m == "default")
        );
        // a plan approved: Claude Code leaves plan mode, and the window follows
        c.set_mode("plan").unwrap();
        let ask = c.read(line(r#"{"type":"control_request","request_id":"r5","request":{"subtype":"can_use_tool","tool_name":"ExitPlanMode","display_name":"ExitPlanMode","input":{},"tool_use_id":"toolu_plan","requires_user_interaction":true}}"#));
        assert!(matches!(&ask.events[..],
            [ChatEvent::Approval { tool, detail, .. }] if tool == "ExitPlanMode" && detail.starts_with("Allow approves the plan")));
        // with the plan its call gave, as P9's did
        c.read(line(r#"{"type":"assistant","message":{"id":"m","content":[{"type":"tool_use","id":"toolu_p2","name":"ExitPlanMode","input":{"plan":"1. Write out.txt\n2. Check it"}}]}}"#));
        let ask = c.read(line(r#"{"type":"control_request","request_id":"r6","request":{"subtype":"can_use_tool","tool_name":"ExitPlanMode","input":{},"tool_use_id":"toolu_p2"}}"#));
        assert!(matches!(&ask.events[..],
            [ChatEvent::Approval { detail, .. }] if detail.starts_with("1. Write out.txt\n2. Check it\n\nAllow approves")));
        let left = c.read(line(
            r#"{"type":"system","subtype":"status","status":null,"permissionMode":"default"}"#,
        ));
        assert!(
            matches!(&left.events[..], [ChatEvent::Modes { mode: Some(m), .. }] if m == "default")
        );
    }

    #[test]
    fn a_mode_from_before_is_set_before_the_first_message_if_offered() {
        let mut c = Claude::new(None, None, Some("acceptEdits".into()));
        c.start("/w");
        assert!(c.send("hi").unwrap().write.is_empty()); // held until the mode is set
        let step = c.read(line(MODELS));
        assert_eq!(
            step.write[0]["request"],
            json!({"subtype": "set_permission_mode", "mode": "acceptEdits"})
        );
        assert_eq!(step.write[1]["message"]["content"], "hi");

        let mut never = Claude::new(None, None, Some("bypassPermissions".into()));
        never.start("/w");
        never.send("hi").unwrap();
        let step = never.read(line(MODELS));
        assert!(
            matches!(&step.events[0], ChatEvent::Log { text } if text.contains("bypassPermissions"))
        );
        assert_eq!(step.write.len(), 1);
        assert_eq!(step.write[0]["message"]["content"], "hi");
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
