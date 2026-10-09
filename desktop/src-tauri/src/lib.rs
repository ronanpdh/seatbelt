//! Seatbelt desktop: Claude Code, Codex and Gemini CLI in tabs, each run through
//! `seatbelt run`, so each is recorded and keeps its own sign-in.
//!
//! The web view can do only what these commands allow: open a chat or a terminal tab for one
//! of the three CLIs in a folder; send a chat message, answer its approvals, interrupt or end
//! it; write to, resize or close a tab; list runs; show, verify or open the page of a run by
//! its id; report usage; open an http(s) link from a reply in the browser; check a folder;
//! quit. It never names a program to run or a file to open, and never writes to a CLI's
//! protocol itself.
//!
//! No native dialogs: macOS's `+[NSOpenPanel openPanel]` can return nil (a code-signature
//! mismatch after an in-place update is one reported cause), and the binding the dialog plugin
//! uses panics on the main thread when it does (tauri-apps/tauri#13047). A folder is typed or dropped instead, and the quit question is
//! asked in the window.
//! Design: docs/plans/2026-10-09-desktop-terminal-design.md.

mod chat;
mod pty;
mod runs;
mod tools;

use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::time::{SystemTime, UNIX_EPOCH};

use tauri::ipc::Channel;
use tauri::{Emitter, Manager, RunEvent, State, WindowEvent};
use tauri_plugin_opener::OpenerExt;

use crate::chat::protocol::ChatEvent;
use crate::chat::Chats;
use crate::pty::{Launch, TabEvent, Tabs, CLOSE_WAIT};
use crate::tools::Tools;

struct AppState {
    tabs: Tabs,
    chats: Chats,
    tools: Mutex<Tools>,
}

/// What a session needs to start: seatbelt, the CLI's path, the folder, the search path, and
/// a fresh file for its run report. Everything is checked here; the web view names only a
/// CLI from the fixed list and a folder.
struct Start {
    seatbelt: PathBuf,
    exe: PathBuf,
    cwd: PathBuf,
    search_path: std::ffi::OsString,
    report: PathBuf,
}

fn prepare(
    app: &tauri::AppHandle,
    state: &AppState,
    cli: &str,
    cwd: String,
) -> Result<Start, String> {
    if !tools::known_clis().contains_key(cli) {
        return Err(format!("{cli} is not one of the CLIs seatbelt records"));
    }
    let found = state.tools();
    let seatbelt = found
        .seatbelt
        .as_ref()
        .ok_or("seatbelt is not installed: uv tool install seatbelt-ai")?;
    if !seatbelt.supported {
        return Err(format!(
            "seatbelt {} is too old for this app: uv tool upgrade seatbelt-ai",
            seatbelt.version
        ));
    }
    let exe = found
        .cli(cli)
        .and_then(|c| c.path.clone())
        .ok_or_else(|| format!("{cli} is not installed, or not on your PATH"))?;
    let cwd = PathBuf::from(cwd);
    if !cwd.is_absolute() || !cwd.is_dir() {
        return Err(format!("{} is not a folder", cwd.display()));
    }
    let reports = app
        .path()
        .app_cache_dir()
        .map_err(|e| e.to_string())?
        .join("reports");
    std::fs::create_dir_all(&reports).map_err(|e| e.to_string())?;
    let stamp = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_nanos())
        .unwrap_or_default();
    Ok(Start {
        seatbelt: seatbelt.path.clone(),
        exe,
        cwd,
        search_path: found.search_path.clone(),
        report: reports.join(format!("{}-{stamp}.json", std::process::id())),
    })
}

impl AppState {
    fn tools(&self) -> Tools {
        self.tools
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .clone()
    }
}

/// Look for seatbelt and the CLIs again, and say what was found.
#[tauri::command]
async fn status(state: State<'_, AppState>) -> Result<Tools, String> {
    let found = tauri::async_runtime::spawn_blocking(tools::discover)
        .await
        .map_err(|e| e.to_string())?;
    *state
        .tools
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner) = found.clone();
    Ok(found)
}

/// The folder a session would start in, as typed or dropped: `~` is the home folder, and the
/// answer is the folder's full path, or why it cannot be used.
#[tauri::command]
fn check_folder(path: String) -> Result<String, String> {
    folder(&path, tools::home_dir().as_deref())
}

fn folder(typed: &str, home: Option<&Path>) -> Result<String, String> {
    let typed = typed.trim();
    let path = match (typed.strip_prefix('~'), home) {
        (Some(""), Some(home)) => home.to_path_buf(),
        (Some(rest), Some(home)) if rest.starts_with(['/', '\\']) => home.join(&rest[1..]),
        _ => PathBuf::from(typed),
    };
    if typed.is_empty() || !path.is_absolute() {
        return Err("type a full path, such as ~/code/project, or drop a folder here".into());
    }
    let full = path
        .canonicalize()
        .map_err(|_| format!("{typed} does not exist"))?;
    if !full.is_dir() {
        return Err(format!("{typed} is a file, not a folder"));
    }
    Ok(full.to_string_lossy().into_owned())
}

#[tauri::command]
fn open_tab(
    app: tauri::AppHandle,
    state: State<'_, AppState>,
    cli: String,
    cwd: String,
    cols: u16,
    rows: u16,
    events: Channel<TabEvent>,
) -> Result<u32, String> {
    let start = prepare(&app, &state, &cli, cwd)?;
    let launch = Launch {
        seatbelt: &start.seatbelt,
        cli: &cli,
        exe: &start.exe,
        cwd: &start.cwd,
        search_path: &start.search_path,
        report: start.report,
        cols,
        rows,
    };
    state.tabs.open(launch, events)
}

/// Start a chat: the CLI's headless session, through `seatbelt run`.
#[tauri::command]
fn open_chat(
    app: tauri::AppHandle,
    state: State<'_, AppState>,
    cli: String,
    cwd: String,
    events: Channel<ChatEvent>,
) -> Result<u32, String> {
    let start = prepare(&app, &state, &cli, cwd)?;
    let launch = chat::Launch {
        seatbelt: &start.seatbelt,
        cli: &cli,
        exe: &start.exe,
        cwd: &start.cwd,
        search_path: &start.search_path,
        report: start.report,
    };
    state.chats.open(launch, events)
}

#[tauri::command]
fn chat_send(state: State<'_, AppState>, id: u32, text: String) -> Result<(), String> {
    state.chats.send(id, &text)
}

#[tauri::command]
fn chat_answer(
    state: State<'_, AppState>,
    id: u32,
    request: String,
    allow: bool,
) -> Result<(), String> {
    state.chats.answer(id, &request, allow)
}

#[tauri::command]
fn chat_interrupt(state: State<'_, AppState>, id: u32) -> Result<(), String> {
    state.chats.interrupt(id)
}

#[tauri::command]
fn close_chat(state: State<'_, AppState>, id: u32) -> Result<(), String> {
    state.chats.close(id)
}

#[tauri::command]
fn write_tab(state: State<'_, AppState>, id: u32, data: String) -> Result<(), String> {
    state.tabs.write(id, &data)
}

#[tauri::command]
fn resize_tab(state: State<'_, AppState>, id: u32, cols: u16, rows: u16) -> Result<(), String> {
    state.tabs.resize(id, cols, rows)
}

#[tauri::command]
fn close_tab(state: State<'_, AppState>, id: u32) -> Result<(), String> {
    state.tabs.close(id)
}

#[tauri::command]
async fn list_runs(state: State<'_, AppState>) -> Result<serde_json::Value, String> {
    let found = state.tools();
    tauri::async_runtime::spawn_blocking(move || runs::list(&found))
        .await
        .map_err(|e| e.to_string())?
}

/// What the run's page shows, for the run viewer.
#[tauri::command]
async fn run_detail(state: State<'_, AppState>, id: String) -> Result<serde_json::Value, String> {
    let found = state.tools();
    tauri::async_runtime::spawn_blocking(move || runs::detail(&found, &id))
        .await
        .map_err(|e| e.to_string())?
}

/// `seatbelt verify` on one run: its chain, completeness and signature.
#[tauri::command]
async fn verify_run(state: State<'_, AppState>, id: String) -> Result<serde_json::Value, String> {
    let found = state.tools();
    tauri::async_runtime::spawn_blocking(move || runs::verify(&found, &id))
        .await
        .map_err(|e| e.to_string())?
}

/// Usage across this machine's runs, by model, person and tool.
#[tauri::command]
async fn usage_report(state: State<'_, AppState>) -> Result<serde_json::Value, String> {
    let found = state.tools();
    tauri::async_runtime::spawn_blocking(move || runs::usage(&found))
        .await
        .map_err(|e| e.to_string())?
}

/// Open a run's page in the system's default browser, outside the app's web view.
#[tauri::command]
async fn open_page(
    app: tauri::AppHandle,
    state: State<'_, AppState>,
    id: String,
) -> Result<(), String> {
    let found = state.tools();
    let page = tauri::async_runtime::spawn_blocking(move || {
        runs::list(&found).and_then(|listing| runs::page_of(&listing, &id))
    })
    .await
    .map_err(|e| e.to_string())??;
    app.opener()
        .open_path(page.to_string_lossy(), None::<&str>)
        .map_err(|e| e.to_string())
}

/// Open a link from a reply in the system's browser: only `http` and `https`, and nothing
/// with spaces or control characters in it. The window never follows a link itself.
#[tauri::command]
fn open_link(app: tauri::AppHandle, url: String) -> Result<(), String> {
    let url = web_url(&url)?;
    app.opener()
        .open_url(url, None::<&str>)
        .map_err(|e| e.to_string())
}

fn web_url(url: &str) -> Result<&str, String> {
    let lower = url.to_ascii_lowercase();
    let web = (lower.starts_with("https://") || lower.starts_with("http://"))
        && url.len() <= 4096
        && url.len() > "https://".len()
        && !url.chars().any(|c| c.is_whitespace() || c.is_control());
    if web {
        Ok(url)
    } else {
        Err("only http and https links open".into())
    }
}

/// End every chat and tab as closing it would, then quit: the answer to "quit-requested".
#[tauri::command]
async fn quit(app: tauri::AppHandle) -> Result<(), String> {
    let handle = app.clone();
    tauri::async_runtime::spawn_blocking(move || {
        let state = handle.state::<AppState>();
        std::thread::scope(|scope| {
            scope.spawn(|| state.chats.close_all(CLOSE_WAIT));
            state.tabs.close_all(CLOSE_WAIT);
        });
    })
    .await
    .map_err(|e| e.to_string())?;
    app.exit(0);
    Ok(())
}

/// Quitting while sessions run (closing the window, or ⌘Q) is held, and the window asks.
fn hold_quit(app: &tauri::AppHandle) -> bool {
    let state = app.state::<AppState>();
    let running = state.tabs.running() + state.chats.running();
    if running > 0 {
        let _ = app.emit("quit-requested", running);
    }
    running > 0
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .manage(AppState {
            tabs: Tabs::default(),
            chats: Chats::default(),
            tools: Mutex::new(tools::discover()),
        })
        .on_window_event(|window, event| {
            if let WindowEvent::CloseRequested { api, .. } = event {
                if hold_quit(window.app_handle()) {
                    api.prevent_close();
                }
            }
        })
        .invoke_handler(tauri::generate_handler![
            status,
            check_folder,
            open_tab,
            write_tab,
            resize_tab,
            close_tab,
            open_chat,
            chat_send,
            chat_answer,
            chat_interrupt,
            close_chat,
            list_runs,
            run_detail,
            verify_run,
            usage_report,
            open_page,
            open_link,
            quit,
        ])
        .build(tauri::generate_context!())
        .expect("error while building the Seatbelt app")
        .run(|app, event| {
            // ⌘Q and the app menu's Quit ask too; `quit` itself exits with a code, and passes
            if let RunEvent::ExitRequested {
                code: None, api, ..
            } = event
            {
                if hold_quit(app) {
                    api.prevent_exit();
                }
            }
        });
}

#[cfg(test)]
mod tests {
    use super::{folder, web_url};

    #[test]
    fn only_web_links_open() {
        assert!(web_url("https://example.com/a?b=c").is_ok());
        assert!(web_url("HTTP://example.com").is_ok());
        for bad in [
            "file:///etc/passwd",
            "javascript:alert(1)",
            "https://",
            "https://a b",
            "https://a\nb",
            "ftp://x",
            "/tmp/x",
        ] {
            assert!(web_url(bad).is_err(), "{bad}");
        }
    }

    #[test]
    fn a_folder_is_a_full_path_to_a_directory_with_tilde_for_home() {
        let dir = std::env::temp_dir().join(format!("seatbelt-folder-{}", std::process::id()));
        std::fs::create_dir_all(dir.join("project")).unwrap();
        std::fs::write(dir.join("notes.txt"), "x").unwrap();
        let full = dir.join("project").canonicalize().unwrap();
        let typed = dir.join("project");
        assert_eq!(
            folder(typed.to_str().unwrap(), None).unwrap(),
            full.to_string_lossy()
        );
        assert_eq!(
            folder("~/project", Some(&dir)).unwrap(),
            full.to_string_lossy()
        );
        assert_eq!(
            folder("~", Some(&dir)).unwrap(),
            dir.canonicalize().unwrap().to_string_lossy()
        );
        assert!(folder("relative/path", Some(&dir))
            .unwrap_err()
            .contains("full path"));
        assert!(folder("  ", Some(&dir)).unwrap_err().contains("full path"));
        assert!(folder("~/nope", Some(&dir))
            .unwrap_err()
            .contains("does not exist"));
        assert!(folder("~/notes.txt", Some(&dir))
            .unwrap_err()
            .contains("not a folder"));
        let _ = std::fs::remove_dir_all(&dir);
    }
}
