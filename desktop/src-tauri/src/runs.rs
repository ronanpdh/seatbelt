//! Runs, as seatbelt reports them: the list, one run's detail, its verification, and usage
//! across runs. All of it is what `seatbelt ... --json` prints; the app never reads a ledger.
//!
//! A run is named by its id. The web view never names a file: a ledger or page path comes
//! from seatbelt's own listing, and must be directly in the runs folder seatbelt names.

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

/// What the run's page shows, after the checks `seatbelt reconstruct` makes.
pub fn detail(tools: &Tools, id: &str) -> Result<Value, String> {
    let ledger = ledger_of(&list(tools)?, id)?;
    seatbelt_json(tools, &["reconstruct", &ledger.to_string_lossy(), "--json"])
}

/// `seatbelt verify` on the run, against this machine's key.
pub fn verify(tools: &Tools, id: &str) -> Result<Value, String> {
    let ledger = ledger_of(&list(tools)?, id)?;
    seatbelt_json(tools, &["verify", &ledger.to_string_lossy(), "--json"])
}

/// Usage across this machine's runs, as `seatbelt report` counts it.
pub fn usage(tools: &Tools) -> Result<Value, String> {
    seatbelt_json(tools, &["report", "--json"])
}

fn run<'a>(listing: &'a Value, id: &str) -> Result<(&'a Path, &'a Value), String> {
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
    Ok((Path::new(folder), run))
}

/// The ledger of the run with this id, checked to be a `.jsonl` file in the runs folder.
pub fn ledger_of(listing: &Value, id: &str) -> Result<PathBuf, String> {
    let (folder, run) = run(listing, id)?;
    let ledger = run
        .get("ledger")
        .and_then(Value::as_str)
        .ok_or("this run has no ledger")?;
    checked(folder, Path::new(ledger), "jsonl").map_err(|e| e.replace("page", "ledger"))
}

/// The page of the run with this id, checked to be an HTML file in the runs folder.
pub fn page_of(listing: &Value, id: &str) -> Result<PathBuf, String> {
    let (folder, run) = run(listing, id)?;
    let page = run
        .get("page")
        .and_then(Value::as_str)
        .ok_or("this run has no page")?;
    checked(folder, Path::new(page), "html")
}

/// `file`, if it is a file ending in `.{ext}` directly in `folder`.
fn checked(folder: &Path, file: &Path, ext: &str) -> Result<PathBuf, String> {
    let folder = folder.canonicalize().map_err(|e| e.to_string())?;
    let file = file
        .canonicalize()
        .map_err(|e| format!("the page is missing: {e}"))?;
    let named = file
        .extension()
        .is_some_and(|found| found.eq_ignore_ascii_case(ext));
    if !named || file.parent() != Some(folder.as_path()) || !file.is_file() {
        return Err("not a run's page".into());
    }
    Ok(file)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn folder(test: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("seatbelt-{test}-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn a_page_is_found_by_run_id_and_must_be_html_in_the_runs_folder() {
        let dir = folder("pages");
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

    #[test]
    fn a_ledger_is_found_by_run_id_and_must_be_jsonl_in_the_runs_folder() {
        let dir = folder("ledgers");
        let ledger = dir.join("r1.jsonl");
        std::fs::write(&ledger, "{}").unwrap();
        std::fs::write(dir.join("r1.html"), "x").unwrap();
        let listing = json!({
            "folder": dir,
            "runs": [
                {"id": "r1", "ledger": ledger},
                {"id": "r2", "ledger": dir.join("r1.html")},
                {"id": "r3", "ledger": "/etc/passwd"},
                {"id": "r4"},
            ]
        });
        assert_eq!(
            ledger_of(&listing, "r1").unwrap(),
            ledger.canonicalize().unwrap()
        );
        assert_eq!(ledger_of(&listing, "r2").unwrap_err(), "not a run's ledger");
        assert!(ledger_of(&listing, "r3").is_err());
        assert_eq!(
            ledger_of(&listing, "r4").unwrap_err(),
            "this run has no ledger"
        );
        let _ = std::fs::remove_dir_all(dir);
    }
}
