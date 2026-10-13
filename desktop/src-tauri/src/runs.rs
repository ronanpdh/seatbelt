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

/// Make the run's PDF with `seatbelt reconstruct --pdf`, after the checks it makes, beside
/// the ledger where seatbelt puts it and `erase` looks, and say where it is. When seatbelt
/// makes none (a ledger that does not verify, or no `pdf` extra), its own words say why.
pub fn make_pdf(tools: &Tools, id: &str) -> Result<PathBuf, String> {
    let folder = folder(tools)?;
    let ledger = file_of(&folder, id, "jsonl")?;
    let seatbelt = tools.seatbelt.as_ref().ok_or("seatbelt is not installed")?;
    let out = Command::new(&seatbelt.path)
        .arg("reconstruct")
        .arg(&ledger)
        .arg("--pdf")
        .env("PATH", &tools.search_path)
        .stdin(Stdio::null())
        .output()
        .map_err(|e| format!("could not run seatbelt: {e}"))?;
    if !out.status.success() {
        return Err(failed(&out.stdout, &out.stderr));
    }
    file_of(&folder, id, "pdf")
}

/// Why seatbelt made no PDF: the last thing it said, as one line.
fn failed(stdout: &[u8], stderr: &[u8]) -> String {
    let said = [stderr, stdout]
        .iter()
        .map(|out| {
            String::from_utf8_lossy(out)
                .split_whitespace()
                .collect::<Vec<_>>()
                .join(" ")
        })
        .find(|text| !text.is_empty())
        .unwrap_or_default();
    format!("seatbelt made no PDF: {said}")
}

/// A run's ledger (`jsonl`), page (`html`) or PDF (`pdf`): `<id>.<ext>` directly in the runs
/// folder, as
/// seatbelt names them (a run's id is its ledger's name). An id that could name anything
/// else is no run's; so is a file that resolves outside the folder.
pub fn file_of(folder: &Path, id: &str, ext: &str) -> Result<PathBuf, String> {
    let plain = !id.is_empty() && !id.starts_with('.') && !id.contains(['/', '\\', '\0']);
    if !plain {
        return Err("no such run".into());
    }
    let what = match ext {
        "html" => "page",
        "pdf" => "PDF",
        _ => "ledger",
    };
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
        assert_eq!(
            file_of(&dir, "r1", "pdf").unwrap_err(),
            "this run has no PDF"
        );
        std::fs::write(dir.join("r1.pdf"), "%PDF-1.7").unwrap();
        assert_eq!(
            file_of(&dir, "r1", "pdf").unwrap(),
            dir.join("r1.pdf").canonicalize().unwrap()
        );
        let _ = std::fs::remove_file(outside);
        let _ = std::fs::remove_dir_all(dir);
    }

    #[test]
    fn why_no_pdf_is_seatbelts_own_last_words_on_one_line() {
        let extra = b"a PDF needs the pdf extra: pip install\n  'seatbelt-ai[pdf]'\n";
        assert_eq!(
            failed(extra, b""),
            "seatbelt made no PDF: a PDF needs the pdf extra: pip install 'seatbelt-ai[pdf]'"
        );
        assert_eq!(
            failed(b"ignored", b"chain broken at seq 2"),
            "seatbelt made no PDF: chain broken at seq 2"
        );
    }
}
