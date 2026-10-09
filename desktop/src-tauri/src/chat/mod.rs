//! Chats: each is `seatbelt run --exe <path> <cli> -- <headless arguments>` with pipes instead
//! of a terminal, so it is recorded exactly as a terminal session is. seatbelt says its own
//! lines on stderr; stdout carries only the CLI's protocol, which a driver turns into chat
//! events. The window can send a message, answer an approval, choose a model or a permission
//! mode the chat offers, interrupt, or end the chat; it never writes to the CLI itself.

mod acp;
mod claude;
mod codex;
pub mod protocol;

use std::collections::HashMap;
use std::ffi::OsStr;
use std::io::{BufRead, BufReader, Read, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::atomic::{AtomicBool, AtomicU32, Ordering};
use std::sync::{mpsc, Arc, Mutex, MutexGuard};
use std::thread;
use std::time::{Duration, Instant};

use serde_json::Value;
use tauri::ipc::Channel;

use crate::pty::{take_report, CLOSE_WAIT, RUN_REPORT_ENV};
use protocol::{ChatEvent, Choice, Driver, Step};

/// How long an ended chat's CLI has, once its input is closed, before `seatbelt run` is asked
/// to end it as closing a terminal would.
const EOF_WAIT: Duration = Duration::from_secs(3);
/// Said when a chat leaves out a folder's Claude Code settings.
const UNTRUSTED: &str = "Claude Code has not been told to trust this folder, so this chat \
leaves out the folder's own Claude Code settings (.claude/settings.json, .mcp.json): their \
hooks, permissions and MCP servers. To use them, open Claude Code in a terminal here and \
accept its trust prompt; the chat then starts with them.";

/// Whether Claude Code has been told to trust `cwd`: the trust prompt accepted, by its own
/// record in `.claude.json` (in `CLAUDE_CONFIG_DIR`, else the home folder), for the folder
/// or one above it within its git repository (see `trusted_in`). A record that cannot be read
/// is no.
fn claude_trusts(cwd: &Path) -> bool {
    let record = std::env::var_os("CLAUDE_CONFIG_DIR")
        .map(PathBuf::from)
        .or_else(crate::tools::home_dir)
        .map(|dir| dir.join(".claude.json"));
    record
        .and_then(|path| std::fs::read_to_string(path).ok())
        .and_then(|text| serde_json::from_str::<Value>(&text).ok())
        .is_some_and(|record| trusted_in(&record, cwd))
}

/// `cwd`, or a folder above it, is one `record` says the trust prompt was accepted for. As in
/// Claude Code, the folders above stop at the repository's root (the nearest folder holding
/// `.git`), so a repository cloned into a trusted folder is not trusted with it; outside a
/// repository they go to the top.
fn trusted_in(record: &Value, cwd: &Path) -> bool {
    let Some(projects) = record.get("projects").and_then(Value::as_object) else {
        return false;
    };
    let trusted: Vec<PathBuf> = projects
        .iter()
        .filter(|(_, p)| p.get("hasTrustDialogAccepted") == Some(&Value::Bool(true)))
        .map(|(path, _)| {
            Path::new(path)
                .canonicalize()
                .unwrap_or_else(|_| PathBuf::from(path))
        })
        .collect();
    let cwd = cwd.canonicalize().unwrap_or_else(|_| cwd.to_path_buf());
    for folder in cwd.ancestors() {
        if trusted.iter().any(|t| t == folder) {
            return true;
        }
        if folder.join(".git").exists() {
            return false;
        }
    }
    false
}

/// The folder has Claude Code settings of its own that a chat would leave out.
fn has_project_settings(cwd: &Path) -> bool {
    [
        ".claude/settings.json",
        ".claude/settings.local.json",
        ".mcp.json",
    ]
    .iter()
    .any(|file| cwd.join(file).exists())
}

/// The longest line a CLI may print: anything longer is dropped, not buffered without end.
const MAX_LINE: usize = 32 * 1024 * 1024;
/// How long a chat's output may keep coming once its process has exited: a process the CLI
/// started may hold the pipes open past it.
const DRAIN_WAIT: Duration = Duration::from_secs(2);

/// The driver for `cli`, resuming conversation `resume` (a plain id: see `conversation_id`),
/// on the model and effort `choice` and the permission mode `mode`, if the CLI offers them.
/// `trusted`: Claude Code trusts the folder, so its project settings may load.
fn driver(
    cli: &str,
    resume: Option<&str>,
    choice: Option<Choice>,
    mode: Option<String>,
    trusted: bool,
) -> Option<Box<dyn Driver>> {
    match cli {
        "claude" => Some(Box::new(
            claude::Claude::new(resume, choice, mode).with_project_settings(trusted),
        )),
        "codex" => Some(Box::new(codex::Codex::new(resume, choice, mode))),
        "gemini" => Some(Box::new(acp::Acp::new(resume, choice, mode))),
        _ => None,
    }
}

/// What a chat runs: `seatbelt run --exe <exe> <cli> -- <driver's arguments>` in `cwd`.
pub struct Launch<'a> {
    pub seatbelt: &'a Path,
    pub cli: &'a str,
    pub exe: &'a Path,
    pub cwd: &'a Path,
    pub search_path: &'a OsStr,
    pub report: PathBuf,
    /// The conversation to continue, by the CLI's own id, checked by `conversation_id`.
    pub resume: Option<&'a str>,
    /// The model and effort to use, if the CLI offers them; else its own setting.
    pub choice: Option<Choice>,
    /// The permission mode to use, if it is one offered; else the CLI's own.
    pub mode: Option<String>,
}

struct Session {
    driver: Box<dyn Driver>,
    /// Lines for the CLI, written in order by a thread of the chat's own, so a CLI that is
    /// not reading never holds up the app or this session. None once the chat is closing:
    /// the writer then ends, and the CLI's input closes.
    input: Option<mpsc::Sender<Vec<u8>>>,
    events: Channel<ChatEvent>,
}

impl Session {
    /// Show the step's events and queue its lines, in that order.
    fn apply(&mut self, step: Step) {
        for event in step.events {
            let _ = self.events.send(event);
        }
        for line in step.write {
            let mut text = line.to_string();
            text.push('\n');
            let queued = self
                .input
                .as_ref()
                .is_some_and(|input| input.send(text.into_bytes()).is_ok());
            if !queued {
                let _ = self.events.send(ChatEvent::Log {
                    text: "could not write to the CLI: its input is closed".into(),
                });
            }
        }
    }
}

/// Write each line for the CLI to its input, in order, until the session lets go of it.
fn write_input(mut stdin: ChildStdin, lines: mpsc::Receiver<Vec<u8>>, events: Channel<ChatEvent>) {
    for line in lines {
        if let Err(e) = stdin.write_all(&line).and_then(|()| stdin.flush()) {
            let _ = events.send(ChatEvent::Log {
                text: format!("could not write to the CLI: {e}"),
            });
            break;
        }
    }
}

struct Chat {
    session: Arc<Mutex<Session>>,
    child: Arc<Mutex<Child>>,
    /// Its process has exited and been reaped: its pid may be another process's by now.
    exited: Arc<AtomicBool>,
}

#[derive(Default)]
pub struct Chats {
    next: AtomicU32,
    open: Arc<Mutex<HashMap<u32, Chat>>>,
}

fn lock<T>(m: &Mutex<T>) -> MutexGuard<'_, T> {
    m.lock().unwrap_or_else(std::sync::PoisonError::into_inner)
}

impl Chats {
    /// Start a chat. Its events, and its end, arrive on `events`.
    pub fn open(&self, launch: Launch<'_>, events: Channel<ChatEvent>) -> Result<u32, String> {
        let resume = match launch.resume {
            Some(id) => Some(protocol::conversation_id(id).ok_or("not a conversation id")?),
            None => None,
        };
        let trusted = launch.cli == "claude" && claude_trusts(launch.cwd);
        let mut driver = driver(launch.cli, resume, launch.choice, launch.mode, trusted)
            .ok_or("no chat for that CLI")?;
        if launch.cli == "claude" && !trusted && has_project_settings(launch.cwd) {
            let _ = events.send(ChatEvent::Note {
                text: UNTRUSTED.into(),
            });
        }
        let mut child = Command::new(launch.seatbelt)
            .arg("run")
            .arg("--exe")
            .arg(launch.exe)
            .arg(launch.cli)
            .arg("--")
            .args(driver.args())
            .current_dir(launch.cwd)
            .env("PATH", launch.search_path)
            .env(RUN_REPORT_ENV, &launch.report)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .map_err(|e| format!("could not start seatbelt: {e}"))?;
        let stdout = child.stdout.take().ok_or("no output from seatbelt")?;
        let stderr = child.stderr.take().ok_or("no output from seatbelt")?;
        let stdin = child.stdin.take().ok_or("no input for seatbelt")?;
        let (input, lines) = mpsc::channel::<Vec<u8>>();
        let writer_events = events.clone();
        thread::spawn(move || write_input(stdin, lines, writer_events));
        let start = driver.start(&launch.cwd.to_string_lossy());
        let session = Arc::new(Mutex::new(Session {
            driver,
            input: Some(input),
            events: events.clone(),
        }));
        lock(&session).apply(start);
        let id = self.next.fetch_add(1, Ordering::Relaxed) + 1;
        let child = Arc::new(Mutex::new(child));
        let exited = Arc::new(AtomicBool::new(false));
        lock(&self.open).insert(
            id,
            Chat {
                session: Arc::clone(&session),
                child: Arc::clone(&child),
                exited: Arc::clone(&exited),
            },
        );

        let (drained, done) = mpsc::channel::<()>();
        let reading = Arc::clone(&session);
        let out_done = drained.clone();
        thread::spawn(move || {
            read_protocol(stdout, &reading);
            let _ = out_done.send(());
        });
        let log = events.clone();
        thread::spawn(move || {
            read_log(stderr, &log);
            let _ = drained.send(());
        });
        let open = Arc::clone(&self.open);
        let report = launch.report;
        thread::spawn(move || {
            let code = loop {
                match lock(&child).try_wait() {
                    Ok(Some(status)) => break status.code(),
                    Ok(None) => thread::sleep(Duration::from_millis(100)),
                    Err(_) => break None,
                }
            };
            exited.store(true, Ordering::SeqCst);
            // the pipes close when seatbelt and the CLI have both ended, unless something
            // they started holds them: what is left after DRAIN_WAIT is not waited for
            let until = Instant::now() + DRAIN_WAIT;
            for _ in 0..2 {
                let left = until.saturating_duration_since(Instant::now());
                if done.recv_timeout(left).is_err() {
                    break;
                }
            }
            lock(&open).remove(&id);
            let _ = events.send(ChatEvent::Exit {
                code,
                report: take_report(&report),
            });
        });
        Ok(id)
    }

    fn session(&self, id: u32) -> Result<Arc<Mutex<Session>>, String> {
        let open = lock(&self.open);
        let chat = open.get(&id).ok_or("that chat has ended")?;
        Ok(Arc::clone(&chat.session))
    }

    pub fn send(&self, id: u32, text: &str) -> Result<(), String> {
        let session = self.session(id)?;
        let mut session = lock(&session);
        let step = session.driver.send(text)?;
        session.apply(step);
        Ok(())
    }

    pub fn answer(&self, id: u32, request: &str, allow: bool) -> Result<(), String> {
        let session = self.session(id)?;
        let mut session = lock(&session);
        let step = session.driver.answer(request, allow)?;
        session.apply(step);
        Ok(())
    }

    pub fn choose(&self, id: u32, choice: Choice) -> Result<(), String> {
        let session = self.session(id)?;
        let mut session = lock(&session);
        let step = session.driver.choose(choice)?;
        session.apply(step);
        Ok(())
    }

    pub fn set_mode(&self, id: u32, mode: &str) -> Result<(), String> {
        let session = self.session(id)?;
        let mut session = lock(&session);
        let step = session.driver.set_mode(mode)?;
        session.apply(step);
        Ok(())
    }

    pub fn interrupt(&self, id: u32) -> Result<(), String> {
        let session = self.session(id)?;
        let mut session = lock(&session);
        let step = session.driver.interrupt();
        session.apply(step);
        Ok(())
    }

    /// End a chat: its input closes, which ends each CLI's headless session; if it is still
    /// running EOF_WAIT later, `seatbelt run` gets SIGTERM and passes it on, as when a
    /// terminal closes; anything left after CLOSE_WAIT is killed. The run is closed and
    /// signed by `seatbelt run` in the first two cases, and by the next one in the last.
    pub fn close(&self, id: u32) -> Result<(), String> {
        let (session, child, exited) = {
            let open = lock(&self.open);
            let chat = open.get(&id).ok_or("that chat has ended")?;
            (
                Arc::clone(&chat.session),
                Arc::clone(&chat.child),
                Arc::clone(&chat.exited),
            )
        };
        lock(&session).input = None; // the writer ends, and with it the CLI's input
        thread::spawn(move || {
            // once it has exited and been reaped, its pid is no longer its own to signal
            let ended = |until: Instant| {
                while Instant::now() < until {
                    if exited.load(Ordering::SeqCst) {
                        return true;
                    }
                    thread::sleep(Duration::from_millis(100));
                }
                exited.load(Ordering::SeqCst)
            };
            if ended(Instant::now() + EOF_WAIT) {
                return;
            }
            let pid = lock(&child).id();
            if terminate(pid) && ended(Instant::now() + CLOSE_WAIT) {
                return;
            }
            let _ = lock(&child).kill(); // refused for a reaped child: std checks
        });
        Ok(())
    }

    pub fn running(&self) -> usize {
        lock(&self.open).len()
    }

    /// Close every chat, and wait until they have all ended or `wait` has passed.
    pub fn close_all(&self, wait: Duration) {
        let ids: Vec<u32> = lock(&self.open).keys().copied().collect();
        for id in ids {
            let _ = self.close(id);
        }
        let deadline = Instant::now() + wait;
        while self.running() > 0 && Instant::now() < deadline {
            thread::sleep(Duration::from_millis(100));
        }
    }
}

#[cfg(unix)]
fn terminate(pid: u32) -> bool {
    let Ok(pid) = libc::pid_t::try_from(pid) else {
        return false;
    };
    // SAFETY: kill(2) with a pid this process started and SIGTERM; no memory is touched
    unsafe { libc::kill(pid, libc::SIGTERM) == 0 }
}

#[cfg(not(unix))]
fn terminate(_pid: u32) -> bool {
    false // Windows: closing the input is the way to ask; then the process is killed
}

/// Read the CLI's protocol, a JSON object per line, through the driver.
fn read_protocol(stdout: impl Read, session: &Mutex<Session>) {
    let mut reader = BufReader::new(stdout);
    let mut buf = Vec::new();
    loop {
        buf.clear();
        match (&mut reader)
            .take(MAX_LINE as u64 + 1)
            .read_until(b'\n', &mut buf)
        {
            Ok(0) | Err(_) => break,
            Ok(_) => {}
        }
        if buf.len() > MAX_LINE && buf.last() != Some(&b'\n') {
            // too long: the rest of it is read and dropped a piece at a time, so the next
            // read starts on a line of its own and nothing of it is kept
            if !skip_line(&mut reader) {
                break;
            }
            let _ = lock(session).events.send(ChatEvent::Log {
                text: "a line over 32 MiB from the CLI was skipped".into(),
            });
            continue;
        }
        let mut session = lock(session);
        let text = String::from_utf8_lossy(&buf);
        let text = text.trim();
        if text.is_empty() {
            continue;
        }
        match serde_json::from_str::<Value>(text) {
            Ok(line) if line.is_object() => {
                let step = session.driver.read(line);
                session.apply(step);
            }
            _ => {
                let _ = session.events.send(ChatEvent::Log { text: plain(text) });
            }
        }
    }
}

/// Read and drop the rest of a line; false at the end of the output.
fn skip_line(reader: &mut impl BufRead) -> bool {
    let mut piece = Vec::new();
    loop {
        piece.clear();
        match reader.take(64 * 1024).read_until(b'\n', &mut piece) {
            Ok(0) | Err(_) => return false,
            Ok(_) if piece.last() == Some(&b'\n') => return true,
            Ok(_) => {}
        }
    }
}

/// seatbelt's own lines and the CLI's diagnostics, for the session's log.
fn read_log(stderr: impl Read, events: &Channel<ChatEvent>) {
    for line in BufReader::new(stderr).lines() {
        let Ok(line) = line else { break };
        let text = plain(&line);
        if !text.trim().is_empty() {
            let _ = events.send(ChatEvent::Log { text });
        }
    }
}

/// Text without terminal escape sequences or other control characters, for the log.
pub fn plain(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    let mut chars = text.chars().peekable();
    while let Some(c) = chars.next() {
        if c == '\u{1b}' {
            match chars.peek() {
                Some('[') => {
                    chars.next();
                    // a CSI sequence ends at its first byte in @..~
                    for c in chars.by_ref() {
                        if ('@'..='~').contains(&c) {
                            break;
                        }
                    }
                }
                Some(']') => {
                    chars.next();
                    // an OSC sequence ends at BEL or ESC \
                    while let Some(c) = chars.next() {
                        if c == '\u{7}' || (c == '\u{1b}' && chars.peek() == Some(&'\\')) {
                            if c == '\u{1b}' {
                                chars.next();
                            }
                            break;
                        }
                    }
                }
                _ => {
                    chars.next();
                }
            }
        } else if c == '\t' {
            out.push(' ');
        } else if !c.is_control() {
            out.push(c);
        }
    }
    out
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;
    use std::sync::mpsc;
    use tauri::ipc::InvokeResponseBody;

    #[test]
    fn escape_sequences_and_control_characters_are_taken_out_of_log_lines() {
        assert_eq!(plain("\u{1b}[1;32mok\u{1b}[0m done"), "ok done");
        assert_eq!(
            plain("\u{1b}]8;;http://x\u{7}link\u{1b}]8;;\u{1b}\\"),
            "link"
        );
        assert_eq!(plain("a\tb\u{7}c\u{0}"), "a bc");
    }

    /// A session whose events are collected, for feeding `read_protocol` directly.
    fn collecting() -> (Mutex<Session>, mpsc::Receiver<Value>) {
        let (tx, rx) = mpsc::channel();
        let events = Channel::new(move |body| {
            if let InvokeResponseBody::Json(json) = body {
                let _ = tx.send(serde_json::from_str(&json).unwrap());
            }
            Ok(())
        });
        let session = Session {
            driver: Box::new(claude::Claude::default()),
            input: None,
            events,
        };
        (Mutex::new(session), rx)
    }

    #[test]
    fn trust_is_claude_codes_own_record_for_the_folder_or_one_above_it_in_its_repository() {
        let dir = scratch("trust");
        let child = dir.join("repo").join("sub");
        std::fs::create_dir_all(&child).unwrap();
        let record = |path: &Path, accepted: bool| serde_json::json!({"projects": {path.to_string_lossy(): {"hasTrustDialogAccepted": accepted}}});
        assert!(trusted_in(&record(&dir.join("repo"), true), &child));
        assert!(trusted_in(&record(&child, true), &child));
        assert!(!trusted_in(&record(&child, false), &child));
        assert!(!trusted_in(&record(&child.join("deeper"), true), &child));
        assert!(!trusted_in(&serde_json::json!({}), &child));
        // a repository cloned into a trusted folder is not trusted with it, as in Claude Code;
        // a folder inside a trusted repository is
        std::fs::create_dir(dir.join("repo").join(".git")).unwrap();
        assert!(!trusted_in(&record(&dir, true), &child));
        assert!(!trusted_in(&record(&dir, true), &dir.join("repo")));
        assert!(trusted_in(&record(&dir.join("repo"), true), &child));
        assert!(!has_project_settings(&child));
        std::fs::write(child.join(".mcp.json"), "{}").unwrap();
        assert!(has_project_settings(&child));
        let _ = std::fs::remove_dir_all(dir);
    }

    #[test]
    fn a_line_over_the_limit_is_skipped_and_one_at_it_is_read_with_the_next() {
        let end = r#"{"type":"result","subtype":"success","is_error":false,"result":"ok"}"#;
        // a line at the limit, its newline the byte after: read, and the line after it too
        let pad = MAX_LINE - r#"{"type":"x","p":""}"#.len();
        let at = format!("{{\"type\":\"x\",\"p\":\"{}\"}}\n{end}\n", "a".repeat(pad));
        assert_eq!(at.find('\n'), Some(MAX_LINE));
        let (session, rx) = collecting();
        read_protocol(at.as_bytes(), &session);
        let kinds: Vec<Value> = rx.try_iter().map(|e| e["kind"].clone()).collect();
        assert_eq!(kinds, ["turn_end"]);
        // one byte over: skipped, said, and the next line read whole
        let over = format!("{}\n{end}\n", "b".repeat(MAX_LINE + 1));
        let (session, rx) = collecting();
        read_protocol(over.as_bytes(), &session);
        let events: Vec<Value> = rx.try_iter().collect();
        assert_eq!(events[0]["kind"], "log");
        assert!(events[0]["text"].as_str().unwrap().contains("skipped"));
        assert_eq!(events[1]["kind"], "turn_end");
        assert_eq!(events.len(), 2);
    }

    fn scratch(name: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("seatbelt-chat-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    /// A stand-in `seatbelt` that writes a line to stderr, records its arguments, then plays
    /// a Claude Code session: it answers `initialize` with two models, reads a model change
    /// and one message, asks to run a tool, and ends the turn once answered.
    fn start(dir: &Path) -> (Chats, u32, mpsc::Receiver<Value>) {
        use std::os::unix::fs::PermissionsExt;
        let script = format!(
            r#"#!/bin/sh
echo "$@" > "{args}"
printf '\033[1mseatbelt\033[0m recording claude on this machine\n' >&2
read -r init
echo "$init" > "{got}"
read -r settings
echo "$settings" >> "{got}"
echo '{{"type":"control_response","response":{{"subtype":"success","request_id":"seatbelt-1","response":{{"models":[{{"value":"default","displayName":"Default","supportsEffort":true,"supportedEffortLevels":["low","high"]}},{{"value":"haiku","displayName":"Haiku"}}]}}}}}}'
read -r chosen
echo "$chosen" >> "{got}"
read -r first
echo "$first" >> "{got}"
echo '{{"type":"control_request","request_id":"r1","request":{{"subtype":"can_use_tool","tool_name":"Bash","input":{{"command":"ls"}},"tool_use_id":"t1"}}}}'
read -r answer
echo "$answer" >> "{got}"
echo '{{"type":"result","subtype":"success","is_error":false,"result":"ok"}}'
read -r rest
printf '{{"run": "claude-1", "recorded": true}}' > "$SEATBELT_RUN_REPORT"
"#,
            args = dir.join("args").display(),
            got = dir.join("got").display(),
        );
        let seatbelt = dir.join("seatbelt");
        std::fs::write(&seatbelt, script).unwrap();
        std::fs::set_permissions(&seatbelt, std::fs::Permissions::from_mode(0o755)).unwrap();
        let (tx, rx) = mpsc::channel();
        let events = Channel::new(move |body| {
            if let InvokeResponseBody::Json(json) = body {
                let _ = tx.send(serde_json::from_str(&json).unwrap());
            }
            Ok(())
        });
        let chats = Chats::default();
        let id = chats
            .open(
                Launch {
                    seatbelt: &seatbelt,
                    cli: "claude",
                    exe: Path::new("/opt/claude"),
                    cwd: dir,
                    search_path: OsStr::new("/usr/bin:/bin"),
                    report: dir.join("report.json"),
                    resume: None,
                    choice: None,
                    mode: None,
                },
                events,
            )
            .unwrap();
        (chats, id, rx)
    }

    /// A chat's events as the test reads them. The log (stderr) and the protocol (stdout) are
    /// read by threads of their own, so their events can come in either order: one passed
    /// over while waiting for another kind is kept for when it is asked for.
    struct Events {
        rx: mpsc::Receiver<Value>,
        kept: Vec<Value>,
    }

    impl Events {
        fn next(&mut self, kind: &str) -> Value {
            if let Some(at) = self.kept.iter().position(|e| e["kind"] == kind) {
                return self.kept.remove(at);
            }
            loop {
                let event = self.rx.recv_timeout(Duration::from_secs(10)).expect(kind);
                if event["kind"] == kind {
                    return event;
                }
                self.kept.push(event);
            }
        }
    }

    #[test]
    fn a_chat_runs_through_seatbelt_and_an_approval_goes_back_to_the_cli() {
        let dir = scratch("approve");
        let (chats, id, rx) = start(&dir);
        let mut rx = Events { rx, kept: vec![] };
        assert_eq!(
            rx.next("log")["text"],
            "seatbelt recording claude on this machine"
        );
        let models = rx.next("models");
        assert_eq!(
            (models["models"][1]["id"].as_str(), models["model"].as_str()),
            (Some("haiku"), None)
        );
        let haiku = |effort: Option<&str>| Choice {
            model: "haiku".into(),
            effort: effort.map(str::to_string),
        };
        assert!(chats.choose(id, haiku(Some("low"))).is_err()); // Haiku takes no effort here
        chats.choose(id, haiku(None)).unwrap();
        assert_eq!(rx.next("models")["model"], "haiku");
        chats.send(id, "hello").unwrap();
        let ask = rx.next("approval");
        assert_eq!(
            (ask["id"].as_str(), ask["detail"].as_str()),
            (Some("r1"), Some("ls"))
        );
        chats.answer(id, "r1", true).unwrap();
        assert_eq!(rx.next("turn_end")["ok"], true);
        assert_eq!(chats.running(), 1);
        chats.close(id).unwrap(); // closes its input: the stand-in's last read ends
        let exit = rx.next("exit");
        assert_eq!(
            (exit["code"].as_i64(), exit["report"]["run"].as_str()),
            (Some(0), Some("claude-1"))
        );
        assert_eq!(chats.running(), 0);
        let args = std::fs::read_to_string(dir.join("args")).unwrap();
        assert!(args.starts_with("run --exe /opt/claude claude -- -p --input-format stream-json"));
        let got = std::fs::read_to_string(dir.join("got")).unwrap();
        let lines: Vec<Value> = got
            .lines()
            .map(|l| serde_json::from_str(l).unwrap())
            .collect();
        assert_eq!(lines[0]["request"]["subtype"], "initialize");
        assert_eq!(lines[1]["request"]["subtype"], "get_settings");
        assert_eq!(
            lines[2]["request"],
            serde_json::json!({"subtype": "set_model", "model": "haiku"})
        );
        assert_eq!(lines[3]["message"]["content"], "hello");
        assert_eq!(lines[4]["response"]["response"]["behavior"], "allow");
        assert!(chats.send(id, "again").is_err());
        let _ = std::fs::remove_dir_all(dir);
    }
}
