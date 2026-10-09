//! The runs list: what `seatbelt runs --json` says, never ledgers read by the app itself.
//!
//! A run's page is opened by the run's id. The web view never names a file: the path comes
//! from seatbelt, and must be an `.html` file directly in the runs folder seatbelt names.

use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};

use serde_json::Value;

use crate::tools::Tools;

/// How many runs the list shows, newest first.
pub const LIMIT: u32 = 200;

pub fn list(tools: &Tools) -> Result<Value, String> {
    let seatbelt = tools.seatbelt.as_ref().ok_or("seatbelt is not installed")?;
    let out = Command::new(&seatbelt.path)
        .args(["runs", "--json", "--limit", &LIMIT.to_string()])
        .env("PATH", &tools.search_path)
        .stdin(Stdio::null())
        .output()
        .map_err(|e| format!("could not run seatbelt: {e}"))?;
    if !out.status.success() {
        let err = String::from_utf8_lossy(&out.stderr);
        let said = String::from_utf8_lossy(&out.stdout);
        let why = if err.trim().is_empty() {
            said.trim()
        } else {
            err.trim()
        };
        return Err(format!("seatbelt runs failed: {why}"));
    }
    serde_json::from_slice(&out.stdout).map_err(|e| format!("seatbelt runs: {e}"))
}

/// The page of the run with this id, checked to be an HTML file in the runs folder.
pub fn page_of(listing: &Value, id: &str) -> Result<PathBuf, String> {
    let folder = listing
        .get("folder")
        .and_then(Value::as_str)
        .ok_or("seatbelt runs named no folder")?;
    let run = listing
        .get("runs")
        .and_then(Value::as_array)
        .and_then(|runs| {
            runs.iter()
                .find(|r| r.get("id").and_then(Value::as_str) == Some(id))
        })
        .ok_or("no such run")?;
    let page = run
        .get("page")
        .and_then(Value::as_str)
        .ok_or("this run has no page")?;
    checked_page(Path::new(folder), Path::new(page))
}

fn checked_page(folder: &Path, page: &Path) -> Result<PathBuf, String> {
    let folder = folder.canonicalize().map_err(|e| e.to_string())?;
    let page = page
        .canonicalize()
        .map_err(|e| format!("the page is missing: {e}"))?;
    let is_html = page
        .extension()
        .is_some_and(|ext| ext.eq_ignore_ascii_case("html"));
    if !is_html || page.parent() != Some(folder.as_path()) || !page.is_file() {
        return Err("not a run's page".into());
    }
    Ok(page)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn folder() -> PathBuf {
        let dir = std::env::temp_dir().join(format!("seatbelt-runs-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn a_page_is_found_by_run_id_and_must_be_html_in_the_runs_folder() {
        let dir = folder();
        let page = dir.join("r1.html");
        std::fs::write(&page, "<!doctype html>").unwrap();
        std::fs::write(dir.join("r1.jsonl"), "{}").unwrap();
        let outside = std::env::temp_dir().join(format!("outside-{}.html", std::process::id()));
        std::fs::write(&outside, "x").unwrap();
        let listing = json!({
            "folder": dir,
            "runs": [
                {"id": "r1", "page": page},
                {"id": "r2", "page": null},
                {"id": "r3", "page": dir.join("r1.jsonl")},
                {"id": "r4", "page": outside},
                {"id": "r5", "page": dir.join("..").join(dir.file_name().unwrap()).join("r1.html")},
            ]
        });
        assert_eq!(
            page_of(&listing, "r1").unwrap(),
            page.canonicalize().unwrap()
        );
        assert_eq!(page_of(&listing, "r2").unwrap_err(), "this run has no page");
        assert_eq!(page_of(&listing, "r3").unwrap_err(), "not a run's page");
        assert_eq!(page_of(&listing, "r4").unwrap_err(), "not a run's page");
        assert!(page_of(&listing, "r5").is_ok()); // the same file by another spelling
        assert_eq!(page_of(&listing, "nope").unwrap_err(), "no such run");
        let _ = std::fs::remove_file(outside);
        let _ = std::fs::remove_dir_all(dir);
    }
}
