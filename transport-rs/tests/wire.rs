//! Wire-level checks of the empty-registry dispatcher and the framing limits. These are new
//! tests (not ports); the Go-vs-Rust comparison itself is `scripts/ipc_smoke.py`.

use std::time::Duration;
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::net::UnixStream;
use transport_rs::{dispatch, ipc};

async fn start(tag: &str) -> (ipc::Server, std::path::PathBuf) {
    let dir = std::env::temp_dir().join(format!("wire-{}-{tag}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    let sock = dir.join("t.sock");
    let srv = ipc::Server::listen(&sock).unwrap();
    dispatch::register(&srv, dispatch::HubOptions::default());
    (srv, sock)
}

#[tokio::test]
async fn empty_registry_answers() {
    let (srv, sock) = start("a").await;
    let s = srv.clone();
    tokio::spawn(async move { s.serve().await });
    let c = UnixStream::connect(&sock).await.unwrap();
    let (rd, mut wr) = c.into_split();
    let mut rd = BufReader::new(rd);
    wr.write_all(b"not json\n{\"id\":1,\"method\":\"dial\",\"params\":{\"device_id\":\"d1\"}}\n")
        .await
        .unwrap();
    let mut line = String::new();
    rd.read_line(&mut line).await.unwrap();
    assert_eq!(
        line,
        "{\"id\":1,\"error\":{\"code\":\"unknown_device\",\"message\":\"d1\"}}\n"
    );
    wr.write_all(b"{\"id\":2,\"method\":\"keepalive\"}\n")
        .await
        .unwrap();
    line.clear();
    rd.read_line(&mut line).await.unwrap();
    assert!(line.contains("\"code\":\"not_implemented\""), "{line}");
    srv.close().await;
}

#[tokio::test]
async fn over_limit_line_fails_serve_with_token_too_long() {
    let (srv, sock) = start("b").await;
    let s = srv.clone();
    let done = tokio::spawn(async move { s.serve().await });
    let mut c = UnixStream::connect(&sock).await.unwrap();
    let head = b"{\"id\":5,\"method\":\"keepalive\",\"params\":\"";
    let mut line = head.to_vec();
    line.resize(ipc::MAX_LINE, b'A'); // 1 MiB of content: with its newline it does not fit
    line.push(b'\n');
    let _ = c.write_all(&line).await;
    let r = tokio::time::timeout(Duration::from_secs(5), done)
        .await
        .unwrap()
        .unwrap();
    assert_eq!(r, Err(ipc::ERR_TOO_LONG.to_string()));
}

// R2-12: a request line that never ends is bounded too (the test above sends its newline, which
// the other check catches): 1 MiB + 1 bytes with no newline and the connection kept open.
#[tokio::test]
async fn an_endless_line_without_a_newline_fails_serve_with_token_too_long() {
    let (srv, sock) = start("c").await;
    let s = srv.clone();
    let done = tokio::spawn(async move { s.serve().await });
    let mut c = UnixStream::connect(&sock).await.unwrap();
    let _ = c.write_all(&vec![b'A'; ipc::MAX_LINE + 1]).await;
    let r = tokio::time::timeout(Duration::from_secs(1), done)
        .await
        .expect("serve kept reading an endless line")
        .unwrap();
    assert_eq!(r, Err(ipc::ERR_TOO_LONG.to_string()));
}
