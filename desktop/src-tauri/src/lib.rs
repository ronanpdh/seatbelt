//! Seatbelt desktop: Claude Code, Codex and Gemini CLI in tabs, each run through
//! `seatbelt run`, so each is recorded and keeps its own sign-in.
//!
//! The web view can do only what these commands allow: open a tab for one of the three CLIs
//! in a folder; write to, resize or close a tab; list runs; open a run's page by its id; pick
//! a folder. It never names a program to run or a file to open.
//! Design: docs/plans/2026-10-09-desktop-terminal-design.md.

mod pty;
mod runs;
mod tools;

use std::path::PathBuf;
use std::sync::Mutex;
use std::thread;
use std::time::{SystemTime, UNIX_EPOCH};

use tauri::ipc::Channel;
use tauri::{Manager, State, WindowEvent};
use tauri_plugin_dialog::{DialogExt, MessageDialogButtons, MessageDialogKind};
use tauri_plugin_opener::OpenerExt;

use crate::pty::{Launch, TabEvent, Tabs, CLOSE_WAIT};
use crate::tools::Tools;

struct AppState {
    tabs: Tabs,
    tools: Mutex<Tools>,
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

#[tauri::command]
async fn pick_folder(app: tauri::AppHandle) -> Option<String> {
    let picked = app.dialog().file().blocking_pick_folder()?;
    picked
        .into_path()
        .ok()
        .map(|p| p.to_string_lossy().into_owned())
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
    if !tools::known_clis().contains_key(cli.as_str()) {
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
        .cli(&cli)
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
    let launch = Launch {
        seatbelt: &seatbelt.path,
        cli: &cli,
        exe: &exe,
        cwd: &cwd,
        search_path: &found.search_path,
        report: reports.join(format!("{}-{stamp}.json", std::process::id())),
        cols,
        rows,
    };
    state.tabs.open(launch, events)
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

/// Quitting while tabs run asks first, then ends each tab as closing it would.
fn confirm_quit(window: &tauri::Window) {
    let app = window.app_handle().clone();
    thread::spawn(move || {
        let state = app.state::<AppState>();
        let n = state.tabs.running();
        let quit = app
            .dialog()
            .message(format!(
                "{n} session{} still running. Quitting ends {} and closes {} run{}.",
                if n == 1 { " is" } else { "s are" },
                if n == 1 { "it" } else { "them" },
                if n == 1 { "its" } else { "their" },
                if n == 1 { "" } else { "s" },
            ))
            .title("Quit Seatbelt?")
            .kind(MessageDialogKind::Warning)
            .buttons(MessageDialogButtons::OkCancelCustom(
                "Quit".into(),
                "Cancel".into(),
            ))
            .blocking_show();
        if quit {
            state.tabs.close_all(CLOSE_WAIT);
            app.exit(0);
        }
    });
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_dialog::init())
        .manage(AppState {
            tabs: Tabs::default(),
            tools: Mutex::new(tools::discover()),
        })
        .on_window_event(|window, event| {
            if let WindowEvent::CloseRequested { api, .. } = event {
                if window.state::<AppState>().tabs.running() > 0 {
                    api.prevent_close();
                    confirm_quit(window);
                }
            }
        })
        .invoke_handler(tauri::generate_handler![
            status,
            pick_folder,
            open_tab,
            write_tab,
            resize_tab,
            close_tab,
            list_runs,
            open_page,
        ])
        .run(tauri::generate_context!())
        .expect("error while running the Seatbelt app");
}
