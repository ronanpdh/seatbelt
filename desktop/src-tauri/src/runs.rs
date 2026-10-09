//! Runs, as seatbelt reports them: the list, one run's detail, its verification, and usage
//! across runs. All of it is what `seatbelt ... --json` prints; the app never reads a ledger.
//!
//! A run is named by its id. The web view never names a file: a run's ledger or page is the
//! file of that id directly in the runs folder seatbelt names.

use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};

use serde_json::Value;

use crate::tools::Tools;

/// How many runs the list shows, newest first.
pub const LIMIT: u32 = 500;

/// Run seatbelt with `args` and read the JSON it prints. A command that fails still prints
/// JSON when it has a result to give (a broken ledger's verification, a report with broken
/// runs); `{"error": ...}` is its reason for giving none.
fn seatbelt_json(tools: &Tools, args: &[&str]) -> Result<Value, String> {
    let seatbelt = tools.seatbelt.as_ref().ok_or("seatbelt is not installed")?;
    let out = Command::new(&seatbelt.path)
        .args(args)
        .env("PATH", &tools.search_path)
        .stdin(Stdio::null())
        .output()
        .map_err(|e| format!("could not run seatbelt: {e}"))?;
    let what = args.first().copied().unwrap_or_default();
    match serde_json::from_slice::<Value>(&out.stdout) {
        Ok(value) => match value.get("error").and_then(Value::as_str) {
            Some(error) => Err(error.to_string()),
            None => Ok(value),
        },
        Err(_) => {
            let err = String::from_utf8_lossy(&out.stderr);
            let said = String::from_utf8_lossy(&out.stdout);
            let why = if err.trim().is_empty() {
                said.trim()
            } else {
                err.trim()
            };
            Err(format!("seatbelt {what} failed: {why}"))
        }
    }
}

pub fn list(tools: &Tools) -> Result<Value, String> {
    seatbelt_json(tools, &["runs", "--json", "--limit", &LIMIT.to_string()])
}

/// The runs folder seatbelt names: a listing of none of its runs, so none is checked.
pub fn folder(tools: &Tools) -> Result<PathBuf, String> {
    let listing = seatbelt_json(tools, &["runs", "--json", "--limit", "0"])?;
    listing
        .get("folder")
        .and_then(Value::as_str)
        .map(PathBuf::from)
        .ok_or_else(|| "seatbelt runs named no folder".to_string())
}

/// What the run's page shows, after the checks `seatbelt reconstruct` makes.
pub fn detail(tools: &Tools, id: &str) -> Result<Value, String> {
    let ledger = file_of(&folder(tools)?, id, "jsonl")?;
    seatbelt_json(tools, &["reconstruct", &ledger.to_string_lossy(), "--json"])
}

/// `seatbelt verify` on the run, against this machine's key.
pub fn verify(tools: &Tools, id: &str) -> Result<Value, String> {
    let ledger = file_of(&folder(tools)?, id, "jsonl")?;
    seatbelt_json(tools, &["verify", &ledger.to_string_lossy(), "--json"])
}

pub fn usage(tools: &Tools) -> Result<Value, String> {
    seatbelt_json(tools, &["report", "--json"])
}

/// A run's ledger (`jsonl`) or page (`html`): `<id>.<ext>` directly in the runs folder, as
/// seatbelt names them (a run's id is its ledger's name). An id that could name anything
/// else is no run's; so is a file that resolves outside the folder.
pub fn file_of(folder: &Path, id: &str, ext: &str) -> Result<PathBuf, String> {
    let plain = !id.is_empty() && !id.starts_with('.') && !id.contains(['/', '\\', '\0']);
    if !plain {
        return Err("no such run".into());
    }
    let what = if ext == "html" { "page" } else { "ledger" };
    let folder = folder.canonicalize().map_err(|e| e.to_string())?;
    let file = folder
        .join(format!("{id}.{ext}"))
        .canonicalize()
        .map_err(|_| format!("this run has no {what}"))?;
    if file.parent() != Some(folder.as_path()) || !file.is_file() {
        return Err(format!("not a run's {what}"));
    }
    Ok(file)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn folder(test: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("seatbelt-{test}-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn a_runs_files_are_found_by_its_id_directly_in_the_runs_folder() {
        let dir = folder("files");
        std::fs::write(dir.join("r1.jsonl"), "{}").unwrap();
        std::fs::write(dir.join("r1.html"), "<!doctype html>").unwrap();
        let outside = std::env::temp_dir().join(format!("outside-{}.html", std::process::id()));
        std::fs::write(&outside, "x").unwrap();
        #[cfg(unix)]
        std::os::unix::fs::symlink(&outside, dir.join("r2.html")).unwrap();
        assert_eq!(
            file_of(&dir, "r1", "html").unwrap(),
            dir.join("r1.html").canonicalize().unwrap()
        );
        assert_eq!(
            file_of(&dir, "r1", "jsonl").unwrap(),
            dir.join("r1.jsonl").canonicalize().unwrap()
        );
        assert_eq!(
            file_of(&dir, "r9", "html").unwrap_err(),
            "this run has no page"
        );
        #[cfg(unix)]
        assert_eq!(file_of(&dir, "r2", "html").unwrap_err(), "not a run's page");
        for bad in ["", "../r1", "..", ".r1", "a/b", "a\\b", "r1\0"] {
            assert_eq!(
                file_of(&dir, bad, "html").unwrap_err(),
                "no such run",
                "{bad}"
            );
        }
        let _ = std::fs::remove_file(outside);
        let _ = std::fs::remove_dir_all(dir);
    }
}
