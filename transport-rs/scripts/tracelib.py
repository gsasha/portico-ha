"""Shared pieces of the trace-diff (DEV-764 Phase 2 item 4): timestamped loopback fakes, an IPC tap
and the small statistics the comparisons use. Python standard library only.

Every timestamp is CLOCK_REALTIME nanoseconds: UDP datagrams carry the KERNEL receive time
(SO_TIMESTAMPNS), so the harness's own thread scheduling does not enter the SIP/RTP traces; the IPC
tap stamps a line with time.time_ns() the moment its reader thread has it (a few hundred
microseconds of Python scheduling, stated where it matters).
"""
import base64
import json
import math
import os
import re
import signal
import socket
import struct
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import ipc_smoke as S  # noqa: E402  (the seed: Proc, DahuaStation helpers, free_udp_port)

SO_TIMESTAMPNS = getattr(socket, "SO_TIMESTAMPNS", 35)


def ms(ns):
    return ns / 1e6


def enable_ts(sock):
    try:
        sock.setsockopt(socket.SOL_SOCKET, SO_TIMESTAMPNS, 1)
    except OSError:
        pass


def recv_ts(sock, size=65535):
    """(data, addr, t_ns): t_ns is the kernel receive time when the socket provides it."""
    data, anc, _flags, addr = sock.recvmsg(size, 128)
    t = None
    for lvl, typ, cd in anc:
        if lvl == socket.SOL_SOCKET and typ == SO_TIMESTAMPNS and len(cd) >= 16:
            sec, nsec = struct.unpack("ll", cd[:16])
            t = sec * 1_000_000_000 + nsec
    return data, addr, (t if t is not None else time.time_ns())


# ---------------------------------------------------------------------------------------------
# statistics
# ---------------------------------------------------------------------------------------------
def pct(xs, p):
    if not xs:
        return None
    s = sorted(xs)
    k = min(len(s) - 1, max(0, int(math.ceil(p / 100.0 * len(s))) - 1))
    return s[k]


def summary(xs):
    if not xs:
        return {"n": 0}
    n = len(xs)
    m = sum(xs) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in xs) / n)
    return {"n": n, "mean": round(m, 4), "std": round(sd, 4), "min": round(min(xs), 4),
            "p50": round(pct(xs, 50), 4), "p99": round(pct(xs, 99), 4), "max": round(max(xs), 4)}


def slope(xs, ys):
    """Least-squares slope of ys against xs (same unit per unit)."""
    n = len(xs)
    if n < 3:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    den = sum((x - mx) ** 2 for x in xs)
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den if den else None


# ---------------------------------------------------------------------------------------------
# a SIP station on a loopback alias, with a kernel-timestamped log
# ---------------------------------------------------------------------------------------------
def hdr(msg, name):
    m = re.search(r"(?im)^%s:[ \t]*(.*?)\r?$" % re.escape(name), msg)
    return m.group(1) if m else ""


class TStation:
    """Like ipc_smoke.DahuaStation (a fake Dahua VTO on <ip>:5060 that REGISTERs, is INVITEd and
    BYEd by the transport) but every datagram is logged with its kernel receive time and every
    behaviour is a field:

        reg_mode    ok | silent | 403 | closed (do not answer; the same as silent) | nonce_only
        granted     the Expires the 200 to an authenticated REGISTER carries
        inv_mode    none | 100 | 180 | 200 | 200badsdp | 486
        bye_mode    ok | silent
        cancel_mode ok (200 to the CANCEL, then 487 to the INVITE) | only200 | silent
    """

    def __init__(self, ip, port=5060):
        self.ip = ip
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((ip, port))
        self.sock.settimeout(0.1)
        enable_ts(self.sock)
        self.rtp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.rtp.bind((ip, 0))
        self.reg_mode, self.granted = "ok", 3600
        self.inv_mode, self.bye_mode, self.cancel_mode = "none", "ok", "ok"
        self.log = []  # {"t": ns, "line": ..., "call_id", "cseq", "auth", "expires", "raw"}
        self.sent = []  # (t_ns, first line) of what the station sent
        self.drv = None
        self.live = True
        self.lock = threading.Lock()
        self.t = threading.Thread(target=self.run, daemon=True)
        self.t.start()

    def echo(self, req, code, reason, tag="", body="", extra=()):
        head = req.split("\r\n\r\n", 1)[0]
        lines = ["SIP/2.0 %d %s" % (code, reason)]
        lines += [l for l in head.split("\r\n") if l.lower().startswith("via:")]
        to = hdr(req, "To")
        if tag and "tag=" not in to:
            to += ";tag=" + tag
        lines += ["From: " + hdr(req, "From"), "To: " + to, "Call-ID: " + hdr(req, "Call-ID"),
                  "CSeq: " + hdr(req, "CSeq"), *extra]
        if body:
            lines.append("Content-Type: application/sdp")
        lines.append("Content-Length: %d" % len(body))
        return "\r\n".join(lines) + "\r\n\r\n" + body

    def run(self):
        while self.live:
            try:
                data, frm, t = recv_ts(self.sock)
            except OSError:
                continue
            req = data.decode("utf-8", "replace")
            self.drv = self.drv or frm
            with self.lock:
                self.log.append({"t": t, "line": req.split("\r\n")[0], "call_id": hdr(req, "Call-ID"),
                                 "cseq": hdr(req, "CSeq"), "auth": "Authorization:" in req,
                                 "expires": hdr(req, "Expires"), "raw": req, "src": frm})
            self.react(req, frm)

    def reply(self, text, frm):
        self.sock.sendto(text.encode(), frm)
        with self.lock:
            self.sent.append((time.time_ns(), text.split("\r\n")[0]))

    def react(self, req, frm):
        if req.startswith("REGISTER "):
            if self.reg_mode in ("silent", "closed"):
                return
            if self.reg_mode == "403":
                return self.reply(self.echo(req, 403, "Forbidden"), frm)
            if "Authorization:" not in req:
                return self.reply(self.echo(req, 401, "Unauthorized", extra=[
                    'WWW-Authenticate: Digest realm="r", nonce="n1", algorithm=MD5']), frm)
            exp = 0 if "Expires: 0" in req else self.granted
            return self.reply(self.echo(req, 200, "OK", extra=["Expires: %d" % exp]), frm)
        if req.startswith("INVITE "):
            m = self.inv_mode
            if m == "100":
                self.reply(self.echo(req, 100, "Trying"), frm)
            elif m == "180":
                self.reply(self.echo(req, 100, "Trying"), frm)
                self.reply(self.echo(req, 180, "Ringing", "devicetag"), frm)
            elif m in ("200", "200badsdp"):
                sdp = ("v=0\r\no=- 0 0 IN IP4 %s\r\ns=x\r\nc=IN IP4 %s\r\nt=0 0\r\nm=audio %d RTP/AVP 96\r\n"
                       "a=rtpmap:96 L16/16000\r\n" % (self.ip, self.ip, self.rtp.getsockname()[1]))
                if m == "200badsdp":
                    sdp = "this is not an sdp\r\n"
                self.reply(self.echo(req, 200, "OK", "devicetag", sdp), frm)
            elif m == "486":
                self.reply(self.echo(req, 486, "Busy Here", "devicetag"), frm)
        elif req.startswith("CANCEL "):
            if self.cancel_mode == "silent":
                return
            self.reply(self.echo(req, 200, "OK"), frm)
        elif req.startswith("BYE "):
            if self.bye_mode == "ok":
                self.reply(self.echo(req, 200, "OK"), frm)

    def send(self, text):
        self.sock.sendto(text.encode(), self.drv)
        with self.lock:
            self.sent.append((time.time_ns(), text.split("\r\n")[0]))

    def snapshot(self):
        with self.lock:
            return list(self.log)

    def wait_for(self, pred, timeout):
        end = time.time() + timeout
        while time.time() < end:
            for e in self.snapshot():
                if pred(e):
                    return e
            time.sleep(0.01)
        return None

    def close(self):
        self.live = False
        self.t.join(2.0)
        self.sock.close()
        self.rtp.close()


# ---------------------------------------------------------------------------------------------
# an IPC tap: a reader thread that stamps every line the binary writes
# ---------------------------------------------------------------------------------------------
class Tap(threading.Thread):
    def __init__(self, pr):
        super().__init__(daemon=True)
        self.pr = pr
        self.lines = []  # (t_ns, raw line)
        self.live = True
        self.lock = threading.Lock()
        pr.s.settimeout(0.05)
        self.start()

    def run(self):
        buf = b""
        while self.live:
            try:
                d = self.pr.s.recv(1 << 20)
            except socket.timeout:
                continue
            except OSError:
                return
            if not d:
                return
            t = time.time_ns()
            buf += d
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                with self.lock:
                    self.lines.append((t, line.decode("utf-8", "replace")))

    def snapshot(self):
        with self.lock:
            return list(self.lines)

    def wait_for(self, pred, timeout, start=0):
        end = time.time() + timeout
        while time.time() < end:
            for i, (t, l) in enumerate(self.snapshot()[start:], start):
                try:
                    m = json.loads(l)
                except ValueError:
                    continue
                if pred(m):
                    return i, t, m
            time.sleep(0.005)
        return None

    def stop(self):
        self.live = False


def ipc(pr, **kw):
    pr.send(json.dumps(kw, separators=(",", ":")) + "\n")


def sleep_until(t_ns):
    """Absolute-deadline sleep in CLOCK_REALTIME ns: coarse sleep, then spin the last 0.4 ms."""
    while True:
        d = (t_ns - time.time_ns()) / 1e9
        if d <= 0:
            return
        time.sleep(d - 0.0004 if d > 0.0008 else 0)


# ---------------------------------------------------------------------------------------------
# G.711 oracles (independent of both binaries) for the PCM comparisons
# ---------------------------------------------------------------------------------------------
def mulaw_decode(b):
    b = ~b & 0xFF
    t = (((b & 0x0F) << 3) + 0x84) << ((b & 0x70) >> 4)
    return (0x84 - t) if (b & 0x80) else (t - 0x84)


def alaw_decode(a):
    a ^= 0x55
    t = (a & 0x0F) << 4
    seg = (a & 0x70) >> 4
    if seg == 0:
        t += 8
    elif seg == 1:
        t += 0x108
    else:
        t += 0x108
        t <<= seg - 1
    return t if (a & 0x80) else -t


def mulaw_encode(pcm):
    """Standard G.711 mu-law (the Sun g711.c routine)."""
    BIAS, CLIP = 0x84, 32635
    sign = 0x80 if pcm < 0 else 0
    if pcm < 0:
        pcm = -pcm
    pcm = min(pcm, CLIP) + BIAS
    exp = 7
    mask = 0x4000
    while exp > 0 and not (pcm & mask):
        exp -= 1
        mask >>= 1
    mant = (pcm >> (exp + 3)) & 0x0F
    return ~(sign | (exp << 4) | mant) & 0xFF


def wav_bytes(samples, rate):
    data = struct.pack("<%dh" % len(samples), *samples)
    return (b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
            + b"data" + struct.pack("<I", len(data)) + data)


def b64(b):
    return base64.b64encode(b).decode()


def chaos(pr, at_ns, dur_s):
    """Discrimination helper: stop the binary (SIGSTOP) at the absolute time at_ns for dur_s seconds,
    then continue it. A trace taken this way must FAIL the pacing / timer checks."""
    def run():
        sleep_until(at_ns)
        os.kill(pr.p.pid, signal.SIGSTOP)
        time.sleep(dur_s)
        os.kill(pr.p.pid, signal.SIGCONT)
    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t
