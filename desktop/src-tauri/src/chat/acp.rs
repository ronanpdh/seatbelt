//! The Agent Client Protocol, as Gemini CLI speaks it (`gemini --acp`): JSON-RPC 2.0 over
//! stdio. The app is the client: it starts a session, sends prompts, and answers the agent's
//! permission requests. It offers no file system or terminal of its own, so the agent uses
//! its own tools. Check P5 in the design ran this against Gemini CLI 0.63.0.

use std::collections::{HashMap, HashSet};

use serde_json::{json, Value};

use super::protocol::{
    about_sign_in, checked, cut, shown, str_of, ChatEvent, Choice, Driver, Model, Models, Step,
    ToolStatus, MAX_TOOL_TEXT,
};

/// A permission request: its JSON-RPC id, the agent's options, and the tool call's id.
type Waiting = (Value, Vec<Value>, String);

#[derive(Default)]
pub struct Acp {
    next_id: u64,
    pending: HashMap<u64, &'static str>,
    cwd: String,
    session: Option<String>,
    queued: Vec<String>,
    /// The assistant message being streamed: a new one after each tool call.
    message: Option<String>,
    messages: u32,
    /// The reasoning being streamed: a new one after each message or tool call.
    thought: Option<String>,
    thoughts: u32,
    /// Permission requests waiting, by our id: the agent's request id, its options, and the
    /// tool call it is about.
    waiting: HashMap<String, Waiting>,
    /// Each tool call's name, from the kind its first update gave, for its approval card.
    kinds: HashMap<String, &'static str>,
    /// Tool calls the user rejected, which the agent then reports as failed.
    declined: HashSet<String>,
    failed: bool,
    /// The session to load, by its ACP id (already checked as a plain id); while it loads,
    /// the agent replays its history, which the window already shows.
    resume: Option<String>,
    loading: bool,
    models: Models,
}

/// The models a session offers, from its `session/new` or `session/load` result, and the one
/// it uses.
fn models_of(result: &Value) -> (Vec<Model>, &str) {
    let list = result
        .pointer("/models/availableModels")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter_map(|m| {
            let id = str_of(m, "modelId");
            (!id.is_empty()).then(|| Model {
                id: id.to_string(),
                name: match str_of(m, "name") {
                    "" => id.to_string(),
                    name => name.to_string(),
                },
                description: str_of(m, "description").to_string(),
                efforts: vec![],
                default_effort: None,
            })
        })
        .collect();
    let current = result
        .pointer("/models/currentModelId")
        .and_then(Value::as_str)
        .unwrap_or_default();
    (list, current)
}

/// What the chat calls a tool of an ACP kind.
fn tool_name(kind: &str) -> &'static str {
    match kind {
        "execute" => "Shell",
        "edit" => "Edit",
        "read" => "Read",
        "delete" => "Delete",
        "move" => "Move",
        "search" => "Search",
        "fetch" => "Fetch",
        "think" => "Think",
        _ => "Tool",
    }
}

fn status(s: &str) -> ToolStatus {
    match s {
        "completed" => ToolStatus::Done,
        "failed" => ToolStatus::Failed,
        _ => ToolStatus::Running,
    }
}

/// The text in a tool call's content: ACP content blocks of type text.
fn content_text(content: Option<&Value>) -> String {
    let parts: Vec<&str> = content
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter_map(|c| c.pointer("/content/text").and_then(Value::as_str))
        .collect();
    cut(&parts.join("\n"), 2000)
}

/// A tool call's content for the window: its text, and each diff as the file's new text.
fn tool_content(content: Option<&Value>) -> String {
    let parts: Vec<String> = content
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter_map(|c| match str_of(c, "type") {
            "diff" => Some(format!("{}\n{}", str_of(c, "path"), str_of(c, "newText"))),
            _ => c
                .pointer("/content/text")
                .and_then(Value::as_str)
                .map(str::to_string),
        })
        .collect();
    cut(&parts.join("\n\n"), MAX_TOOL_TEXT)
}

impl Acp {
    /// A session that loads session `resume` (already checked as a plain id), if any, on the
    /// model `choice`, if it is one Gemini CLI offers.
    pub fn new(resume: Option<&str>, choice: Option<Choice>) -> Self {
        Self {
            resume: resume.map(str::to_string),
            models: Models::wanting(choice),
            ..Self::default()
        }
    }

    fn set_model(&mut self, session: &str, model: &str) -> Value {
        self.request(
            "session/set_model",
            json!({"sessionId": session, "modelId": model}),
        )
    }

    /// The session's models: the one chosen at the start is set before anything is sent.
    fn listed(&mut self, session: &str, result: &Value, step: &mut Step) {
        let (list, current) = models_of(result);
        self.models.using(current);
        if let Some(choice) = self.models.listed(list, step) {
            if self.models.current.as_deref() != Some(choice.model.as_str()) {
                let line = self.set_model(session, &choice.model);
                step.send(line);
            }
            self.models.chosen = Some(choice);
        }
        if !self.models.list.is_empty() {
            step.show(self.models.event());
        }
    }

    fn session_new(&mut self) -> Value {
        let cwd = self.cwd.clone();
        self.request("session/new", json!({"cwd": cwd, "mcpServers": []}))
    }

    /// The session is ready: what was sent before it was goes now.
    fn ready(&mut self, session: String, step: &mut Step) {
        for text in std::mem::take(&mut self.queued) {
            let line = self.prompt(&session, &text);
            step.send(line);
        }
        step.show(ChatEvent::Conversation {
            id: session.clone(),
        });
        self.session = Some(session);
    }

    fn request(&mut self, method: &'static str, params: Value) -> Value {
        self.next_id += 1;
        self.pending.insert(self.next_id, method);
        json!({"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params})
    }

    fn prompt(&mut self, session: &str, text: &str) -> Value {
        self.request(
            "session/prompt",
            json!({"sessionId": session, "prompt": [{"type": "text", "text": text}]}),
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
            let mut message = str_of(error, "message").to_string();
            if let Some(data) = error.get("data").filter(|d| !d.is_null()) {
                message = format!(
                    "{message}: {}",
                    data.as_str()
                        .map_or_else(|| data.to_string(), str::to_string)
                );
            }
            let message = cut(&message, 2000);
            if method == "session/load" {
                // not continued: a new session, and the window is told
                self.loading = false;
                step.show(ChatEvent::Log {
                    text: format!("could not continue the conversation: {message}"),
                });
                step.show(ChatEvent::Resumed { ok: false });
                let new = self.session_new();
                step.send(new);
                return;
            }
            if method == "session/set_model" {
                step.show(ChatEvent::Log {
                    text: format!("Gemini CLI did not change the model: {message}"),
                });
                return;
            }
            if method == "session/new" {
                self.failed = true;
            }
            if method == "session/new" || about_sign_in(&message) {
                step.show(ChatEvent::SignIn {
                    reason: format!("Gemini CLI could not start a session: {message}"),
                });
            }
            if method == "session/prompt" || method == "session/new" {
                self.message = None;
                step.show(ChatEvent::TurnEnd {
                    ok: false,
                    error: Some(message),
                });
            }
            return;
        }
        let result = line.get("result").cloned().unwrap_or_default();
        match method {
            "initialize" => {
                let loads = result.pointer("/agentCapabilities/loadSession") == Some(&json!(true));
                match self.resume.clone() {
                    Some(session) if loads => {
                        let cwd = self.cwd.clone();
                        self.loading = true;
                        let load = self.request(
                            "session/load",
                            json!({"sessionId": session, "cwd": cwd, "mcpServers": []}),
                        );
                        step.send(load);
                    }
                    resume => {
                        if resume.is_some() {
                            step.show(ChatEvent::Resumed { ok: false });
                        }
                        let new = self.session_new();
                        step.send(new);
                    }
                }
            }
            "session/load" => {
                self.loading = false;
                if let Some(session) = self.resume.clone() {
                    step.show(ChatEvent::Resumed { ok: true });
                    self.listed(&session, &result, step);
                    self.ready(session, step);
                }
            }
            "session/new" => {
                if let Some(session) = result.get("sessionId").and_then(Value::as_str) {
                    let session = session.to_string();
                    self.listed(&session, &result, step);
                    self.ready(session, step);
                }
            }
            "session/prompt" => {
                self.message = None;
                for (id, _) in self.waiting.drain() {
                    step.show(ChatEvent::Resolved { id });
                }
                let stop = str_of(&result, "stopReason");
                let ok = matches!(stop, "end_turn" | "max_tokens" | "max_turn_requests");
                let error = (!ok).then(|| {
                    if stop == "cancelled" {
                        "interrupted".to_string()
                    } else {
                        stop.to_string()
                    }
                });
                step.show(ChatEvent::TurnEnd { ok, error });
            }
            _ => {}
        }
    }

    fn update(&mut self, params: &Value, step: &mut Step) {
        let update = params.get("update").cloned().unwrap_or_default();
        match str_of(&update, "sessionUpdate") {
            "agent_thought_chunk" => {
                let text = update.pointer("/content/text").and_then(Value::as_str);
                if let Some(text) = text {
                    let id = match &self.thought {
                        Some(id) => id.clone(),
                        None => {
                            self.thoughts += 1;
                            let id = format!("gemini-thinking-{}", self.thoughts);
                            self.thought = Some(id.clone());
                            id
                        }
                    };
                    step.show(ChatEvent::Thinking {
                        id,
                        delta: text.to_string(),
                    });
                }
            }
            "agent_message_chunk" => {
                self.thought = None;
                let text = update.pointer("/content/text").and_then(Value::as_str);
                if let Some(text) = text {
                    let id = match &self.message {
                        Some(id) => id.clone(),
                        None => {
                            self.messages += 1;
                            let id = format!("gemini-{}", self.messages);
                            self.message = Some(id.clone());
                            id
                        }
                    };
                    step.show(ChatEvent::Text {
                        id,
                        delta: text.to_string(),
                    });
                }
            }
            kind @ ("tool_call" | "tool_call_update") => {
                self.message = None;
                self.thought = None;
                let id = str_of(&update, "toolCallId").to_string();
                let name = if kind == "tool_call" {
                    let name = tool_name(str_of(&update, "kind"));
                    self.kinds.insert(id.clone(), name);
                    name.to_string()
                } else {
                    String::new()
                };
                let mut status = status(str_of(&update, "status"));
                if status == ToolStatus::Failed && self.declined.remove(&id) {
                    status = ToolStatus::Declined;
                }
                step.show(ChatEvent::Tool {
                    id: id.clone(),
                    name,
                    detail: cut(str_of(&update, "title"), 2000),
                    status,
                });
                let content = tool_content(update.get("content"));
                if kind == "tool_call" {
                    // what it was given: its raw input if the agent sends it, else its content
                    let input = match update.get("rawInput") {
                        Some(raw) if !raw.is_null() => shown(raw),
                        _ => content,
                    };
                    if !input.is_empty() {
                        step.show(ChatEvent::ToolInput { id, input });
                    }
                } else if !content.is_empty() {
                    step.show(ChatEvent::ToolOutput {
                        id,
                        output: content,
                    });
                }
            }
            _ => {} // plans, commands and modes: not shown
        }
    }

    fn server_request(&mut self, line: &Value, step: &mut Step) {
        let rpc_id = line.get("id").cloned().unwrap_or_default();
        if str_of(line, "method") != "session/request_permission" {
            // fs/* and terminal/*: the client said it has neither
            step.send(
                json!({"jsonrpc": "2.0", "id": rpc_id, "error": {"code": -32601,
                "message": "Seatbelt does not handle this request"}}),
            );
            return;
        }
        let params = line.get("params").cloned().unwrap_or_default();
        let call = params.get("toolCall").cloned().unwrap_or_default();
        let options = params
            .get("options")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default();
        let id = format!("gemini-{}", rpc_id.to_string().trim_matches('"'));
        let call_id = str_of(&call, "toolCallId").to_string();
        let tool = self
            .kinds
            .get(&call_id)
            .copied()
            .unwrap_or_else(|| tool_name(str_of(&call, "kind")));
        self.waiting.insert(id.clone(), (rpc_id, options, call_id));
        let title = cut(str_of(&call, "title"), 2000);
        let text = content_text(call.get("content"));
        let detail = match (title.is_empty(), text.is_empty()) {
            (false, false) => format!("{title}\n{text}"),
            (false, true) => title,
            _ => text,
        };
        step.show(ChatEvent::Approval {
            id,
            tool: tool.to_string(),
            detail,
        });
    }

    fn outcome(options: &[Value], allow: bool) -> Value {
        let kinds: &[&str] = if allow {
            &["allow_once"]
        } else {
            &["reject_once", "reject_always"]
        };
        let chosen = kinds.iter().find_map(|kind| {
            options
                .iter()
                .find(|o| str_of(o, "kind") == *kind)
                .map(|o| str_of(o, "optionId").to_string())
        });
        match chosen {
            Some(option) => json!({"outcome": "selected", "optionId": option}),
            None => json!({"outcome": "cancelled"}),
        }
    }
}

impl Driver for Acp {
    fn args(&self) -> Vec<String> {
        vec!["--acp".into()]
    }

    fn start(&mut self, cwd: &str) -> Step {
        self.cwd = cwd.to_string();
        let mut step = Step::default();
        let init = self.request(
            "initialize",
            json!({"protocolVersion": 1, "clientCapabilities": {
                "fs": {"readTextFile": false, "writeTextFile": false}, "terminal": false}}),
        );
        step.send(init);
        step
    }

    fn read(&mut self, line: Value) -> Step {
        let mut step = Step::default();
        let has_id = line.get("id").is_some();
        match line.get("method").and_then(Value::as_str) {
            None if has_id => self.response(&line, &mut step),
            Some(_) if has_id => self.server_request(&line, &mut step),
            Some("session/update") if self.loading => {} // the history, replayed as it loads
            Some("session/update") => {
                let params = line.get("params").cloned().unwrap_or_default();
                self.update(&params, &mut step);
            }
            _ => {}
        }
        step
    }

    fn send(&mut self, text: &str) -> Result<Step, String> {
        if self.failed {
            return Err("Gemini CLI has no session: sign in, then start the chat again".into());
        }
        let mut step = Step::default();
        match self.session.clone() {
            Some(session) => {
                let line = self.prompt(&session, text);
                step.send(line);
            }
            None => self.queued.push(text.to_string()),
        }
        Ok(step)
    }

    fn answer(&mut self, id: &str, allow: bool) -> Result<Step, String> {
        let (rpc_id, options, call_id) = self
            .waiting
            .remove(id)
            .ok_or("that request is no longer waiting")?;
        if !allow {
            self.declined.insert(call_id);
        }
        let mut step = Step::default();
        step.send(json!({"jsonrpc": "2.0", "id": rpc_id,
            "result": {"outcome": Self::outcome(&options, allow)}}));
        step.show(ChatEvent::Resolved { id: id.to_string() });
        Ok(step)
    }

    fn interrupt(&mut self) -> Step {
        let mut step = Step::default();
        // the protocol has a waiting permission request answered as cancelled
        let waiting: Vec<(String, Waiting)> = self.waiting.drain().collect();
        for (id, (rpc_id, _, _)) in waiting {
            step.send(json!({"jsonrpc": "2.0", "id": rpc_id, "result": {"outcome": {"outcome": "cancelled"}}}));
            step.show(ChatEvent::Resolved { id });
        }
        if let Some(session) = &self.session {
            step.send(json!({"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": session}}));
        }
        step
    }

    fn choose(&mut self, choice: Choice) -> Result<Step, String> {
        checked(&self.models.list, &choice)?;
        let session = self.session.clone().ok_or("the session has not started")?;
        let mut step = Step::default();
        let line = self.set_model(&session, &choice.model);
        step.send(line);
        self.models.chosen = Some(choice);
        step.show(self.models.event());
        Ok(step)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn line(s: &str) -> Value {
        serde_json::from_str(s).unwrap()
    }

    fn started() -> (Acp, Step) {
        let mut a = Acp::default();
        let mut all = a.start("/work");
        for l in [
            r#"{"jsonrpc":"2.0","id":1,"result":{"protocolVersion":1,"authMethods":[{"id":"oauth-personal","name":"Log in with Google"}]}}"#,
            r#"{"jsonrpc":"2.0","id":2,"result":{"sessionId":"s-1","modes":{"currentModeId":"default"}}}"#,
        ] {
            let step = a.read(line(l));
            all.events.extend(step.events);
            all.write.extend(step.write);
        }
        (a, all)
    }

    #[test]
    fn a_session_starts_with_no_file_system_or_terminal_offered() {
        let (a, step) = started();
        assert_eq!(
            step.write,
            vec![
                json!({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": 1,
                    "clientCapabilities": {"fs": {"readTextFile": false, "writeTextFile": false}, "terminal": false}}}),
                json!({"jsonrpc": "2.0", "id": 2, "method": "session/new", "params": {"cwd": "/work", "mcpServers": []}}),
            ]
        );
        assert_eq!(a.session.as_deref(), Some("s-1"));
    }

    #[test]
    fn a_prompt_with_a_permission_request_runs_as_p5_did() {
        let (mut a, _) = started();
        let sent = a.send("please run it").unwrap();
        assert_eq!(
            sent.write,
            vec![
                json!({"jsonrpc": "2.0", "id": 3, "method": "session/prompt",
            "params": {"sessionId": "s-1", "prompt": [{"type": "text", "text": "please run it"}]}})
            ]
        );
        let ask = [
            r#"{"jsonrpc":"2.0","method":"session/update","params":{"sessionId":"s-1","update":{"sessionUpdate":"tool_call","toolCallId":"run_1","status":"pending","title":"echo hi > out.txt","content":[{"type":"content","content":{"type":"text","text":"[cwd /work]"}}],"kind":"execute"}}}"#,
            r#"{"jsonrpc":"2.0","id":0,"method":"session/request_permission","params":{"sessionId":"s-1","options":[{"optionId":"proceed_always","name":"Allow for this session","kind":"allow_always"},{"optionId":"proceed_once","name":"Allow","kind":"allow_once"},{"optionId":"cancel","name":"Reject","kind":"reject_once"}],"toolCall":{"toolCallId":"run_1","status":"pending","title":"echo hi > out.txt","content":[{"type":"content","content":{"type":"text","text":"[cwd /work]"}}]}}}"#,
        ];
        let mut events = vec![];
        for l in ask {
            events.extend(a.read(line(l)).events);
        }
        assert_eq!(
            events,
            vec![
                ChatEvent::Tool {
                    id: "run_1".into(),
                    name: "Shell".into(),
                    detail: "echo hi > out.txt".into(),
                    status: ToolStatus::Running
                },
                ChatEvent::ToolInput {
                    id: "run_1".into(),
                    input: "[cwd /work]".into(),
                },
                ChatEvent::Approval {
                    id: "gemini-0".into(),
                    tool: "Shell".into(),
                    detail: "echo hi > out.txt\n[cwd /work]".into()
                },
            ]
        );
        assert_eq!(
            a.answer("gemini-0", true).unwrap().write,
            vec![
                json!({"jsonrpc": "2.0", "id": 0, "result": {"outcome": {"outcome": "selected", "optionId": "proceed_once"}}})
            ]
        );
        let rest = [
            r#"{"jsonrpc":"2.0","method":"session/update","params":{"sessionId":"s-1","update":{"sessionUpdate":"tool_call_update","toolCallId":"run_1","status":"completed","title":"echo hi > out.txt","kind":"execute"}}}"#,
            r#"{"jsonrpc":"2.0","method":"session/update","params":{"sessionId":"s-1","update":{"sessionUpdate":"agent_message_chunk","content":{"type":"text","text":"All done."}}}}"#,
            r#"{"jsonrpc":"2.0","id":3,"result":{"stopReason":"end_turn"}}"#,
        ];
        let mut events = vec![];
        for l in rest {
            events.extend(a.read(line(l)).events);
        }
        assert_eq!(
            events,
            vec![
                ChatEvent::Tool {
                    id: "run_1".into(),
                    name: String::new(),
                    detail: "echo hi > out.txt".into(),
                    status: ToolStatus::Done
                },
                ChatEvent::Text {
                    id: "gemini-1".into(),
                    delta: "All done.".into()
                },
                ChatEvent::TurnEnd {
                    ok: true,
                    error: None
                },
            ]
        );
    }

    #[test]
    fn rejecting_cancelling_and_requests_the_client_does_not_offer() {
        let (mut a, _) = started();
        a.read(line(r#"{"jsonrpc":"2.0","id":"p","method":"session/request_permission","params":{"options":[{"optionId":"no","kind":"reject_once"}],"toolCall":{"toolCallId":"rm_1","title":"rm"}}}"#));
        assert_eq!(
            a.answer("gemini-p", false).unwrap().write[0]["result"]["outcome"],
            json!({"outcome": "selected", "optionId": "no"})
        );
        let failed = a.read(line(r#"{"jsonrpc":"2.0","method":"session/update","params":{"update":{"sessionUpdate":"tool_call_update","toolCallId":"rm_1","status":"failed"}}}"#));
        assert!(matches!(
            &failed.events[0],
            ChatEvent::Tool {
                status: ToolStatus::Declined,
                ..
            }
        ));
        a.read(line(r#"{"jsonrpc":"2.0","id":5,"method":"session/request_permission","params":{"options":[],"toolCall":{"title":"x"}}}"#));
        let stop = a.interrupt();
        assert_eq!(
            stop.write,
            vec![
                json!({"jsonrpc": "2.0", "id": 5, "result": {"outcome": {"outcome": "cancelled"}}}),
                json!({"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": "s-1"}}),
            ]
        );
        let fs = a.read(line(r#"{"jsonrpc":"2.0","id":6,"method":"fs/read_text_file","params":{"path":"/etc/passwd"}}"#));
        assert_eq!(fs.write[0]["error"]["code"], -32601);
        assert!(fs.events.is_empty());
    }

    #[test]
    fn thoughts_raw_input_and_tool_output_and_diffs_reach_the_window() {
        let (mut a, _) = started();
        let mut events = vec![];
        for l in [
            r#"{"jsonrpc":"2.0","method":"session/update","params":{"update":{"sessionUpdate":"agent_thought_chunk","content":{"type":"text","text":"Plan "}}}}"#,
            r#"{"jsonrpc":"2.0","method":"session/update","params":{"update":{"sessionUpdate":"agent_thought_chunk","content":{"type":"text","text":"it."}}}}"#,
            r#"{"jsonrpc":"2.0","method":"session/update","params":{"update":{"sessionUpdate":"tool_call","toolCallId":"w1","status":"pending","title":"Write a.txt","kind":"edit","rawInput":{"file_path":"a.txt"}}}}"#,
            r#"{"jsonrpc":"2.0","method":"session/update","params":{"update":{"sessionUpdate":"tool_call_update","toolCallId":"w1","status":"completed","content":[{"type":"diff","path":"a.txt","oldText":null,"newText":"hello"}]}}}"#,
            r#"{"jsonrpc":"2.0","method":"session/update","params":{"update":{"sessionUpdate":"agent_thought_chunk","content":{"type":"text","text":"Again."}}}}"#,
        ] {
            events.extend(a.read(line(l)).events);
        }
        assert_eq!(
            events,
            vec![
                ChatEvent::Thinking {
                    id: "gemini-thinking-1".into(),
                    delta: "Plan ".into()
                },
                ChatEvent::Thinking {
                    id: "gemini-thinking-1".into(),
                    delta: "it.".into()
                },
                ChatEvent::Tool {
                    id: "w1".into(),
                    name: "Edit".into(),
                    detail: "Write a.txt".into(),
                    status: ToolStatus::Running
                },
                ChatEvent::ToolInput {
                    id: "w1".into(),
                    input: "{\n  \"file_path\": \"a.txt\"\n}".into()
                },
                ChatEvent::Tool {
                    id: "w1".into(),
                    name: String::new(),
                    detail: String::new(),
                    status: ToolStatus::Done
                },
                ChatEvent::ToolOutput {
                    id: "w1".into(),
                    output: "a.txt\nhello".into()
                },
                ChatEvent::Thinking {
                    id: "gemini-thinking-2".into(),
                    delta: "Again.".into()
                },
            ]
        );
    }

    #[test]
    fn a_session_is_loaded_without_its_replayed_history_or_a_new_one_starts() {
        let mut a = Acp::new(Some("s-9"), None);
        a.start("/w");
        let load = a.read(line(r#"{"jsonrpc":"2.0","id":1,"result":{"protocolVersion":1,"agentCapabilities":{"loadSession":true}}}"#));
        assert_eq!(
            load.write,
            vec![json!({"jsonrpc": "2.0", "id": 2, "method": "session/load",
                "params": {"sessionId": "s-9", "cwd": "/w", "mcpServers": []}})]
        );
        let replay = a.read(line(r#"{"jsonrpc":"2.0","method":"session/update","params":{"update":{"sessionUpdate":"agent_message_chunk","content":{"type":"text","text":"old"}}}}"#));
        assert!(replay.events.is_empty());
        let loaded = a.read(line(r#"{"jsonrpc":"2.0","id":2,"result":null}"#));
        assert_eq!(
            loaded.events,
            vec![
                ChatEvent::Resumed { ok: true },
                ChatEvent::Conversation { id: "s-9".into() }
            ]
        );
        let after = a.read(line(r#"{"jsonrpc":"2.0","method":"session/update","params":{"update":{"sessionUpdate":"agent_message_chunk","content":{"type":"text","text":"new"}}}}"#));
        assert_eq!(after.events.len(), 1);

        let mut b = Acp::new(Some("s-9"), None);
        b.start("/w");
        b.read(line(r#"{"jsonrpc":"2.0","id":1,"result":{"protocolVersion":1,"agentCapabilities":{"loadSession":true}}}"#));
        let failed = b.read(line(
            r#"{"jsonrpc":"2.0","id":2,"error":{"code":-32603,"message":"no such session"}}"#,
        ));
        assert!(failed.events.contains(&ChatEvent::Resumed { ok: false }));
        assert_eq!(failed.write[0]["method"], "session/new");

        let mut c = Acp::new(Some("s-9"), None);
        c.start("/w");
        let unsupported = c.read(line(
            r#"{"jsonrpc":"2.0","id":1,"result":{"protocolVersion":1}}"#,
        ));
        assert_eq!(unsupported.events, vec![ChatEvent::Resumed { ok: false }]);
        assert_eq!(unsupported.write[0]["method"], "session/new");
    }

    /// `session/new` as Gemini CLI 0.63.0 answered it, cut to the fields that matter.
    const NEW: &str = r#"{"jsonrpc":"2.0","id":2,"result":{"sessionId":"s-1",
        "modes":{"availableModes":[{"id":"default","name":"Default"}],"currentModeId":"default"},
        "models":{"availableModels":[{"modelId":"auto","name":"Auto","description":"Let Gemini CLI decide"},
            {"modelId":"gemini-2.5-pro","name":"gemini-2.5-pro"}],"currentModelId":"auto"}}}"#;

    fn pick(model: &str) -> Choice {
        Choice {
            model: model.into(),
            effort: None,
        }
    }

    #[test]
    fn the_sessions_models_are_shown_and_a_choice_is_set_on_it() {
        let mut a = Acp::default();
        a.start("/w");
        a.read(line(
            r#"{"jsonrpc":"2.0","id":1,"result":{"protocolVersion":1}}"#,
        ));
        let new = a.read(line(NEW));
        let ChatEvent::Models {
            models,
            model,
            effort,
            current,
        } = &new.events[0]
        else {
            panic!("{:?}", new.events);
        };
        assert_eq!(models.len(), 2);
        assert_eq!(models[0].description, "Let Gemini CLI decide");
        assert!(models[1].efforts.is_empty()); // Gemini CLI offers no effort to choose
        assert_eq!(
            (model.as_deref(), effort, current.as_deref()),
            (Some("auto"), &None, Some("auto"))
        );
        assert!(a.choose(pick("gemini-9")).is_err());
        assert!(a
            .choose(Choice {
                model: "auto".into(),
                effort: Some("high".into())
            })
            .is_err());
        let chosen = a.choose(pick("gemini-2.5-pro")).unwrap();
        assert_eq!(
            chosen.write,
            vec![
                json!({"jsonrpc": "2.0", "id": 3, "method": "session/set_model",
                "params": {"sessionId": "s-1", "modelId": "gemini-2.5-pro"}})
            ]
        );
        assert!(matches!(&chosen.events[..],
            [ChatEvent::Models { model: Some(m), .. }] if m == "gemini-2.5-pro"));
        let refused = a.read(line(
            r#"{"jsonrpc":"2.0","id":3,"error":{"code":-32603,"message":"no such model"}}"#,
        ));
        assert!(
            matches!(&refused.events[..], [ChatEvent::Log { text }] if text.contains("no such model"))
        );
    }

    #[test]
    fn a_choice_from_before_is_set_before_the_first_prompt() {
        let mut a = Acp::new(None, Some(pick("gemini-2.5-pro")));
        a.start("/w");
        a.read(line(
            r#"{"jsonrpc":"2.0","id":1,"result":{"protocolVersion":1}}"#,
        ));
        assert!(a.send("hi").unwrap().write.is_empty());
        let step = a.read(line(NEW));
        assert_eq!(
            step.write
                .iter()
                .map(|l| l["method"].as_str().unwrap())
                .collect::<Vec<_>>(),
            ["session/set_model", "session/prompt"]
        );
        // what it already uses is not set again
        let mut same = Acp::new(None, Some(pick("auto")));
        same.start("/w");
        same.read(line(
            r#"{"jsonrpc":"2.0","id":1,"result":{"protocolVersion":1}}"#,
        ));
        assert!(same.read(line(NEW)).write.is_empty());
        // one it no longer offers is left out, and the log says so
        let mut gone = Acp::new(None, Some(pick("gemini-1.0")));
        gone.start("/w");
        gone.read(line(
            r#"{"jsonrpc":"2.0","id":1,"result":{"protocolVersion":1}}"#,
        ));
        gone.send("hi").unwrap();
        let step = gone.read(line(NEW));
        assert!(matches!(&step.events[0], ChatEvent::Log { text } if text.contains("gemini-1.0")));
        assert_eq!(
            step.write
                .iter()
                .map(|l| l["method"].as_str().unwrap())
                .collect::<Vec<_>>(),
            ["session/prompt"]
        );
    }

    #[test]
    fn no_session_asks_for_sign_in() {
        let mut a = Acp::default();
        a.start("/w");
        a.read(line(
            r#"{"jsonrpc":"2.0","id":1,"result":{"protocolVersion":1}}"#,
        ));
        let step = a.read(line(r#"{"jsonrpc":"2.0","id":2,"error":{"code":-32000,"message":"Authentication required"}}"#));
        assert!(matches!(step.events[0], ChatEvent::SignIn { .. }));
        assert!(a.send("hi").is_err());
    }
}
