//! The Settings view: what this machine has, checked again on request. seatbelt's own checks
//! are `seatbelt doctor --json`; each CLI says its version and, where it can, whether it is
//! signed in, by its own commands. Signing in again is the CLI's own login, in a terminal.

use std::ffi::OsStr;
use std::path::Path;
use std::process::{Command, Stdio};
use std::sync::mpsc;
use std::thread;
use std::time::{Duration, Instant};

use serde::Serialize;
use serde_json::Value;

use crate::tools::{Tools, CLIS};

/// How long a CLI has to say its version or whether it is signed in: one that hangs (on the
/// network, say) must not hold the view.
const ASK_WAIT: Duration = Duration::from_secs(15);

#[derive(Debug, Serialize)]
pub struct CliStatus {
    pub name: &'static str,
    pub label: &'static str,
    pub path: Option<String>,
    pub version: Option<String>,
    /// What the CLI says; None when it has no command that says
    pub signed_in: Option<bool>,
    /// The CLI's answer, in words, or why there is none
    pub sign_in: String,
    /// What "Sign in again" runs, as the user would type it
    pub login: String,
    /// What to know before trusting the answer or signing in again, if anything
    pub note: Option<&'static str>,
}

/// What a CLI printed, within ASK_WAIT: whether it exited 0, its output and its error output.
/// One still running then is killed, and None.
fn ask(exe: &Path, args: &[&str], search_path: &OsStr) -> Option<(bool, String, String)> {
    let mut child = Command::new(exe)
        .args(args)
        .env("PATH", search_path)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .ok()?;
    let read = |pipe: Option<Box<dyn std::io::Read + Send>>| {
        let (tx, rx) = mpsc::channel();
        thread::spawn(move || {
            let mut text = String::new();
            if let Some(mut pipe) = pipe {
                let _ = pipe.read_to_string(&mut text);
            }
            let _ = tx.send(text);
        });
        rx
    };
    let out = read(child.stdout.take().map(|p| Box::new(p) as _));
    let err = read(child.stderr.take().map(|p| Box::new(p) as _));
    let deadline = Instant::now() + ASK_WAIT;
    let status = loop {
        match child.try_wait() {
            Ok(Some(status)) => break status,
            Ok(None) if Instant::now() < deadline => thread::sleep(Duration::from_millis(50)),
            _ => {
                let _ = child.kill();
                let _ = child.wait();
                return None;
            }
        }
    };
    // a process it started may still hold its output open: what came by then is the answer
    let text =
        |rx: mpsc::Receiver<String>| rx.recv_timeout(Duration::from_secs(1)).unwrap_or_default();
    Some((status.success(), text(out), text(err)))
}

/// The last line a CLI printed that says something and is not a warning: a line inside a box
/// it drew (as seatbelt's own errors are) without the box.
fn said(out: &str, err: &str) -> String {
    const BOX: &[char] = &['│', '╭', '╮', '╰', '╯', '─'];
    [err, out]
        .iter()
        .flat_map(|text| text.lines())
        .map(|line| line.trim().trim_matches(BOX).trim())
        .rfind(|line| line.chars().any(char::is_alphanumeric) && !line.starts_with("WARNING"))
        .unwrap_or_default()
        .to_string()
}

/// Claude Code's `auth status --json`, in words: what Claude Code says of its sign-in. Nothing
/// here tries the sign-in with a request to a model.
fn claude_signed_in(json: &str) -> (Option<bool>, String) {
    let Ok(answer) = serde_json::from_str::<Value>(json) else {
        return (
            None,
            "Claude Code gave no status it could be read from".into(),
        );
    };
    let logged_in = answer.get("loggedIn").and_then(Value::as_bool);
    let method = match answer.get("authMethod").and_then(Value::as_str) {
        Some("oauth_token") => "a Claude account (OAuth)".to_string(),
        Some("api_key") => match answer.get("apiKeySource").and_then(Value::as_str) {
            Some(source) => format!("an API key ({source})"),
            None => "an API key".to_string(),
        },
        Some(other) => other.to_string(),
        None => "a sign-in it does not name".to_string(),
    };
    match logged_in {
        Some(true) => (Some(true), format!("Signed in with {method}")),
        Some(false) => (Some(false), "Not signed in".into()),
        None => (
            None,
            "Claude Code did not say whether it is signed in".into(),
        ),
    }
}

/// What "Sign in again" runs for `cli`, after the CLI's own name: its login command, or none
/// for a CLI that signs in only from its own interface (Gemini CLI, with `/auth`).
pub fn login_args(cli: &str) -> Option<&'static [&'static str]> {
    match cli {
        "claude" => Some(&["auth", "login"]),
        "codex" => Some(&["login"]),
        _ => None,
    }
}

/// What to know of `cli`'s answer or its login, from checks of the CLI itself (P18 to P22 in
/// docs/plans/2026-10-09-desktop-terminal-design.md).
fn note(cli: &str) -> Option<&'static str> {
    match cli {
        "claude" => Some(
            "This is what Claude Code says of its sign-in: the app does not try it with a \
             request to a model. If a chat says Claude Code could not sign in, sign in again.",
        ),
        "codex" => Some(
            "Codex's login removes its stored sign-in as soon as it starts: stopping it before \
             it finishes leaves Codex signed out.",
        ),
        _ => None,
    }
}

/// Where `cli` is, its version, and whether it is signed in, as it says.
pub fn cli_status(tools: &Tools, cli: &str) -> Result<CliStatus, String> {
    let (name, label) = CLIS
        .iter()
        .find(|(name, _)| *name == cli)
        .copied()
        .ok_or("no such CLI")?;
    let login = match login_args(name) {
        Some(args) => format!("{name} {}", args.join(" ")),
        None => format!("seatbelt run {name}, then /auth in its own interface"),
    };
    let Some(exe) = tools.cli(name).and_then(|c| c.path.clone()) else {
        return Ok(CliStatus {
            name,
            label,
            path: None,
            version: None,
            signed_in: None,
            sign_in: "Not found".into(),
            login,
            note: note(name),
        });
    };
    let search = tools.search_path.as_os_str();
    let version = ask(&exe, &["--version"], search)
        .filter(|(ok, ..)| *ok)
        .map(|(_, out, err)| said(&out, &err))
        .filter(|v| !v.is_empty());
    let (signed_in, sign_in) = match name {
        "claude" => match ask(&exe, &["auth", "status", "--json"], search) {
            Some((_, out, _)) => claude_signed_in(&out),
            None => (None, "Claude Code did not answer in time".into()),
        },
        // `codex login status` exits 0 when signed in, and says how on its error output
        "codex" => match ask(&exe, &["login", "status"], search) {
            Some((ok, out, err)) => (Some(ok), said(&out, &err)),
            None => (None, "Codex did not answer in time".into()),
        },
        _ => (
            None,
            "Gemini CLI has no command that says; sign in from its own interface with /auth".into(),
        ),
    };
    Ok(CliStatus {
        name,
        label,
        path: Some(exe.to_string_lossy().into_owned()),
        version,
        signed_in,
        sign_in,
        login,
        note: note(name),
    })
}

/// `seatbelt doctor --json`: its version, runs folder, key, config, and whether it can write
/// a PDF.
pub fn doctor(tools: &Tools) -> Result<Value, String> {
    let seatbelt = tools.seatbelt.as_ref().ok_or("seatbelt is not installed")?;
    let (_, out, err) = ask(
        &seatbelt.path,
        &["doctor", "--json"],
        tools.search_path.as_os_str(),
    )
    .ok_or("seatbelt did not answer in time")?;
    serde_json::from_str::<Value>(&out).map_err(|_| {
        if err.contains("No such command") {
            "this seatbelt has no doctor command: uv tool upgrade seatbelt-ai".to_string()
        } else {
            format!("seatbelt doctor failed: {}", said(&out, &err))
        }
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn claude_codes_status_in_words() {
        let oauth = r#"{"loggedIn":true,"authMethod":"oauth_token","apiProvider":"firstParty"}"#;
        assert_eq!(
            claude_signed_in(oauth),
            (Some(true), "Signed in with a Claude account (OAuth)".into())
        );
        let key = r#"{"loggedIn":true,"authMethod":"api_key","apiKeySource":"ANTHROPIC_API_KEY"}"#;
        assert_eq!(
            claude_signed_in(key),
            (
                Some(true),
                "Signed in with an API key (ANTHROPIC_API_KEY)".into()
            )
        );
        assert_eq!(
            claude_signed_in(r#"{"loggedIn":false}"#),
            (Some(false), "Not signed in".into())
        );
        assert_eq!(claude_signed_in("not json").0, None);
    }

    #[test]
    fn a_clis_answer_is_its_last_line_that_is_not_a_warning() {
        let err =
            "WARNING: proceeding, even though we could not create PATH aliases\nNot logged in\n";
        assert_eq!(said("", err), "Not logged in");
        assert_eq!(said("2.1.295 (Claude Code)\n", ""), "2.1.295 (Claude Code)");
        // what seatbelt 0.6.0 printed for a command it does not have
        let boxed = "Usage: seatbelt [OPTIONS] COMMAND [ARGS]...\n\
            ╭─ Error ─────────╮\n│ No such command 'doctorx'. Did you mean 'doctor'? │\n╰─────────╯\n";
        assert_eq!(
            said("", boxed),
            "No such command 'doctorx'. Did you mean 'doctor'?"
        );
    }

    /// A stand-in program in `dir`: a shell script.
    #[cfg(unix)]
    fn program(dir: &Path, name: &str, script: &str) -> std::path::PathBuf {
        use std::os::unix::fs::PermissionsExt;
        let path = dir.join(name);
        std::fs::write(&path, format!("#!/bin/sh\n{script}\n")).unwrap();
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o755)).unwrap();
        path
    }

    #[cfg(unix)]
    fn tools_with(dir: &Path, seatbelt: Option<std::path::PathBuf>) -> Tools {
        Tools {
            search_path: std::env::var_os("PATH").unwrap_or_default(),
            seatbelt: seatbelt.map(|path| crate::tools::Seatbelt {
                path,
                version: "0.6.0".into(),
                supported: true,
                problem: None,
            }),
            clis: CLIS
                .iter()
                .map(|(name, label)| crate::tools::Cli {
                    name,
                    label,
                    path: Some(dir.join(name)).filter(|p| p.exists()),
                })
                .collect(),
            home: None,
        }
    }

    #[cfg(unix)]
    #[test]
    fn each_cli_is_asked_by_its_own_commands() {
        let dir = std::env::temp_dir().join(format!("seatbelt-settings-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        // the answers Claude Code 2.1.295 and Codex 0.162.0 gave (P18, P19)
        program(
            &dir,
            "claude",
            r#"case "$*" in
  --version) echo '2.1.295 (Claude Code)' ;;
  'auth status --json') echo '{"loggedIn": true, "authMethod": "api_key", "apiKeySource": "ANTHROPIC_API_KEY"}' ;;
esac"#,
        );
        program(
            &dir,
            "codex",
            r#"case "$*" in
  --version) echo 'codex-cli 0.162.0' ;;
  'login status') echo 'WARNING: proceeding' >&2; echo 'Not logged in' >&2; exit 1 ;;
esac"#,
        );
        let tools = tools_with(&dir, None);
        let claude = cli_status(&tools, "claude").unwrap();
        assert_eq!(claude.version.as_deref(), Some("2.1.295 (Claude Code)"));
        assert_eq!(claude.signed_in, Some(true));
        assert_eq!(
            claude.sign_in,
            "Signed in with an API key (ANTHROPIC_API_KEY)"
        );
        assert_eq!(claude.login, "claude auth login");
        let codex = cli_status(&tools, "codex").unwrap();
        assert_eq!(codex.signed_in, Some(false));
        assert_eq!(codex.sign_in, "Not logged in");
        assert!(codex.note.unwrap().contains("signed out"));
        let gemini = cli_status(&tools, "gemini").unwrap();
        assert_eq!((gemini.path, gemini.signed_in), (None, None));
        assert_eq!(gemini.sign_in, "Not found");
        assert_eq!(
            gemini.login,
            "seatbelt run gemini, then /auth in its own interface"
        );
        assert!(cli_status(&tools, "bash").is_err());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[cfg(unix)]
    #[test]
    fn seatbelts_checks_are_its_doctor_and_an_older_one_is_told_to_upgrade() {
        let dir = std::env::temp_dir().join(format!("seatbelt-doctor-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let new = program(
            &dir,
            "seatbelt",
            r#"[ "$*" = "doctor --json" ] && echo '{"version": "0.6.0", "config": null}'"#,
        );
        assert_eq!(
            doctor(&tools_with(&dir, Some(new))).unwrap()["version"],
            "0.6.0"
        );
        let old = program(
            &dir,
            "seatbelt-old",
            r#"printf '%s\n' 'Usage: seatbelt [OPTIONS] COMMAND [ARGS]...' "╭─ Error ─╮" "│ No such command 'doctor'. │" "╰─────────╯" >&2; exit 2"#,
        );
        let said = doctor(&tools_with(&dir, Some(old))).unwrap_err();
        assert!(said.contains("upgrade"), "{said}");
        assert!(doctor(&tools_with(&dir, None)).is_err());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn sign_in_again_is_the_clis_own_login() {
        assert_eq!(login_args("claude"), Some(&["auth", "login"][..]));
        assert_eq!(login_args("codex"), Some(&["login"][..]));
        assert_eq!(login_args("gemini"), None);
    }

    #[cfg(unix)]
    #[test]
    fn a_cli_that_hangs_is_given_up_on() {
        let path = std::env::var_os("PATH").unwrap_or_default();
        let started = std::time::Instant::now();
        assert!(ask(Path::new("sleep"), &["60"], &path).is_none());
        assert!(started.elapsed() < Duration::from_secs(30));
        let (ok, out, _) = ask(Path::new("echo"), &["hi"], &path).unwrap();
        assert!(ok && out == "hi\n");
    }
}
