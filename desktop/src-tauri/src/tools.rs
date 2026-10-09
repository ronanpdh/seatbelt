//! Finding `seatbelt` and the CLIs it records: the user's own installed copies, never bundled.
//!
//! An app started from the Dock or the Start menu may not see the `PATH` the user's shell has
//! (design check K5), so the search path is the app's own `PATH`, then, on Unix, the one the
//! user's login shell prints, then the folders installers commonly use.

use std::collections::BTreeMap;
use std::ffi::OsString;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::mpsc;
use std::thread;
use std::time::Duration;

use serde::Serialize;

/// The CLIs a tab can run, as `seatbelt run` names them, and how the app shows them.
pub const CLIS: [(&str, &str); 3] = [
    ("claude", "Claude Code"),
    ("codex", "Codex"),
    ("gemini", "Gemini CLI"),
];

/// How long the login shell has to print its `PATH`: a slow profile must not hang the app.
const SHELL_WAIT: Duration = Duration::from_secs(5);
const MARK: &str = "__SEATBELT_PATH__";

#[derive(Clone, Debug, Serialize)]
pub struct Seatbelt {
    pub path: PathBuf,
    pub version: String,
    /// `seatbelt runs --json` works: the installed release has what the app needs
    pub supported: bool,
}

#[derive(Clone, Debug, Serialize)]
pub struct Cli {
    pub name: &'static str,
    pub label: &'static str,
    pub path: Option<PathBuf>,
}

#[derive(Clone, Debug, Serialize)]
pub struct Tools {
    #[serde(skip)]
    pub search_path: OsString,
    pub seatbelt: Option<Seatbelt>,
    pub clis: Vec<Cli>,
    pub home: Option<PathBuf>,
}

impl Tools {
    pub fn cli(&self, name: &str) -> Option<&Cli> {
        self.clis.iter().find(|c| c.name == name)
    }
}

/// Look everything up again: a tool installed while the app runs is found on the next call.
pub fn discover() -> Tools {
    let search_path = search_path();
    let seatbelt = which_in("seatbelt", &search_path).map(|path| inspect(&path, &search_path));
    let clis = CLIS
        .iter()
        .map(|(name, label)| Cli {
            name,
            label,
            path: which_in(name, &search_path),
        })
        .collect();
    Tools {
        search_path,
        seatbelt,
        clis,
        home: home_dir(),
    }
}

fn which_in(name: &str, search_path: &OsString) -> Option<PathBuf> {
    let cwd = std::env::current_dir().unwrap_or_else(|_| PathBuf::from("/"));
    which::which_in(name, Some(search_path), cwd).ok()
}

fn inspect(path: &Path, search_path: &OsString) -> Seatbelt {
    let run = |args: &[&str]| {
        Command::new(path)
            .args(args)
            .env("PATH", search_path)
            .stdin(Stdio::null())
            .stderr(Stdio::null())
            .output()
            .ok()
            .filter(|out| out.status.success())
            .map(|out| String::from_utf8_lossy(&out.stdout).trim().to_string())
    };
    let version = run(&["version"]).unwrap_or_default();
    let supported = run(&["runs", "--json", "--limit", "1"])
        .and_then(|out| serde_json::from_str::<serde_json::Value>(&out).ok())
        .is_some_and(|v| v.get("runs").is_some());
    Seatbelt {
        path: path.to_path_buf(),
        version,
        supported,
    }
}

pub fn home_dir() -> Option<PathBuf> {
    let var = if cfg!(windows) { "USERPROFILE" } else { "HOME" };
    std::env::var_os(var).map(PathBuf::from)
}

/// The app's `PATH`, then the login shell's, then common install folders; each folder once.
pub fn search_path() -> OsString {
    let mut dirs: Vec<PathBuf> = Vec::new();
    if let Some(own) = std::env::var_os("PATH") {
        dirs.extend(std::env::split_paths(&own));
    }
    if let Some(shell) = login_shell_path() {
        dirs.extend(std::env::split_paths(&shell));
    }
    dirs.extend(common_dirs());
    let mut seen = std::collections::HashSet::new();
    dirs.retain(|d| !d.as_os_str().is_empty() && seen.insert(d.clone()));
    std::env::join_paths(dirs).unwrap_or_default()
}

fn common_dirs() -> Vec<PathBuf> {
    let mut dirs = Vec::new();
    if let Some(home) = home_dir() {
        // uv tool, pipx, npm --prefix ~/.local, and Claude Code's own installer
        dirs.push(home.join(".local").join("bin"));
        dirs.push(home.join(".claude").join("local"));
    }
    if cfg!(target_os = "macos") {
        dirs.push(PathBuf::from("/opt/homebrew/bin"));
        dirs.push(PathBuf::from("/usr/local/bin"));
    }
    dirs
}

#[cfg(unix)]
fn login_shell_path() -> Option<OsString> {
    let shell = std::env::var_os("SHELL").unwrap_or_else(|| OsString::from("/bin/sh"));
    // -l reads the login profile, -i the interactive one (where many put PATH); the markers
    // keep whatever a profile prints out of the answer
    let script = format!("printf '{MARK}%s{MARK}' \"$PATH\"");
    let mut child = Command::new(&shell)
        .args(["-l", "-i", "-c", &script])
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .ok()?;
    let stdout = child.stdout.take()?;
    let (tx, rx) = mpsc::channel();
    thread::spawn(move || {
        let mut out = String::new();
        let _ = std::io::Read::read_to_string(&mut { stdout }, &mut out);
        let _ = tx.send(out);
    });
    let out = rx.recv_timeout(SHELL_WAIT).ok();
    let _ = child.kill();
    let _ = child.wait();
    parse_marked(&out?).map(OsString::from)
}

#[cfg(not(unix))]
fn login_shell_path() -> Option<OsString> {
    None // Windows apps get the user's PATH from the system, not from a shell profile
}

fn parse_marked(out: &str) -> Option<String> {
    let start = out.find(MARK)? + MARK.len();
    let len = out[start..].find(MARK)?;
    Some(out[start..start + len].to_string()).filter(|p| !p.is_empty())
}

/// The CLIs a tab may run, by name; anything else is refused before a process starts.
pub fn known_clis() -> BTreeMap<&'static str, &'static str> {
    CLIS.iter().copied().collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_marked_path_is_taken_from_whatever_a_profile_prints() {
        let out = format!("welcome!\n{MARK}/usr/bin:/opt/x{MARK}\nbye");
        assert_eq!(parse_marked(&out).as_deref(), Some("/usr/bin:/opt/x"));
        assert_eq!(parse_marked("no marks here"), None);
        assert_eq!(parse_marked(&format!("{MARK}{MARK}")), None);
    }

    #[test]
    fn the_search_path_lists_each_folder_once() {
        let path = search_path();
        let dirs: Vec<_> = std::env::split_paths(&path).collect();
        let unique: std::collections::HashSet<_> = dirs.iter().collect();
        assert_eq!(dirs.len(), unique.len());
    }

    #[test]
    fn only_the_three_clis_are_known() {
        let known = known_clis();
        assert_eq!(
            known.keys().copied().collect::<Vec<_>>(),
            ["claude", "codex", "gemini"]
        );
    }
}
