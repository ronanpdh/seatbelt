//! What a chat shows, whichever CLI it runs, and what each CLI's driver must do.
//!
//! A driver turns one CLI's headless protocol into `ChatEvent`s, and the window's few actions
//! (send a message, answer an approval, choose a model, interrupt) into that protocol's
//! messages. Drivers do
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
    /// What a tool was given, whole, for the window to show when its line is opened.
    ToolInput { id: String, input: String },
    /// What a tool gave back: its output, error or diff.
    ToolOutput { id: String, output: String },
    /// More of the agent's reasoning, as the CLI shows it; created by its first piece.
    Thinking { id: String, delta: String },
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
    /// The CLI's own id for this conversation: a later chat, or the CLI's own interface in a
    /// terminal, resumes it.
    Conversation { id: String },
    /// Whether the conversation asked for was continued; if not, this is a new one.
    Resumed { ok: bool },
    /// The models the CLI offers and what is in use; sent again whenever either changes.
    Models {
        models: Vec<Model>,
        /// The model in use, by id from `models`; None while the CLI's own setting is in use
        /// and is not one of them.
        model: Option<String>,
        /// The effort chosen; None is the model's own default.
        effort: Option<String>,
        /// The model the CLI says it is using, as it names it, when it says.
        current: Option<String>,
    },
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

/// A model a CLI offers, as its own list gives it.
#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct Model {
    /// What the CLI is told to use.
    pub id: String,
    pub name: String,
    pub description: String,
    /// The reasoning efforts it takes, as the CLI names them; empty when there is no choice.
    pub efforts: Vec<String>,
    /// The effort it uses when none is chosen, when the CLI says.
    pub default_effort: Option<String>,
}

/// A model and effort asked for, by the ids in the CLI's own list. No effort is the model's
/// own default.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Choice {
    pub model: String,
    pub effort: Option<String>,
}

/// `choice`, checked against the CLI's own list: the model is on it, and the effort is one the
/// model takes. Anything else is refused, not passed on: Codex and Gemini CLI send a model or
/// effort they do not know to the provider as it is.
pub fn checked(models: &[Model], choice: &Choice) -> Result<(), String> {
    let model = models
        .iter()
        .find(|m| m.id == choice.model)
        .ok_or_else(|| format!("{} is not one of the models offered", choice.model))?;
    match &choice.effort {
        Some(effort) if !model.efforts.contains(effort) => {
            Err(format!("{} does not take the effort {effort}", model.name))
        }
        _ => Ok(()),
    }
}

/// What a driver knows of models: the CLI's list, the choice made, and what the CLI says it
/// uses.
#[derive(Debug, Default)]
pub struct Models {
    pub list: Vec<Model>,
    /// The list has arrived (or could not be had).
    pub known: bool,
    pub chosen: Option<Choice>,
    pub current: Option<String>,
    /// Asked for as the chat started: applied once the list has arrived, if it is on it.
    pub wanted: Option<Choice>,
}

impl Models {
    pub fn wanting(wanted: Option<Choice>) -> Self {
        Self {
            wanted,
            ..Self::default()
        }
    }

    pub fn event(&self) -> ChatEvent {
        let model = match &self.chosen {
            Some(choice) => Some(choice.model.clone()),
            None => self
                .current
                .clone()
                .filter(|c| self.list.iter().any(|m| &m.id == c)),
        };
        ChatEvent::Models {
            models: self.list.clone(),
            model,
            effort: self.chosen.as_ref().and_then(|c| c.effort.clone()),
            current: self.current.clone(),
        }
    }

    /// The list has arrived: what was asked for at the start, if it is on it, to apply now.
    pub fn listed(&mut self, list: Vec<Model>, step: &mut Step) -> Option<Choice> {
        self.list = list;
        self.known = true;
        let wanted = self.wanted.take()?;
        match checked(&self.list, &wanted) {
            Ok(()) => Some(wanted),
            Err(e) => {
                step.show(ChatEvent::Log {
                    text: format!("the model chosen before is not used: {e}"),
                });
                None
            }
        }
    }

    /// What the CLI says it is using; true if that is news.
    pub fn using(&mut self, current: &str) -> bool {
        if current.is_empty() || self.current.as_deref() == Some(current) {
            return false;
        }
        self.current = Some(current.to_string());
        true
    }

    /// A message waits until the choice asked for at the start is made.
    pub fn holds(&self) -> bool {
        self.wanted.is_some() && !self.known
    }
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
    /// Use `choice` from the next message on; refused unless it is on the CLI's own list.
    fn choose(&mut self, choice: Choice) -> Result<Step, String>;
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

/// A conversation id the app passes back to a CLI to resume it: letters, digits, `-` and `_`,
/// starting with a letter or digit, at most 128 characters. Anything else is refused, so it
/// can never be read as an option or as anything but an id.
pub fn conversation_id(id: &str) -> Option<&str> {
    let ok = !id.is_empty()
        && id.len() <= 128
        && id.chars().next().is_some_and(|c| c.is_ascii_alphanumeric())
        && id
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_');
    ok.then_some(id)
}

/// The arguments that resume conversation `id` in the CLI's own interface.
pub fn resume_args(cli: &str, id: &str) -> Vec<String> {
    match cli {
        "codex" => vec!["resume".into(), id.into()],
        _ => vec!["--resume".into(), id.into()], // Claude Code and Gemini CLI
    }
}

/// The most of one tool's input or output the window is sent; the ledger keeps all of it.
pub const MAX_TOOL_TEXT: usize = 20_000;

/// A tool's input or output for the window: a string as it is, anything else as indented
/// JSON, cut to MAX_TOOL_TEXT characters.
pub fn shown(value: &Value) -> String {
    let text = match value {
        Value::Null => String::new(),
        Value::String(s) => s.clone(),
        other => serde_json::to_string_pretty(other).unwrap_or_default(),
    };
    cut(&text, MAX_TOOL_TEXT)
}

/// The text of content blocks (`[{"type": "text", "text": ...}, ...]`, as the Messages API
/// and MCP give a tool's result), or a string as it is; other blocks are named, not shown.
pub fn content_text(content: &Value) -> String {
    let text = match content {
        Value::String(s) => s.clone(),
        Value::Array(blocks) => blocks
            .iter()
            .map(
                |b| match (str_of(b, "type"), b.get("text").and_then(Value::as_str)) {
                    (_, Some(text)) => text.to_string(),
                    ("", None) => b.to_string(),
                    (kind, None) => format!("[{kind}]"),
                },
            )
            .collect::<Vec<_>>()
            .join("\n"),
        Value::Null => String::new(),
        other => serde_json::to_string_pretty(other).unwrap_or_default(),
    };
    cut(&text, MAX_TOOL_TEXT)
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
    fn tool_text_is_a_string_as_it_is_and_anything_else_as_indented_json() {
        assert_eq!(shown(&json!("ls\nout")), "ls\nout");
        assert_eq!(shown(&json!({"a": 1})), "{\n  \"a\": 1\n}");
        assert_eq!(shown(&Value::Null), "");
        assert_eq!(
            content_text(
                &json!([{"type": "text", "text": "one"}, {"type": "image"}, {"type": "text", "text": "two"}])
            ),
            "one\n[image]\ntwo"
        );
        assert_eq!(content_text(&json!("plain")), "plain");
        assert_eq!(
            shown(&json!("x".repeat(MAX_TOOL_TEXT + 5))).chars().count(),
            MAX_TOOL_TEXT + 1
        );
    }

    #[test]
    fn only_plain_ids_are_passed_back_to_a_cli() {
        assert_eq!(
            conversation_id("57bedf4b-f1bf-464f-b5ae-fe4eb596e580"),
            Some("57bedf4b-f1bf-464f-b5ae-fe4eb596e580")
        );
        assert_eq!(conversation_id("thread_1"), Some("thread_1"));
        for bad in [
            "",
            "--dangerously-skip-permissions",
            "-x",
            "a b",
            "a;b",
            "a/b",
            "ü",
        ] {
            assert_eq!(conversation_id(bad), None, "{bad}");
        }
        assert_eq!(conversation_id(&"a".repeat(129)), None);
        assert_eq!(resume_args("codex", "t1"), ["resume", "t1"]);
        assert_eq!(resume_args("claude", "s1"), ["--resume", "s1"]);
        assert_eq!(resume_args("gemini", "g1"), ["--resume", "g1"]);
    }

    #[test]
    fn a_choice_is_checked_against_the_clis_own_list() {
        let models = vec![Model {
            id: "m".into(),
            name: "M".into(),
            description: String::new(),
            efforts: vec!["low".into()],
            default_effort: None,
        }];
        let choice = |model: &str, effort: Option<&str>| Choice {
            model: model.into(),
            effort: effort.map(str::to_string),
        };
        assert!(checked(&models, &choice("m", None)).is_ok());
        assert!(checked(&models, &choice("m", Some("low"))).is_ok());
        assert!(checked(&models, &choice("m", Some("max"))).is_err());
        assert!(checked(&models, &choice("x", None)).is_err());
        assert!(checked(&[], &choice("m", None)).is_err());
        assert_eq!(
            serde_json::to_value(Models::default().event()).unwrap(),
            json!({"kind": "models", "models": [], "model": null, "effort": null, "current": null})
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
