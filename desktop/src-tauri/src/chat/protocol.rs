//! What a chat shows, whichever CLI it runs, and what each CLI's driver must do.
//!
//! A driver turns one CLI's headless protocol into `ChatEvent`s, and the window's few actions
//! (send a message, answer an approval, interrupt) into that protocol's messages. Drivers do
//! no I/O: they take a parsed line and return what to show and what to write, so each is tested
//! with the lines the real CLIs printed (design: "Checked against the real CLIs").

use serde::Serialize;
use serde_json::Value;

/// One thing for the chat window. Text that came from the model or a tool is untrusted: the
/// window shows it as text only.
#[derive(Clone, Debug, PartialEq, Serialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum ChatEvent {
    /// More of an assistant message, which is created by its first piece.
    Text { id: String, delta: String },
    /// An assistant message, whole: it replaces any pieces sent for the same id.
    Message { id: String, text: String },
    /// A tool's state. `name` and `detail` are empty on an update that does not change them.
    Tool {
        id: String,
        name: String,
        detail: String,
        status: ToolStatus,
    },
    /// The CLI asks before a tool use; answered with `answer(id, allow)`.
    Approval {
        id: String,
        tool: String,
        detail: String,
    },
    /// An approval that no longer waits: answered, or withdrawn by an interrupt.
    Resolved { id: String },
    /// The turn is over: the chat can take the next message.
    TurnEnd { ok: bool, error: Option<String> },
    /// The CLI is not signed in; the window offers its own sign-in, in a terminal.
    SignIn { reason: String },
    /// A line for the session's log, not the conversation.
    Log { text: String },
    /// The session has ended. `report` is what `seatbelt run` recorded.
    Exit {
        code: Option<i32>,
        report: Option<Value>,
    },
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ToolStatus {
    Running,
    Done,
    Failed,
    Declined,
}

/// What a driver gives back for one line: events for the window and lines to write.
#[derive(Debug, Default, PartialEq)]
pub struct Step {
    pub events: Vec<ChatEvent>,
    pub write: Vec<Value>,
}

impl Step {
    pub fn show(&mut self, event: ChatEvent) {
        self.events.push(event);
    }

    pub fn send(&mut self, line: Value) {
        self.write.push(line);
    }
}

pub trait Driver: Send {
    /// Arguments for the CLI, after `seatbelt run <cli> --`.
    fn args(&self) -> Vec<String>;
    /// Lines to write as the session starts, in `cwd`.
    fn start(&mut self, cwd: &str) -> Step;
    /// One JSON line the CLI printed.
    fn read(&mut self, line: Value) -> Step;
    /// The user's message.
    fn send(&mut self, text: &str) -> Result<Step, String>;
    /// The user's answer to the approval `id`.
    fn answer(&mut self, id: &str, allow: bool) -> Result<Step, String>;
    /// Stop the turn in progress.
    fn interrupt(&mut self) -> Step;
}

/// A one-line summary of a tool's input for an approval card or a tool line: the command, the
/// file or the pattern when there is one, else the input as compact JSON, cut to 2000
/// characters.
pub fn summary(input: &Value) -> String {
    const KEYS: [&str; 7] = [
        "command",
        "cmd",
        "file_path",
        "path",
        "pattern",
        "url",
        "query",
    ];
    let text = KEYS
        .iter()
        .find_map(|k| input.get(*k))
        .map(|v| match v {
            Value::String(s) => s.clone(),
            Value::Array(parts) => parts
                .iter()
                .map(|p| p.as_str().map_or_else(|| p.to_string(), str::to_string))
                .collect::<Vec<_>>()
                .join(" "),
            other => other.to_string(),
        })
        .unwrap_or_else(|| match input {
            Value::Null => String::new(),
            Value::Object(o) if o.is_empty() => String::new(),
            other => other.to_string(),
        });
    cut(&text, 2000)
}

pub fn cut(text: &str, max: usize) -> String {
    match text.char_indices().nth(max) {
        Some((at, _)) => format!("{}…", &text[..at]),
        None => text.to_string(),
    }
}

pub fn str_of<'a>(v: &'a Value, key: &str) -> &'a str {
    v.get(key).and_then(Value::as_str).unwrap_or_default()
}

/// Words that say a failure is about signing in.
pub fn about_sign_in(text: &str) -> bool {
    let t = text.to_lowercase();
    [
        "auth",
        "login",
        "log in",
        "sign in",
        "credential",
        "api key",
        "unauthorized",
        "401",
    ]
    .iter()
    .any(|w| t.contains(w))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn a_summary_names_the_command_or_file_else_shows_the_input() {
        assert_eq!(summary(&json!({"command": "ls -la", "x": 1})), "ls -la");
        assert_eq!(
            summary(&json!({"command": ["bash", "-lc", "echo hi"]})),
            "bash -lc echo hi"
        );
        assert_eq!(summary(&json!({"file_path": "/a/b.rs"})), "/a/b.rs");
        assert_eq!(summary(&json!({"a": 1})), r#"{"a":1}"#);
        assert_eq!(summary(&json!({})), "");
        assert_eq!(
            summary(&json!({"command": "x".repeat(3000)}))
                .chars()
                .count(),
            2001
        );
    }

    #[test]
    fn events_are_tagged_by_kind_for_the_window() {
        let e = ChatEvent::Tool {
            id: "t".into(),
            name: "Bash".into(),
            detail: "ls".into(),
            status: ToolStatus::Running,
        };
        assert_eq!(
            serde_json::to_value(e).unwrap(),
            json!({"kind": "tool", "id": "t", "name": "Bash", "detail": "ls", "status": "running"})
        );
    }
}
