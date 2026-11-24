//! Hardening for the files that hold a secret (slice 10 review): `-ipc-token-file`,
//! `PORTICO_SIP_PASSWORD_FILE` and `-secrets-env`. Go reads whatever it is pointed at, to the end
//! (`os.ReadFile`), so a path that is a device (`/dev/zero`) or a huge file would fill memory
//! before the first check; and it says nothing about a secret file anyone can read. This port
//! refuses what is not a regular file (a directory is let through to fail in `read` with Go's
//! text), refuses a file over a cap, and warns (never refuses, never prints content or path
//! contents) when group or others can read it. PARITY difference 186.

#[cfg(feature = "test-seams")]
use crate::util::lock;
use std::io::Read;
#[cfg(feature = "test-seams")]
use std::sync::Mutex;

/// `O_NONBLOCK` on Linux (x86_64 and aarch64 alike; `libc` is a dev-dependency only). Another
/// target has another value, so the build refuses to guess (R2-110).
#[cfg(not(target_os = "linux"))]
compile_error!("secretfile::O_NONBLOCK is the Linux value: take it from `libc` for another OS");
#[cfg(any(target_arch = "x86_64", target_arch = "aarch64"))]
const O_NONBLOCK: i32 = 0o4000;
#[cfg(not(any(target_arch = "x86_64", target_arch = "aarch64")))]
compile_error!("secretfile::O_NONBLOCK is 0o4000 on x86_64 and aarch64 only (it differs on alpha, mips, parisc, sparc)");

/// Largest token or password file (a secret is a few dozen bytes).
pub const MAX_SECRET_FILE: usize = 64 * 1024;
/// Largest `.env` for `-secrets-env`: above Go's 1 MiB line limit, so every line Go accepts is
/// accepted, and far below what could hurt.
pub const MAX_ENV_FILE: usize = 4 * 1024 * 1024;

/// The warnings issued so far, kept for the tests (at most 64).
#[cfg(feature = "test-seams")]
static WARNINGS: Mutex<Vec<String>> = Mutex::new(Vec::new());

/// Prints a fixed warning line on stderr (and, with the `test-seams` feature, remembers it).
pub fn warn(line: &str) {
    crate::panic_hook::err_line(line);
    #[cfg(feature = "test-seams")]
    {
        let mut g = lock(&WARNINGS);
        if g.len() < 64 {
            g.push(line.to_string());
        }
    }
}

/// Every warning issued so far (test seam).
#[cfg(feature = "test-seams")]
pub fn warnings() -> Vec<String> {
    lock(&WARNINGS).clone()
}

/// True when group or others have any permission on a file of this mode.
pub fn loose_mode(mode: u32) -> bool {
    mode & 0o077 != 0
}

/// Opens a secret file. Errors read like Go's (`open P: no such file or directory`). `what` names
/// the flag or variable in the warning (never the path's content).
pub fn open_secret_file(path: &str, what: &str) -> Result<std::fs::File, String> {
    use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};
    // O_NONBLOCK: opening a FIFO with no writer would block for ever before the type check below
    // could refuse it (R1-15); on a regular file the flag changes nothing.
    let file = std::fs::OpenOptions::new()
        .read(true)
        .custom_flags(O_NONBLOCK)
        .open(path)
        .map_err(|e| format!("open {path}: {}", crate::gojson::io_err(&e)))?;
    let meta = file
        .metadata()
        .map_err(|e| format!("open {path}: {}", crate::gojson::io_err(&e)))?;
    if !meta.is_file() && !meta.is_dir() {
        return Err(format!("open {path}: not a regular file"));
    }
    if meta.is_file() && loose_mode(meta.permissions().mode()) {
        warn(&format!(
            "portico-transport: warning: {what} is readable by group or others (mode {:04o}); restrict it to its owner",
            meta.permissions().mode() & 0o777
        ));
    }
    Ok(file)
}

/// Reads at most `cap` bytes; one more is an error.
pub fn read_capped(file: &mut std::fs::File, path: &str, cap: usize) -> Result<Vec<u8>, String> {
    let mut b = Vec::new();
    file.take(cap as u64 + 1)
        .read_to_end(&mut b)
        .map_err(|e| format!("read {path}: {}", crate::gojson::io_err(&e)))?;
    if b.len() > cap {
        return Err(format!("read {path}: the file is larger than {cap} bytes"));
    }
    Ok(b)
}
