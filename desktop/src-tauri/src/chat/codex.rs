//! Codex's `app-server`: JSON-RPC 2.0 over stdio, one JSON object per line, without the
//! `"jsonrpc"` member. A thread is the chat; each message is a turn; approvals are requests
//! from the server. Check P4 in the design ran this against Codex 0.162.0; the message shapes
//! are those `codex app-server generate-ts` gives for that release.

use std::collections::HashMap;

use serde_json::{json, Value};

use super::protocol::{
    about_sign_in, checked, content_text, cut, shown, str_of, ChatEvent, Choice, Driver, Mode,
    Model, Models, Modes, Step, ToolStatus, MAX_TOOL_TEXT,
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
    /// The thread to resume, by Codex's own id (already checked as a plain id).
    resume: Option<String>,
    /// The model and effort are given with each turn, once one is chosen.
    models: Models,
    /// So is the permission mode: an approval policy and a sandbox.
    modes: Modes,
    mode_chosen: Option<String>,
    /// The thread's own sandbox as it started, kept for the preset of its kind.
    start_sandbox: Option<Value>,
}

/// The permission modes offered: two of the presets Codex's documentation names, both asking
/// before going outside the sandbox. Its full access, with no sandbox and no approvals, is not.
fn modes() -> Vec<Mode> {
    vec![
        Mode::new(
            "auto",
            "Auto",
            "Workspace-write sandbox; asks before going outside it (on-request approvals).",
        ),
        Mode::new(
            "read-only",
            "Read-only",
            "Read-only sandbox; asks before going outside it (on-request approvals).",
        ),
    ]
}

/// The mode a thread is in, from its approval policy and sandbox: a preset's id, or else
/// both as Codex names them.
fn mode_of(result: &Value) -> String {
    let approval = match result.get("approvalPolicy") {
        Some(Value::String(s)) => s.clone(),
        Some(Value::Object(o)) => o.keys().next().cloned().unwrap_or_default(),
        _ => String::new(),
    };
    let sandbox = result
        .pointer("/sandbox/type")
        .and_then(Value::as_str)
        .unwrap_or_default();
    match (approval.as_str(), sandbox) {
        ("on-request", "workspaceWrite") => "auto".into(),
        ("on-request", "readOnly") => "read-only".into(),
        ("", "") => String::new(),
        _ => format!("{approval}, {sandbox}"),
    }
}

/// Codex's models, from `model/list`: those its own picker shows.
fn models_of(result: &Value) -> Vec<Model> {
    result
        .get("data")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter(|m| m.get("hidden").and_then(Value::as_bool) != Some(true))
        .filter_map(|m| {
            let id = match str_of(m, "model") {
                "" => str_of(m, "id"),
                model => model,
            };
            (!id.is_empty()).then(|| Model {
                id: id.to_string(),
                name: match str_of(m, "displayName") {
                    "" => id.to_string(),
                    name => name.to_string(),
                },
                description: str_of(m, "description").to_string(),
                efforts: m
                    .get("supportedReasoningEfforts")
                    .and_then(Value::as_array)
                    .into_iter()
                    .flatten()
                    .map(|e| str_of(e, "reasoningEffort").to_string())
                    .filter(|e| !e.is_empty())
                    .collect(),
                default_effort: Some(str_of(m, "defaultReasoningEffort"))
                    .filter(|e| !e.is_empty())
                    .map(str::to_string),
            })
        })
        .collect()
}

impl Codex {
    /// A session that resumes thread `resume` (already checked as a plain id), if any, on the
    /// model and effort `choice` and the permission mode `mode`, if they are offered.
    pub fn new(resume: Option<&str>, choice: Option<Choice>, mode: Option<String>) -> Self {
        Self {
            resume: resume.map(str::to_string),
            models: Models::wanting(choice),
            modes: Modes::wanting(mode),
            ..Self::default()
        }
    }

    /// The sandbox for preset `mode`: the thread's own, if it started in one of that kind, so
    /// its writable roots and network access are kept; else the preset's.
    fn sandbox(&self, mode: &str) -> Value {
        let kind = if mode == "read-only" {
            "readOnly"
        } else {
            "workspaceWrite"
        };
        match &self.start_sandbox {
            Some(own) if str_of(own, "type") == kind => own.clone(),
            _ if kind == "readOnly" => json!({"type": "readOnly", "networkAccess": false}),
            _ => json!({"type": "workspaceWrite", "writableRoots": [], "networkAccess": false,
                "excludeTmpdirEnvVar": false, "excludeSlashTmp": false}),
        }
    }

    /// Messages sent before the thread started, or before the model chosen at the start could
    /// be checked, go now.
    fn flush(&mut self, step: &mut Step) {
        let Some(thread) = self.thread.clone() else {
            return;
        };
        if self.models.holds() {
            return;
        }
        for text in std::mem::take(&mut self.queued) {
            let line = self.turn_start(&thread, &text);
            step.send(line);
        }
    }

    fn thread_start(&mut self) -> Value {
        let cwd = self.cwd.clone();
        self.request("thread/start", json!({"cwd": cwd}))
    }

    fn request(&mut self, method: &'static str, params: Value) -> Value {
        self.next_id += 1;
        self.pending.insert(self.next_id, method);
        json!({"id": self.next_id, "method": method, "params": params})
    }

    fn turn_start(&mut self, thread: &str, text: &str) -> Value {
        let mut params = json!({"threadId": thread,
            "input": [{"type": "text", "text": text, "text_elements": []}]});
        // the model and effort chosen, for this turn and the ones after; no effort chosen is
        // the model's own default, said rather than left to the effort of an earlier turn
        if let Some(mode) = &self.mode_chosen {
            params["approvalPolicy"] = json!("on-request");
            params["sandboxPolicy"] = self.sandbox(mode);
        }
        if let Some(choice) = &self.models.chosen {
            params["model"] = json!(choice.model);
            let model = self.models.list.iter().find(|m| m.id == choice.model);
            let effort = choice
                .effort
                .clone()
                .or_else(|| model.and_then(|m| m.default_effort.clone()));
            if let Some(effort) = effort {
                params["effort"] = json!(effort);
            }
        }
        self.request("turn/start", params)
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
                "thread/resume" => {
                    // the thread could not be continued: a new one, and the window is told
                    self.resume = None;
                    step.show(ChatEvent::Log {
                        text: format!("could not continue the conversation: {message}"),
                    });
                    step.show(ChatEvent::Resumed { ok: false });
                    let start = self.thread_start();
                    step.send(start);
                }
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
                "model/list" => {
                    step.show(ChatEvent::Log {
                        text: format!("could not list Codex's models: {message}"),
                    });
                    self.models.listed(vec![], step);
                    self.flush(step);
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
                    if let Some(thread) = self.resume.clone() {
                        let cwd = self.cwd.clone();
                        let resume = self.request(
                            "thread/resume",
                            json!({"threadId": thread, "cwd": cwd, "excludeTurns": true}),
                        );
                        step.send(resume);
                    } else {
                        let start = self.thread_start();
                        step.send(start);
                    }
                    let list = self.request("model/list", json!({}));
                    step.send(list);
                }
            }
            "model/list" => {
                if let Some(choice) = self.models.listed(models_of(&result), step) {
                    self.models.chosen = Some(choice);
                }
                step.show(self.models.event());
                self.flush(step);
            }
            "thread/start" | "thread/resume" => {
                let thread = result.pointer("/thread/id").and_then(Value::as_str);
                if let Some(thread) = thread.map(str::to_string) {
                    if method == "thread/resume" {
                        step.show(ChatEvent::Resumed { ok: true });
                    }
                    step.show(ChatEvent::Conversation { id: thread.clone() });
                    self.thread = Some(thread);
                    if self.models.using(str_of(&result, "model")) && self.models.known {
                        step.show(self.models.event());
                    }
                    self.start_sandbox = result.get("sandbox").filter(|s| s.is_object()).cloned();
                    self.modes.using(&mode_of(&result));
                    if let Some(mode) = self.modes.listed(modes(), step) {
                        self.modes.current = Some(mode.clone());
                        self.mode_chosen = Some(mode);
                    }
                    step.show(self.modes.event());
                    self.flush(step);
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
        self.queued.push(text.to_string());
        self.flush(&mut step);
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

    fn choose(&mut self, choice: Choice) -> Result<Step, String> {
        checked(&self.models.list, &choice)?;
        self.models.chosen = Some(choice);
        let mut step = Step::default();
        step.show(self.models.event());
        Ok(step)
    }

    fn set_mode(&mut self, mode: &str) -> Result<Step, String> {
        self.modes.offered(mode)?;
        self.modes.current = Some(mode.to_string());
        self.mode_chosen = Some(mode.to_string());
        let mut step = Step::default();
        step.show(self.modes.event());
        Ok(step)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn line(s: &str) -> Value {
        serde_json::from_str(s).unwrap()
    }

    /// `model/list`'s answer, cut to the fields that matter, as Codex 0.162.0 gave it.
    const LIST: &str = r#"{"id":4,"result":{"data":[
        {"id":"gpt","model":"gpt","displayName":"GPT","description":"Workhorse.","hidden":false,
         "isDefault":true,"defaultReasoningEffort":"low",
         "supportedReasoningEfforts":[{"reasoningEffort":"low","description":""},{"reasoningEffort":"high","description":""}]},
        {"id":"gpt-mini","model":"gpt-mini","displayName":"GPT mini","description":"Fast.","hidden":false,
         "isDefault":false,"defaultReasoningEffort":"medium",
         "supportedReasoningEfforts":[{"reasoningEffort":"medium","description":""}]},
        {"id":"secret","model":"secret","displayName":"Secret","description":"","hidden":true,
         "isDefault":false,"defaultReasoningEffort":"low","supportedReasoningEfforts":[]}
        ],"nextCursor":null}}"#;

    /// Start a session as P4 did: initialize, account, thread, models.
    fn started(account: &str) -> (Codex, Step) {
        let mut c = Codex::default();
        let mut all = c.start("/work");
        for l in [
            r#"{"id":1,"result":{"userAgent":"x","codexHome":"/h","platformFamily":"unix","platformOs":"linux"}}"#.to_string(),
            format!(r#"{{"id":2,"result":{account}}}"#),
            r#"{"id":3,"result":{"thread":{"id":"th-1"},"model":"gpt"}}"#.to_string(),
            LIST.to_string(),
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
                json!({"id": 4, "method": "model/list", "params": {}}),
            ]
        );
        assert_eq!(
            step.events[0],
            ChatEvent::Conversation { id: "th-1".into() }
        );
        let ChatEvent::Models {
            models,
            model,
            effort,
            current,
        } = &step.events[2]
        else {
            panic!("{:?}", step.events[2]);
        };
        // the hidden model is left out, as Codex's own picker leaves it out
        assert_eq!(
            models.iter().map(|m| m.id.as_str()).collect::<Vec<_>>(),
            ["gpt", "gpt-mini"]
        );
        assert_eq!(models[0].efforts, ["low", "high"]);
        assert_eq!(models[0].default_effort.as_deref(), Some("low"));
        assert_eq!(
            (model.as_deref(), effort, current.as_deref()),
            (Some("gpt"), &None, Some("gpt"))
        );
        assert_eq!(c.thread.as_deref(), Some("th-1"));
    }

    fn turn(c: &mut Codex, text: &str) -> Value {
        let step = c.send(text).unwrap();
        assert_eq!(step.write.len(), 1);
        step.write[0]["params"].clone()
    }

    #[test]
    fn a_model_and_effort_chosen_go_with_each_turn_after() {
        let (mut c, _) = started(r#"{"account":null,"requiresOpenaiAuth":false}"#);
        // nothing chosen: Codex's own setting
        assert!(turn(&mut c, "a").get("model").is_none());
        let pick = |model: &str, effort: Option<&str>| Choice {
            model: model.into(),
            effort: effort.map(str::to_string),
        };
        assert!(c.choose(pick("gpt-9", None)).is_err());
        assert!(c.choose(pick("secret", None)).is_err());
        assert!(c.choose(pick("gpt-mini", Some("high"))).is_err());
        let chosen = c.choose(pick("gpt", Some("high"))).unwrap();
        assert!(chosen.write.is_empty()); // it goes with the next turn
        assert!(matches!(&chosen.events[..],
            [ChatEvent::Models { model: Some(m), effort: Some(e), .. }] if m == "gpt" && e == "high"));
        let params = turn(&mut c, "b");
        assert_eq!(
            (&params["model"], &params["effort"]),
            (&json!("gpt"), &json!("high"))
        );
        // the model's own default effort is said, not left to the one before
        c.choose(pick("gpt-mini", None)).unwrap();
        let params = turn(&mut c, "c");
        assert_eq!(
            (&params["model"], &params["effort"]),
            (&json!("gpt-mini"), &json!("medium"))
        );
    }

    /// A session whose thread started as `thread` says (its approval policy and sandbox).
    fn started_as(thread: &str, mode: Option<&str>) -> (Codex, Step) {
        let mut c = Codex::new(None, None, mode.map(str::to_string));
        c.start("/w");
        c.read(line(r#"{"id":1,"result":{}}"#));
        c.read(line(
            r#"{"id":2,"result":{"account":null,"requiresOpenaiAuth":false}}"#,
        ));
        let step = c.read(line(thread));
        c.read(line(LIST));
        (c, step)
    }

    /// `thread/start` as P7 gave it in a trusted folder, cut to the fields that matter, with
    /// network access on.
    const TRUSTED: &str = r#"{"id":3,"result":{"thread":{"id":"th-1"},"model":"gpt","approvalPolicy":"on-request","sandbox":{"type":"workspaceWrite","writableRoots":[],"networkAccess":true,"excludeTmpdirEnvVar":false,"excludeSlashTmp":false}}}"#;

    #[test]
    fn a_permission_mode_goes_with_each_turn_after() {
        let (mut c, step) = started_as(TRUSTED, None);
        let Some(ChatEvent::Modes { modes, mode, .. }) = step
            .events
            .iter()
            .find(|e| matches!(e, ChatEvent::Modes { .. }))
        else {
            panic!("{:?}", step.events);
        };
        assert_eq!(
            modes.iter().map(|m| m.id.as_str()).collect::<Vec<_>>(),
            ["auto", "read-only"]
        );
        assert_eq!(mode.as_deref(), Some("auto"));
        // nothing chosen: the thread's own
        assert!(turn(&mut c, "a").get("sandboxPolicy").is_none());
        assert!(c.set_mode("full-access").is_err());
        let chosen = c.set_mode("read-only").unwrap();
        assert!(chosen.write.is_empty()); // it goes with the next turn
        let params = turn(&mut c, "b");
        assert_eq!(params["approvalPolicy"], "on-request");
        assert_eq!(
            params["sandboxPolicy"],
            json!({"type": "readOnly", "networkAccess": false})
        );
        // back to Auto: the thread's own workspace sandbox, network access and all
        c.set_mode("auto").unwrap();
        let params = turn(&mut c, "c");
        assert_eq!(params["sandboxPolicy"]["type"], "workspaceWrite");
        assert_eq!(params["sandboxPolicy"]["networkAccess"], true);
    }

    #[test]
    fn a_thread_in_a_mode_not_offered_says_which_and_a_mode_from_before_is_used() {
        let full = r#"{"id":3,"result":{"thread":{"id":"th-1"},"approvalPolicy":"never","sandbox":{"type":"dangerFullAccess"}}}"#;
        let (_, step) = started_as(full, None);
        assert!(step.events.iter().any(|e| matches!(e,
            ChatEvent::Modes { mode: None, current: Some(c), .. } if c == "never, dangerFullAccess")));
        let (mut c, _) = started_as(TRUSTED, Some("read-only"));
        assert_eq!(turn(&mut c, "x")["sandboxPolicy"]["type"], "readOnly");
        let (mut c, _) = started_as(TRUSTED, Some("full-access"));
        assert!(turn(&mut c, "x").get("sandboxPolicy").is_none());
    }

    #[test]
    fn a_choice_from_before_is_used_if_offered_and_waits_for_the_list() {
        let mut c = Codex::new(
            None,
            Some(Choice {
                model: "gpt-mini".into(),
                effort: None,
            }),
            None,
        );
        c.start("/w");
        c.read(line(r#"{"id":1,"result":{}}"#));
        c.read(line(
            r#"{"id":2,"result":{"account":null,"requiresOpenaiAuth":false}}"#,
        ));
        c.read(line(
            r#"{"id":3,"result":{"thread":{"id":"th-1"},"model":"gpt"}}"#,
        ));
        assert!(c.send("early").unwrap().write.is_empty());
        let step = c.read(line(LIST));
        assert_eq!(step.write[0]["params"]["model"], "gpt-mini");
        assert_eq!(step.write[0]["params"]["effort"], "medium");

        let mut gone = Codex::new(
            None,
            Some(Choice {
                model: "gpt-old".into(),
                effort: None,
            }),
            None,
        );
        gone.start("/w");
        gone.read(line(r#"{"id":1,"result":{}}"#));
        gone.read(line(
            r#"{"id":2,"result":{"account":null,"requiresOpenaiAuth":false}}"#,
        ));
        gone.read(line(
            r#"{"id":3,"result":{"thread":{"id":"th-1"},"model":"gpt"}}"#,
        ));
        let step = gone.read(line(LIST));
        assert!(matches!(&step.events[0], ChatEvent::Log { text } if text.contains("gpt-old")));
        assert!(turn(&mut gone, "x").get("model").is_none());
    }

    #[test]
    fn a_thread_is_resumed_by_its_id_or_a_new_one_starts() {
        let mut c = Codex::new(Some("th-7"), None, None);
        let mut all = c.start("/work");
        for l in [
            r#"{"id":1,"result":{}}"#,
            r#"{"id":2,"result":{"account":null,"requiresOpenaiAuth":false}}"#,
            r#"{"id":3,"result":{"thread":{"id":"th-7"}}}"#,
        ] {
            let step = c.read(line(l));
            all.events.extend(step.events);
            all.write.extend(step.write);
        }
        assert_eq!(
            all.write[3],
            json!({"id": 3, "method": "thread/resume",
                "params": {"threadId": "th-7", "cwd": "/work", "excludeTurns": true}})
        );
        assert_eq!(
            all.events[..2],
            [
                ChatEvent::Resumed { ok: true },
                ChatEvent::Conversation { id: "th-7".into() }
            ]
        );
        assert!(matches!(all.events[2], ChatEvent::Modes { .. }));
        let mut gone = Codex::new(Some("th-gone"), None, None);
        gone.start("/w");
        gone.read(line(r#"{"id":1,"result":{}}"#));
        gone.read(line(
            r#"{"id":2,"result":{"account":null,"requiresOpenaiAuth":false}}"#,
        ));
        let step = gone.read(line(
            r#"{"id":3,"error":{"code":-32600,"message":"no rollout found"}}"#,
        ));
        assert!(step.events.contains(&ChatEvent::Resumed { ok: false }));
        assert_eq!(
            step.write,
            vec![json!({"id": 5, "method": "thread/start", "params": {"cwd": "/w"}})]
        );
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
                json!({"id": 5, "method": "turn/start", "params": {"threadId": "th-9",
            "input": [{"type": "text", "text": "early", "text_elements": []}]}})
            ]
        );
    }

    #[test]
    fn a_turn_with_a_command_approval_runs_as_p4_did() {
        let (mut c, _) = started(r#"{"account":null,"requiresOpenaiAuth":false}"#);
        c.send("please run it").unwrap();
        let lines = [
            r#"{"id":5,"result":{"turn":{"id":"tu-1","status":"inProgress"}}}"#,
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
        c.read(line(r#"{"id":5,"result":{"turn":{"id":"tu-1"}}}"#));
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
                json!({"id": 6, "method": "turn/interrupt", "params": {"threadId": "th-1", "turnId": "tu-1"}}),
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
