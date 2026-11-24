//! `portico-transport` entry point: flags, IPC server, signals, exit codes, as in
//! `transport/cmd/portico-transport/main.go`. Go prints everything on stderr as
//! `portico-transport: <message>` with no timestamp (it uses `fmt`, not the `log` package), and
//! so does this.

#![cfg_attr(not(test), deny(clippy::print_stdout, clippy::print_stderr))]

use std::process::exit;
use std::sync::Arc;

use tokio::signal::unix::{signal, SignalKind};
use transport_rs::flags::{self, Parsed};
use transport_rs::{dispatch, secretsenv, standalone};

fn fail(msg: &str) -> ! {
    transport_rs::panic_hook::err_line(&format!("portico-transport: {msg}"));
    exit(1);
}

fn main() {
    transport_rs::panic_hook::install();
    let mut argv = std::env::args_os();
    let argv0 = argv
        .next()
        .map(|a| a.to_string_lossy().into_owned())
        .unwrap_or_default();
    // R1-72: Go keeps the bytes of a path flag; a lossy conversion would silently make a
    // different file (U+FFFD), so an argument that is not UTF-8 is refused by name.
    let args: Vec<String> = argv
        .map(|a| {
            a.into_string().unwrap_or_else(|bad| {
                transport_rs::panic_hook::err_line(&format!(
                    "portico-transport: an argument is not valid UTF-8 (this build cannot keep its bytes): {}",
                    bad.to_string_lossy()
                ));
                exit(2)
            })
        })
        .collect();
    let f = match flags::parse(&args) {
        Parsed::Ok(f) => f,
        Parsed::Help => {
            transport_rs::panic_hook::err_text(&flags::usage(&argv0));
            exit(0);
        }
        Parsed::Err(m) => {
            transport_rs::panic_hook::err_line(&m);
            transport_rs::panic_hook::err_text(&flags::usage(&argv0));
            exit(2);
        }
    };

    // `-secrets-env` is read first, as in Go: the file's values fill in for unset variables.
    let process: Arc<standalone::Getenv> = Arc::new(standalone::process_getenv);
    let getenv: Arc<standalone::Getenv> = if f.secrets_env.is_empty() {
        process
    } else {
        secretsenv::with_secrets_env(&f.secrets_env, process).unwrap_or_else(|e| fail(&e))
    };

    let rt = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .unwrap_or_else(|e| fail(&format!("tokio runtime: {e}")));
    let code = guarded(|| rt.block_on(run(f, getenv)));
    exit(code);
}

/// R1-71: an unrecovered panic on the main thread ends the process with 2, the code of Go's
/// runtime (`panic_hook` has already printed the scrubbed report), not Rust's 101.
fn guarded(f: impl FnOnce() -> i32) -> i32 {
    std::panic::catch_unwind(std::panic::AssertUnwindSafe(f)).unwrap_or(2)
}

async fn run(f: flags::Flags, getenv: Arc<standalone::Getenv>) -> i32 {
    // openIPC
    let (srv, tcp_ipc) = standalone::open_ipc(&f, &*getenv).unwrap_or_else(|e| fail(&e));
    // `-host` is the address a relay-mode device leg advertises in its SDP (dahua_sip).
    let hub = dispatch::register(
        &srv,
        dispatch::HubOptions {
            house_calls: f.house_calls,
            our_host: f.host.clone(),
            ..dispatch::HubOptions::default()
        },
    );
    // `-sip-from-env`: one gate_sip_server from PORTICO_SIP_* variables, started through the
    // real configure path before the first hub connection is accepted.
    if f.sip_from_env {
        let started = match standalone::sip_config_from_env(&*getenv) {
            Ok(cfg) => hub.preload_sip(cfg).await,
            Err(e) => Err(e),
        };
        if let Err(e) = started {
            fail(&format!("-sip-from-env: {e}"));
        }
    }

    let mut sigint =
        signal(SignalKind::interrupt()).unwrap_or_else(|e| fail(&format!("signal: {e}")));
    let mut sigterm =
        signal(SignalKind::terminate()).unwrap_or_else(|e| fail(&format!("signal: {e}")));
    let (s2, h2) = (srv.clone(), hub.clone());
    tokio::spawn(async move {
        tokio::select! {
            _ = sigint.recv() => {}
            _ = sigterm.recv() => {}
        }
        // shutdown() (stop every driver and wait for it), then srv.Close().
        h2.shutdown().await;
        s2.close().await;
    });

    let res = if tcp_ipc {
        // standalone: HA reconnects, the process keeps running until it is closed
        standalone::serve_reconnects(&srv, transport_rs::panic_hook::err_line).await;
        Ok(())
    } else {
        srv.serve().await
    };
    hub.shutdown().await;
    if let Err(e) = res {
        // Go exits here without the deferred Close, so the socket file is left behind.
        transport_rs::panic_hook::err_line(&format!("portico-transport: serve: {e}"));
        return 1;
    }
    srv.close().await; // the deferred srv.Close(): removes the socket
    0
}

#[cfg(test)]
mod tests {
    use super::guarded;

    #[test]
    fn a_panic_on_the_main_thread_is_exit_code_2() {
        assert_eq!(guarded(|| 0), 0);
        assert_eq!(guarded(|| 7), 7);
        assert_eq!(guarded(|| panic!("boom")), 2);
    }
}
