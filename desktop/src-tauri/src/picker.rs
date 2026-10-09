//! The system's folder picker, for choosing the folder a session starts in.
//!
//! On macOS it is AppKit's own `choose folder`, run by `osascript` in a process of its own:
//! the app's process does not open an NSOpenPanel, which can come back nil there (a code
//! signature changed by an in-place rebuild is one reported cause) and the Rust binding then
//! panics (tauri-apps/tauri#13047). On Windows and Linux it is Tauri's dialog plugin, called
//! from Rust only, so the web view gets no dialog permission.

use std::path::Path;

/// What the picker said: a folder, or nothing when it was cancelled.
pub type Picked = Result<Option<String>, String>;

/// AppKit's `choose folder`, by `osascript`. The start folder is an argument to the script's
/// run handler, not part of the script, so nothing in a path is run.
const SCRIPT: [&str; 10] = [
    "on run argv",
    "tell current application",
    "activate",
    "set prompt_text to \"Choose the folder this session starts in\"",
    "if (count of argv) > 0 then",
    "return POSIX path of (choose folder with prompt prompt_text default location ((item 1 of argv) as POSIX file))",
    "end if",
    "return POSIX path of (choose folder with prompt prompt_text)",
    "end tell",
    "end run",
];

#[cfg_attr(not(target_os = "macos"), allow(dead_code))]
fn chooser(start: Option<&Path>) -> std::process::Command {
    let mut cmd = std::process::Command::new("/usr/bin/osascript");
    for line in SCRIPT {
        cmd.arg("-e").arg(line);
    }
    if let Some(start) = start {
        cmd.arg(start);
    }
    cmd.stdin(std::process::Stdio::null());
    cmd
}

#[cfg(target_os = "macos")]
pub fn pick(_app: &tauri::AppHandle, start: Option<&Path>) -> Picked {
    let out = chooser(start)
        .output()
        .map_err(|e| format!("could not open the folder picker: {e}"))?;
    answer(
        out.status.success(),
        &String::from_utf8_lossy(&out.stdout),
        &String::from_utf8_lossy(&out.stderr),
    )
}

#[cfg(not(target_os = "macos"))]
pub fn pick(app: &tauri::AppHandle, start: Option<&Path>) -> Picked {
    use tauri_plugin_dialog::DialogExt;
    let mut dialog = app
        .dialog()
        .file()
        .set_title("Choose the folder this session starts in");
    if let Some(start) = start {
        dialog = dialog.set_directory(start);
    }
    match dialog.blocking_pick_folder() {
        None => Ok(None),
        Some(path) => path
            .into_path()
            .map(|p| Some(p.to_string_lossy().into_owned()))
            .map_err(|e| e.to_string()),
    }
}

/// `osascript`'s answer: the folder's POSIX path on success; nothing when the user cancelled
/// (AppleScript error -128); otherwise its message.
#[cfg_attr(not(target_os = "macos"), allow(dead_code))]
fn answer(ok: bool, stdout: &str, stderr: &str) -> Picked {
    if ok {
        let path = stdout.trim_end_matches(['\n', '\r']);
        let path = match path.strip_suffix('/') {
            Some(trimmed) if !trimmed.is_empty() => trimmed,
            _ => path,
        };
        return Ok((!path.is_empty()).then(|| path.to_string()));
    }
    if stderr.contains("(-128)") {
        return Ok(None);
    }
    Err(format!("the folder picker failed: {}", stderr.trim()))
}

#[cfg(test)]
mod tests {
    use super::{answer, chooser};
    use std::path::Path;

    #[test]
    fn the_macos_chooser_passes_the_start_folder_as_an_argument_not_as_script() {
        let cmd = chooser(Some(Path::new("/Users/rh/a \" & do shell script \"x")));
        let args: Vec<_> = cmd
            .get_args()
            .map(|a| a.to_string_lossy().into_owned())
            .collect();
        assert_eq!(cmd.get_program(), "/usr/bin/osascript");
        assert_eq!(args.iter().filter(|a| *a == "-e").count(), 10);
        assert_eq!(args[1], "on run argv");
        assert_eq!(args.last().unwrap(), "/Users/rh/a \" & do shell script \"x");
        assert_eq!(chooser(None).get_args().count(), 20);
    }

    #[test]
    fn the_pickers_answer_is_a_folder_nothing_when_cancelled_or_its_error() {
        assert_eq!(
            answer(true, "/Users/rh/harness/\n", ""),
            Ok(Some("/Users/rh/harness".into()))
        );
        assert_eq!(answer(true, "/\n", ""), Ok(Some("/".into())));
        assert_eq!(
            answer(false, "", "0:120: execution error: User canceled. (-128)\n"),
            Ok(None)
        );
        assert!(answer(false, "", "boom").unwrap_err().contains("boom"));
        assert_eq!(answer(true, "\n", ""), Ok(None));
    }
}
