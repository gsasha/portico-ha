//! The helpers the golden replays share (`golden_house.rs`, `golden_wsmedia.rs`): reading a
//! vector file, hex, the `wmGen` recipes of the generator, the frame summary and a bounded wait.
//! (`Rec`, `Obs` and `raw_params` differ between the replays in their fields and in what they
//! accept, so each keeps its own; R2-52.)

use std::path::PathBuf;
use std::time::Duration;

use md5::{Digest, Md5};
use serde_json::{json, Map, Value};
use tokio::time::{sleep, Instant};

/// `tests/golden/<name>` as JSON.
pub fn load(name: &str) -> Value {
    let mut p = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    p.push("tests/golden");
    p.push(name);
    let text = std::fs::read_to_string(&p).unwrap_or_else(|e| panic!("{}: {e}", p.display()));
    serde_json::from_str(&text).unwrap()
}

pub fn hex(b: &[u8]) -> String {
    b.iter().map(|x| format!("{x:02x}")).collect()
}

/// The recipes of `wmGen.bytes()`.
pub fn gen_bytes(g: &Value) -> Vec<u8> {
    let (start, step, len) = (
        g["start"].as_i64().unwrap_or(0),
        g["step"].as_i64().unwrap_or(0),
        g["len"].as_u64().unwrap_or(0) as usize,
    );
    match g["kind"].as_str().unwrap() {
        "bytes" => (0..len).map(|i| (start + i as i64 * step) as u8).collect(),
        "samples" => (0..len)
            .flat_map(|i| ((start + i as i64 * step) as i16).to_le_bytes())
            .collect(),
        "lcg" => {
            let mut x = g["seed"].as_u64().unwrap_or(0) as u32;
            (0..len)
                .map(|_| {
                    x = x.wrapping_mul(1103515245).wrapping_add(12345);
                    (x >> 16) as u8
                })
                .collect()
        }
        k => panic!("gen kind {k}"),
    }
}

pub fn frame_of(b: &[u8]) -> Value {
    let mut m = Map::new();
    m.insert("n".into(), json!(b.len()));
    m.insert("md5".into(), json!(hex(&Md5::digest(b))));
    if b.len() <= 24 {
        m.insert("hex".into(), json!(hex(b)));
    }
    Value::Object(m)
}

pub async fn wait_until(ms: u64, mut cond: impl FnMut() -> bool) {
    let end = Instant::now() + Duration::from_millis(ms);
    while Instant::now() < end && !cond() {
        sleep(Duration::from_millis(2)).await;
    }
}
