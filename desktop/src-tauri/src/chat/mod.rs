//! Chats: each is `seatbelt run --exe <path> <cli> -- <headless arguments>` with pipes instead
//! of a terminal, so it is recorded exactly as a terminal session is. seatbelt says its own
//! lines on stderr; stdout carries only the CLI's protocol, which a driver turns into chat
//! events. The window can send a message, answer an approval, choose a model the CLI offers,
//! interrupt, or end the chat; it never writes to the CLI itself.

mod acp;
mod claude;
mod codex;
pub mod protocol;

use std::collections::HashMap;
use std::ffi::OsStr;
use std::io::{BufRead, BufReader, Read, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::atomic::{AtomicU32, Ordering};
use std::sync::{Arc, Mutex, MutexGuard};
use std::thread;
use std::time::{Duration, Instant};

use serde_json::Value;
use tauri::ipc::Channel;

use crate::pty::{take_report, CLOSE_WAIT, RUN_REPORT_ENV};
use protocol::{ChatEvent, Choice, Driver, Step};

/// How long an ended chat's CLI has, once its input is closed, before `seatbelt run` is asked
/// to end it as closing a terminal would.
const EOF_WAIT: Duration = Duration::from_secs(3);
/// The longest line a CLI may print: anything longer is dropped, not buffered without end.
const MAX_LINE: usize = 32 * 1024 * 1024;

/// The driver for `cli`, resuming conversation `resume` (a plain id: see `conversation_id`),
/// on the model and effort `choice` if the CLI offers it.
fn driver(cli: &str, resume: Option<&str>, choice: Option<Choice>) -> Option<Box<dyn Driver>> {
    match cli {
        "claude" => Some(Box::new(claude::Claude::new(resume, choice))),
        "codex" => Some(Box::new(codex::Codex::new(resume, choice))),
        "gemini" => Some(Box::new(acp::Acp::new(resume, choice))),
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
}

struct Session {
    driver: Box<dyn Driver>,
    stdin: Option<ChildStdin>,
    events: Channel<ChatEvent>,
}

impl Session {
    /// Show the step's events and write its lines, in that order.
    fn apply(&mut self, step: Step) {
        for event in step.events {
            let _ = self.events.send(event);
        }
        for line in step.write {
            let wrote = self.stdin.as_mut().map(|stdin| {
                let mut text = line.to_string();
                text.push('\n');
                stdin
                    .write_all(text.as_bytes())
                    .and_then(|()| stdin.flush())
            });
            if let Some(Err(e)) = wrote {
                let _ = self.events.send(ChatEvent::Log {
                    text: format!("could not write to the CLI: {e}"),
                });
            }
        }
    }
}

struct Chat {
    session: Arc<Mutex<Session>>,
    child: Arc<Mutex<Child>>,
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
        let mut driver = driver(launch.cli, resume, launch.choice).ok_or("no chat for that CLI")?;
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
        let start = driver.start(&launch.cwd.to_string_lossy());
        let session = Arc::new(Mutex::new(Session {
            driver,
            stdin: child.stdin.take(),
            events: events.clone(),
        }));
        lock(&session).apply(start);
        let id = self.next.fetch_add(1, Ordering::Relaxed) + 1;
        let child = Arc::new(Mutex::new(child));
        lock(&self.open).insert(
            id,
            Chat {
                session: Arc::clone(&session),
                child: Arc::clone(&child),
            },
        );

        let reading = Arc::clone(&session);
        let out = thread::spawn(move || read_protocol(stdout, &reading));
        let log = events.clone();
        let err = thread::spawn(move || read_log(stderr, &log));
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
            let _ = out.join(); // the pipes close when seatbelt and the CLI have both ended
            let _ = err.join();
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
        let (session, child) = {
            let open = lock(&self.open);
            let chat = open.get(&id).ok_or("that chat has ended")?;
            (Arc::clone(&chat.session), Arc::clone(&chat.child))
        };
        lock(&session).stdin = None;
        let open = Arc::clone(&self.open);
        thread::spawn(move || {
            let ended = |until: Instant| {
                while Instant::now() < until {
                    if !lock(&open).contains_key(&id) {
                        return true;
                    }
                    thread::sleep(Duration::from_millis(100));
                }
                false
            };
            if ended(Instant::now() + EOF_WAIT) {
                return;
            }
            let pid = lock(&child).id();
            if terminate(pid) && ended(Instant::now() + CLOSE_WAIT) {
                return;
            }
            let _ = lock(&child).kill();
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
        let mut session = lock(session);
        if buf.len() > MAX_LINE {
            // the rest of this line is skipped, so the next read starts on a line of its own
            let mut rest = Vec::new();
            let _ = reader.read_until(b'\n', &mut rest);
            let _ = session.events.send(ChatEvent::Log {
                text: "a line over 32 MiB from the CLI was skipped".into(),
            });
            continue;
        }
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
