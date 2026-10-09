//! Tabs: each is a pseudo-terminal running `seatbelt run <cli>`, shown by xterm.js.
//!
//! The app adds no recording of its own: `seatbelt run` records the CLI exactly as it does in
//! any terminal, and writes a report of what it recorded (`SEATBELT_RUN_REPORT`) as it exits.

use std::collections::HashMap;
use std::ffi::OsStr;
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU32, Ordering};
use std::sync::{mpsc, Arc, Mutex, MutexGuard};
use std::thread;
use std::time::{Duration, Instant};

use portable_pty::{native_pty_system, ChildKiller, CommandBuilder, MasterPty, PtySize};
use serde::Serialize;
use tauri::ipc::Channel;

/// The file `seatbelt run` writes its report to (its `RUN_REPORT_ENV`).
pub const RUN_REPORT_ENV: &str = "SEATBELT_RUN_REPORT";
/// Tells `seatbelt run` to say only warnings and errors, and draw no belt.
pub const QUIET_ENV: &str = "SEATBELT_QUIET";
/// How long a closed tab's `seatbelt run` has to end its CLI and sign the run before it is
/// killed. seatbelt itself gives the CLI 10 seconds after passing the signal on.
pub const CLOSE_WAIT: Duration = Duration::from_secs(20);
/// How long a tab's output may keep coming after its process has exited.
const DRAIN_WAIT: Duration = Duration::from_secs(2);

#[derive(Clone, Debug, Serialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum TabEvent {
    Output {
        data: String,
    },
    Exit {
        code: Option<u32>,
        report: Option<serde_json::Value>,
    },
}

struct Tab {
    writer: Box<dyn Write + Send>,
    master: Box<dyn MasterPty + Send>,
    killer: Box<dyn ChildKiller + Send + Sync>,
    pid: Option<u32>,
}

/// What a tab runs: `seatbelt run --exe <exe> <cli>` in `cwd`.
pub struct Launch<'a> {
    pub seatbelt: &'a Path,
    pub cli: &'a str,
    pub exe: &'a Path,
    pub cwd: &'a Path,
    pub search_path: &'a OsStr,
    pub report: PathBuf,
    pub cols: u16,
    pub rows: u16,
    /// Arguments for the CLI, after `--`: those that resume a conversation, or none.
    pub args: Vec<String>,
}

#[derive(Default)]
pub struct Tabs {
    next: AtomicU32,
    open: Arc<Mutex<HashMap<u32, Tab>>>,
}

fn lock<T>(m: &Mutex<T>) -> MutexGuard<'_, T> {
    m.lock().unwrap_or_else(std::sync::PoisonError::into_inner)
}

fn size(cols: u16, rows: u16) -> PtySize {
    PtySize {
        rows: rows.max(2),
        cols: cols.max(10),
        pixel_width: 0,
        pixel_height: 0,
    }
}

impl Tabs {
    /// Start a tab. Its output and its end arrive on `events`.
    pub fn open(&self, launch: Launch<'_>, events: Channel<TabEvent>) -> Result<u32, String> {
        let pair = native_pty_system()
            .openpty(size(launch.cols, launch.rows))
            .map_err(|e| format!("could not open a terminal: {e}"))?;
        let mut cmd = CommandBuilder::new(launch.seatbelt);
        cmd.arg("run");
        cmd.arg("--exe");
        cmd.arg(launch.exe);
        cmd.arg(launch.cli);
        if !launch.args.is_empty() {
            cmd.arg("--");
            cmd.args(&launch.args);
        }
        cmd.cwd(launch.cwd);
        cmd.env("PATH", launch.search_path);
        cmd.env("TERM", "xterm-256color");
        cmd.env("COLORTERM", "truecolor");
        cmd.env(RUN_REPORT_ENV, &launch.report);
        // the app shows what the terminal is for: no belt, no lines that only inform
        cmd.env(QUIET_ENV, "1");
        let mut child = pair
            .slave
            .spawn_command(cmd)
            .map_err(|e| format!("could not start seatbelt: {e}"))?;
        drop(pair.slave); // the child holds it now; the reader sees the end when it exits
        let reader = pair.master.try_clone_reader().map_err(|e| e.to_string())?;
        let writer = pair.master.take_writer().map_err(|e| e.to_string())?;
        let id = self.next.fetch_add(1, Ordering::SeqCst) + 1;
        lock(&self.open).insert(
            id,
            Tab {
                writer,
                master: pair.master,
                killer: child.clone_killer(),
                pid: child.process_id(),
            },
        );
        let (drained, done) = mpsc::channel::<()>();
        let output = events.clone();
        thread::spawn(move || {
            pump(reader, &output);
            let _ = drained.send(());
        });
        let open = Arc::clone(&self.open);
        let report = launch.report;
        thread::spawn(move || {
            let code = child.wait().ok().map(|status| status.exit_code());
            let _ = done.recv_timeout(DRAIN_WAIT);
            lock(&open).remove(&id); // closes the terminal: a reader still blocked ends
            let _ = events.send(TabEvent::Exit {
                code,
                report: take_report(&report),
            });
        });
        Ok(id)
    }

    pub fn write(&self, id: u32, data: &str) -> Result<(), String> {
        let mut open = lock(&self.open);
        let tab = open.get_mut(&id).ok_or("that tab has ended")?;
        tab.writer
            .write_all(data.as_bytes())
            .and_then(|()| tab.writer.flush())
            .map_err(|e| e.to_string())
    }

    pub fn resize(&self, id: u32, cols: u16, rows: u16) -> Result<(), String> {
        let open = lock(&self.open);
        let tab = open.get(&id).ok_or("that tab has ended")?;
        tab.master
            .resize(size(cols, rows))
            .map_err(|e| e.to_string())
    }

    /// End a tab the way closing a terminal window should: `seatbelt run` gets SIGTERM, passes
    /// it on to the CLI, then closes and signs the run. A tab still running after CLOSE_WAIT
    /// is killed; its ledger is then closed by the next `seatbelt run`, as a killed run's is.
    pub fn close(&self, id: u32) -> Result<(), String> {
        let pid = lock(&self.open).get(&id).ok_or("that tab has ended")?.pid;
        if !terminate(pid) {
            return self.kill(id);
        }
        let open = Arc::clone(&self.open);
        thread::spawn(move || {
            let deadline = Instant::now() + CLOSE_WAIT;
            while Instant::now() < deadline {
                if !lock(&open).contains_key(&id) {
                    return;
                }
                thread::sleep(Duration::from_millis(100));
            }
            if let Some(tab) = lock(&open).get_mut(&id) {
                let _ = tab.killer.kill();
            }
        });
        Ok(())
    }

    fn kill(&self, id: u32) -> Result<(), String> {
        let mut open = lock(&self.open);
        let tab = open.get_mut(&id).ok_or("that tab has ended")?;
        tab.killer.kill().map_err(|e| e.to_string())
    }

    pub fn running(&self) -> usize {
        lock(&self.open).len()
    }

    /// Close every tab, and wait until they have all ended or `wait` has passed.
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
fn terminate(pid: Option<u32>) -> bool {
    let Some(pid) = pid.and_then(|p| libc::pid_t::try_from(p).ok()) else {
        return false;
    };
    // SAFETY: kill(2) with a pid this process started and SIGTERM; no memory is touched
    unsafe { libc::kill(pid, libc::SIGTERM) == 0 }
}

#[cfg(not(unix))]
fn terminate(_pid: Option<u32>) -> bool {
    false // Windows: how a ConPTY child should be asked to end is design check K4
}

/// Send the terminal's output to the web view as text, a whole character at a time.
fn pump(mut reader: Box<dyn Read + Send>, events: &Channel<TabEvent>) {
    let mut chunks = Utf8Chunks::default();
    let mut buf = [0u8; 16 * 1024];
    loop {
        match reader.read(&mut buf) {
            Ok(0) | Err(_) => break, // the end of the terminal (EIO on Linux once it closes)
            Ok(n) => {
                let data = chunks.push(&buf[..n]);
                if !data.is_empty() && events.send(TabEvent::Output { data }).is_err() {
                    break;
                }
            }
        }
    }
    let rest = chunks.finish();
    if !rest.is_empty() {
        let _ = events.send(TabEvent::Output { data: rest });
    }
}

/// The run's report, which `seatbelt run` wrote as it exited, read once and removed.
pub(crate) fn take_report(path: &Path) -> Option<serde_json::Value> {
    let data = std::fs::read(path).ok();
    let _ = std::fs::remove_file(path);
    serde_json::from_slice(&data?).ok()
}

/// Decodes UTF-8 that arrives in pieces: a character split across two reads is held until its
/// last byte comes, and bytes that are not UTF-8 become U+FFFD, as a terminal shows them.
#[derive(Default)]
pub struct Utf8Chunks {
    pending: Vec<u8>,
}

impl Utf8Chunks {
    pub fn push(&mut self, bytes: &[u8]) -> String {
        self.pending.extend_from_slice(bytes);
        let mut out = String::new();
        loop {
            match std::str::from_utf8(&self.pending) {
                Ok(text) => {
                    out.push_str(text);
                    self.pending.clear();
                    return out;
                }
                Err(e) => {
                    let valid = e.valid_up_to();
                    out.push_str(&String::from_utf8_lossy(&self.pending[..valid]));
                    match e.error_len() {
                        None => {
                            self.pending.drain(..valid); // an unfinished character: wait
                            return out;
                        }
                        Some(bad) => {
                            out.push('\u{FFFD}');
                            self.pending.drain(..valid + bad);
                        }
                    }
                }
            }
        }
    }

    pub fn finish(&mut self) -> String {
        let rest = String::from_utf8_lossy(&self.pending).into_owned();
        self.pending.clear();
        rest
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_character_split_across_reads_is_held_until_it_is_whole() {
        let mut chunks = Utf8Chunks::default();
        let text = "ok ✓ done";
        let bytes = text.as_bytes();
        let split = text.find('✓').unwrap() + 1; // inside the 3-byte check mark
        let first = chunks.push(&bytes[..split]);
        assert_eq!(first, "ok ");
        let second = chunks.push(&bytes[split..]);
        assert_eq!(first + &second, text);
        assert_eq!(chunks.finish(), "");
    }

    #[test]
    fn bytes_that_are_not_utf8_become_replacement_characters() {
        let mut chunks = Utf8Chunks::default();
        assert_eq!(chunks.push(b"a\xffb"), "a\u{FFFD}b");
        assert_eq!(chunks.push(b"\xe2\x9c"), ""); // unfinished, held
        assert_eq!(chunks.finish(), "\u{FFFD}");
    }

    /// A stand-in for `seatbelt` that says what it was given and writes the run report, run
    /// through a real pseudo-terminal by `Tabs`. `script` is the shell code after the header.
    #[cfg(unix)]
    fn fake_seatbelt(dir: &Path, script: &str) -> PathBuf {
        use std::os::unix::fs::PermissionsExt;
        let path = dir.join("seatbelt");
        std::fs::write(&path, format!("#!/bin/sh\n{script}\n")).unwrap();
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o755)).unwrap();
        path
    }

    #[cfg(unix)]
    fn start(dir: &Path, script: &str) -> (Tabs, u32, mpsc::Receiver<serde_json::Value>) {
        use tauri::ipc::InvokeResponseBody;
        let seatbelt = fake_seatbelt(dir, script);
        let (tx, rx) = mpsc::channel();
        let events = Channel::new(move |body| {
            if let InvokeResponseBody::Json(json) = body {
                let _ = tx.send(serde_json::from_str(&json).unwrap());
            }
            Ok(())
        });
        let tabs = Tabs::default();
        let launch = Launch {
            seatbelt: &seatbelt,
            cli: "claude",
            exe: Path::new("/opt/claude"),
            cwd: dir,
            search_path: OsStr::new("/usr/bin:/bin"),
            report: dir.join("report.json"),
            cols: 100,
            rows: 30,
            args: vec!["--resume".into(), "s-1".into()],
        };
        let id = tabs.open(launch, events).unwrap();
        (tabs, id, rx)
    }

    /// Everything the tab sent, until its exit event: (its output, the exit event).
    #[cfg(unix)]
    fn until_exit(rx: &mpsc::Receiver<serde_json::Value>) -> (String, serde_json::Value) {
        let mut output = String::new();
        loop {
            let event = rx
                .recv_timeout(Duration::from_secs(30))
                .expect("the tab never ended");
            match event["kind"].as_str() {
                Some("output") => output.push_str(event["data"].as_str().unwrap()),
                Some("exit") => return (output, event),
                other => panic!("unexpected event {other:?}"),
            }
        }
    }

    #[cfg(unix)]
    fn scratch(name: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("seatbelt-pty-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[cfg(unix)]
    #[test]
    fn a_tab_runs_seatbelt_run_in_a_terminal_and_hands_back_its_report() {
        let dir = scratch("run");
        let (tabs, _, rx) = start(
            &dir,
            r#"printf 'args: %s\n' "$*"
[ -t 0 ] && [ -t 1 ] && echo 'on a terminal'
echo "TERM=$TERM"
echo "QUIET=$SEATBELT_QUIET"
printf '{"run": "claude-1a2b", "recorded": true, "exit": 3}' > "$SEATBELT_RUN_REPORT"
exit 3"#,
        );
        let (output, exit) = until_exit(&rx);
        assert!(
            output.contains("args: run --exe /opt/claude claude -- --resume s-1"),
            "{output}"
        );
        assert!(output.contains("on a terminal"), "{output}");
        assert!(output.contains("TERM=xterm-256color"), "{output}");
        assert!(output.contains("QUIET=1"), "{output}");
        assert_eq!(exit["code"], 3);
        assert_eq!(exit["report"]["run"], "claude-1a2b");
        assert!(!dir.join("report.json").exists()); // read once, removed
        assert_eq!(tabs.running(), 0);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[cfg(unix)]
    #[test]
    fn keystrokes_reach_the_tab_and_closing_it_sends_sigterm() {
        let dir = scratch("close");
        let (tabs, id, rx) = start(
            &dir,
            r#"trap 'echo got TERM; printf "{\"run\": \"r\", \"exit\": 143}" > "$SEATBELT_RUN_REPORT"; exit 143' TERM
echo ready
read line
echo "typed: $line"
while :; do sleep 0.1; done"#,
        );
        let mut seen = String::new();
        while !seen.contains("ready") {
            let event = rx.recv_timeout(Duration::from_secs(10)).unwrap();
            seen.push_str(event["data"].as_str().unwrap_or_default());
        }
        tabs.write(id, "hello\r").unwrap();
        while !seen.contains("typed: hello") {
            let event = rx.recv_timeout(Duration::from_secs(10)).unwrap();
            seen.push_str(event["data"].as_str().unwrap_or_default());
        }
        tabs.resize(id, 120, 40).unwrap();
        tabs.close(id).unwrap();
        let (output, exit) = until_exit(&rx);
        assert!(output.contains("got TERM"), "{output}");
        assert_eq!(exit["report"]["exit"], 143);
        assert_eq!(tabs.running(), 0);
        assert_eq!(tabs.write(id, "x").unwrap_err(), "that tab has ended");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn a_report_is_read_once_and_removed() {
        let dir = std::env::temp_dir().join(format!("seatbelt-report-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("r.json");
        std::fs::write(&path, br#"{"run": "claude-1", "exit": 0}"#).unwrap();
        let report = take_report(&path).unwrap();
        assert_eq!(report["run"], "claude-1");
        assert!(!path.exists());
        assert!(take_report(&path).is_none());
        std::fs::write(&path, b"not json").unwrap();
        assert!(take_report(&path).is_none());
        let _ = std::fs::remove_dir_all(&dir);
    }
}
