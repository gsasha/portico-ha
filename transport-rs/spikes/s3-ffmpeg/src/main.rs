//! S3: a tokio program spawns ffmpeg, feeds it 16 kHz s16le PCM on stdin (a 440 Hz tone, 20 ms frames at
//! real-time cadence like the talkback pump) and reads 8 kHz s16le from its stdout, then checks the tone.
use std::process::Stdio;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::process::Command;

#[tokio::main]
async fn main() -> std::io::Result<()> {
    let mut child = Command::new("ffmpeg")
        .args(["-hide_banner", "-loglevel", "error", "-f", "s16le", "-ar", "16000", "-ac", "1", "-i", "pipe:0"])
        .args(["-ac", "1", "-ar", "8000", "-f", "s16le", "pipe:1"])
        .stdin(Stdio::piped()).stdout(Stdio::piped()).kill_on_drop(true).spawn()?;
    let mut stdin = child.stdin.take().unwrap();
    let mut stdout = child.stdout.take().unwrap();
    let reader = tokio::spawn(async move { let mut v = Vec::new(); stdout.read_to_end(&mut v).await.map(|_| v) });
    let t0 = std::time::Instant::now();
    for f in 0..100u32 {
        // 20 ms = 320 samples at 16 kHz
        let frame: Vec<u8> = (0..320u32).flat_map(|i| {
            let n = f * 320 + i;
            (((n as f64 * 2.0 * std::f64::consts::PI * 440.0 / 16000.0).sin() * 8000.0) as i16).to_le_bytes()
        }).collect();
        stdin.write_all(&frame).await?;
        tokio::time::sleep(std::time::Duration::from_millis(20)).await;
    }
    drop(stdin); // EOF: ffmpeg flushes and exits
    let out = reader.await.unwrap()?;
    let status = child.wait().await?;
    let s: Vec<f64> = out.chunks_exact(2).map(|b| i16::from_le_bytes([b[0], b[1]]) as f64).collect();
    let (k, n) = (2.0 * (2.0 * std::f64::consts::PI * 440.0 / 8000.0).cos(), s.len() as f64);
    let (mut s1, mut s2, mut e) = (0.0, 0.0, 0.0);
    for x in &s { let s0 = x + k * s1 - s2; s2 = s1; s1 = s0; e += x * x; }
    let ratio = (2.0 * (s1 * s1 + s2 * s2 - k * s1 * s2) / n) / e;
    println!("ffmpeg exit={status} fed=100 frames/2.0 s in {:.2}s; read {} bytes = {} samples at 8 kHz (expect ~16000); 440 Hz energy ratio {:.3}", t0.elapsed().as_secs_f64(), out.len(), s.len(), ratio);
    Ok(())
}
