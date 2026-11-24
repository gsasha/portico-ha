#!/usr/bin/env python3
"""Real-client interop harness for transport-rs (DEV-764 slice 4b). Python 3 standard library only.

A real browser (host Firefox as a plain process, or Chromium in `docker run --rm --network host`) is
the WebRTC offerer. The harness drives the REAL transport binary over its IPC: it configures a
`gate_sip_server`, plays a scripted SIP station (REGISTER with digest, INVITE with a 16 kHz L16 tone,
RTP both ways), relays the page's offer / trickle candidates to `webrtc_offer` / `webrtc_ice`, and
measures what each side hears with an FFT. The station leg is checked too: the 200 OK must come only
after the browser connected, and a BYE must follow the browser's end.

  harness.py --binary BIN --browser firefox|chrome --scenario A|B|C|D|E|F|H1|H2|H3 [--label L] [--out DIR]

Scenarios: A plain call with G.722 (wideband tones) | B a browser that offers PCMU only | C Opus-first
offer with PCMU listed before G.722 (the offerer's order decides) | D restartIce | E browser killed
(time to teardown, station hung up, slot freed) | F two sessions at once (two doors) | H1 trickle
after the offer (normal) | H2 candidates inside the offer | H3 candidates before the offer.
G (hostile offers) is hostile.py. See README.md.

Everything is written under --out (default $INTEROP_OUT or a temp dir), never into the worktree.
"""
import argparse
import cmath
import hashlib
import json
import math
import os
import re
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.environ.get("INTEROP_CACHE", tempfile.gettempdir())


def now():
    return time.time()


def free_udp():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def md5(x):
    return hashlib.md5(x.encode()).hexdigest()


# ---------------------------------------------------------------------------------------------
# FFT (radix 2, pure Python): the dominant frequency of 16 kHz mono PCM windows
# ---------------------------------------------------------------------------------------------
def fft(x):
    n = len(x)
    if n == 1:
        return x
    ev, od = fft(x[0::2]), fft(x[1::2])
    t = [cmath.exp(-2j * math.pi * k / n) * od[k] for k in range(n // 2)]
    return [ev[k] + t[k] for k in range(n // 2)] + [ev[k] - t[k] for k in range(n // 2)]


def dominant(samples, rate=16000, n=4096):
    """(peak Hz, rms) of the first n samples (a Hann window)."""
    seg = samples[:n]
    rms = math.sqrt(sum(v * v for v in seg) / len(seg))
    w = [seg[i] * (0.5 - 0.5 * math.cos(2 * math.pi * i / (n - 1))) for i in range(n)]
    spec = fft([complex(v) for v in w])
    best = max(range(2, n // 2), key=lambda k: abs(spec[k]))
    return best * rate / n, rms


def windows(pcm, rate=16000, n=4096):
    return [dominant(pcm[i:i + n], rate, n) for i in range(0, len(pcm) - n, n)]


# ---------------------------------------------------------------------------------------------
# The transport process and its IPC
# ---------------------------------------------------------------------------------------------
class Transport:
    def __init__(self, binary, out, env_extra=None):
        self.out = out
        self.sock = os.path.join(out, "t.sock")
        env = dict(os.environ)
        env.update(env_extra or {})
        self.lines = []  # (time, line) of stdout+stderr
        self.p = subprocess.Popen([binary, "-socket", self.sock], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  env=env, start_new_session=True)
        threading.Thread(target=self._pump, daemon=True).start()
        self.s = None
        for _ in range(100):
            if os.path.exists(self.sock):
                try:
                    self.s = socket.socket(socket.AF_UNIX)
                    self.s.connect(self.sock)
                    break
                except OSError:
                    self.s.close()
                    self.s = None
            time.sleep(0.05)
        if not self.s:
            raise SystemExit("could not connect to the transport socket")
        self.lock = threading.Lock()
        self.cv = threading.Condition(self.lock)
        self.next_id = 1
        self.resp = {}
        self.notes = []
        threading.Thread(target=self._read, daemon=True).start()

    def _pump(self):
        for raw in self.p.stdout:
            self.lines.append((now(), raw.decode(errors="replace").rstrip()))

    def _read(self):
        f = self.s.makefile("rb")
        for raw in f:
            try:
                m = json.loads(raw)
            except ValueError:
                continue
            with self.cv:
                if "id" in m:
                    self.resp[m["id"]] = (now(), m)
                else:
                    self.notes.append((now(), m))
                self.cv.notify_all()

    def call(self, method, params, timeout=40):
        with self.cv:
            i = self.next_id
            self.next_id += 1
        self.s.sendall((json.dumps({"id": i, "method": method, "params": params}) + "\n").encode())
        end = now() + timeout
        with self.cv:
            while i not in self.resp:
                left = end - now()
                if left <= 0:
                    return {"error": {"code": "harness_timeout", "message": method}}
                self.cv.wait(left)
            return self.resp.pop(i)[1]

    def wait_note(self, method, pred=lambda p: True, timeout=10, since=0):
        end = now() + timeout
        with self.cv:
            while True:
                for t, m in self.notes:
                    if t >= since and m.get("method") == method and pred(m.get("params") or {}):
                        return t, m
                left = end - now()
                if left <= 0:
                    return None
                self.cv.wait(left)

    def rss_kb(self):
        try:
            for l in open("/proc/%d/status" % self.p.pid):
                if l.startswith("VmRSS:"):
                    return int(l.split()[1])
        except OSError:
            pass
        return None

    def cpu_ticks(self):
        """utime + stime of the transport process, in clock ticks (100 per second)."""
        try:
            f = open("/proc/%d/stat" % self.p.pid).read().rsplit(")", 1)[1].split()
            return int(f[11]) + int(f[12])
        except (OSError, IndexError, ValueError):
            return None

    def alive(self):
        return self.p.poll() is None

    def stop(self):
        try:
            os.killpg(self.p.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            self.p.wait(timeout=10)
        except Exception:
            try:
                os.killpg(self.p.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


# ---------------------------------------------------------------------------------------------
# The scripted SIP station of one door
# ---------------------------------------------------------------------------------------------
class Station:
    def __init__(self, sip_port, tone_hz):
        self.sip_port = sip_port
        self.tone_hz = tone_hz
        self.sip = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sip.bind(("127.0.0.1", 0))
        self.me = "127.0.0.1:%d" % self.sip.getsockname()[1]
        self.rtp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.rtp.bind(("127.0.0.1", 0))
        self.rtp_port = self.rtp.getsockname()[1]
        self.server_rtp = None
        self.msgs = []  # (time, text)
        self.cv = threading.Condition()
        self.pcm = bytearray()
        self.pcm_times = []  # (time, bytes_so_far)
        self.rx_packets = 0
        self.tx_packets = 0
        self.sending = threading.Event()
        self.stop_ev = threading.Event()
        self.seen_bye = None
        threading.Thread(target=self._sip_loop, daemon=True).start()
        threading.Thread(target=self._rtp_rx, daemon=True).start()
        threading.Thread(target=self._rtp_tx, daemon=True).start()

    def _sip_loop(self):
        self.sip.settimeout(0.2)
        while not self.stop_ev.is_set():
            try:
                data, _ = self.sip.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                return
            text = data.decode(errors="replace")
            with self.cv:
                self.msgs.append((now(), text))
                self.cv.notify_all()
            first = text.split("\r\n")[0]
            if first.startswith("BYE "):
                self.seen_bye = now()
                self._reply(text, "200 OK")
            elif first.startswith("SIP/2.0 200") and "INVITE" in self._hdr(text, "CSeq"):
                self.server_rtp = self._server_rtp(text)
                self._ack(text)

    def _server_rtp(self, text):
        body = text.split("\r\n\r\n", 1)[1] if "\r\n\r\n" in text else ""
        m = re.search(r"m=audio (\d+)", body)
        c = re.search(r"c=IN IP4 (\S+)", body)
        return (c.group(1), int(m.group(1))) if m and c else None

    @staticmethod
    def _hdr(msg, name):
        m = re.search(r"(?im)^%s:[ \t]*(.*?)\r?$" % re.escape(name), msg)
        return m.group(1) if m else ""

    def _reply(self, req, status):
        lines = ["SIP/2.0 " + status, "Via: " + self._hdr(req, "Via"), "From: " + self._hdr(req, "From"),
                 "To: " + self._hdr(req, "To"), "Call-ID: " + self._hdr(req, "Call-ID"), "CSeq: " + self._hdr(req, "CSeq"),
                 "Content-Length: 0"]
        self.sip.sendto(("\r\n".join(lines) + "\r\n\r\n").encode(), ("127.0.0.1", self.sip_port))

    def _ack(self, resp):
        cid = self._hdr(resp, "Call-ID")
        msg = ("ACK sip:9901@127.0.0.1:%d SIP/2.0\r\nVia: SIP/2.0/UDP %s;branch=z9hG4bKack%s\r\n"
               "From: <sip:8001@station>;tag=t-%s\r\nTo: %s\r\nCall-ID: %s\r\nCSeq: 1 ACK\r\nContent-Length: 0\r\n\r\n"
               % (self.sip_port, self.me, cid, cid, self._hdr(resp, "To"), cid))
        self.sip.sendto(msg.encode(), ("127.0.0.1", self.sip_port))

    def send(self, text):
        self.sip.sendto(text.encode(), ("127.0.0.1", self.sip_port))

    def wait_msg(self, pred, timeout, since=0):
        end = now() + timeout
        with self.cv:
            while True:
                for t, m in self.msgs:
                    if t >= since and pred(m):
                        return t, m
                left = end - now()
                if left <= 0:
                    return None
                self.cv.wait(left)

    def register(self, password="s3cretpass"):
        def reg(auth=""):
            a = "Authorization: %s\r\n" % auth if auth else ""
            self.send("REGISTER sip:127.0.0.1:%d SIP/2.0\r\nVia: SIP/2.0/UDP %s;branch=z9hG4bKreg\r\n"
                      "From: <sip:8001@station>;tag=reg\r\nTo: <sip:8001@station>\r\nCall-ID: reg-1\r\n"
                      "CSeq: 1 REGISTER\r\nContact: <sip:8001@%s>\r\nExpires: 300\r\n%sContent-Length: 0\r\n\r\n"
                      % (self.sip_port, self.me, self.me, a))
        t0 = now()
        reg()
        r = self.wait_msg(lambda m: m.startswith("SIP/2.0 401"), 5, t0)
        if not r:
            return False
        nonce = re.search(r'nonce="([^"]*)"', r[1]).group(1)
        uri = "sip:127.0.0.1"
        resp = md5(md5("8001:VDP:%s" % password) + ":" + nonce + ":" + md5("REGISTER:" + uri))
        t1 = now()
        reg('Digest username="8001", realm="VDP", nonce="%s", uri="%s", response="%s", algorithm=MD5' % (nonce, uri, resp))
        return bool(self.wait_msg(lambda m: m.startswith("SIP/2.0 200") and "REGISTER" in self._hdr(m, "CSeq"), 5, t1))

    def invite(self, call_id):
        sdp = ("v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\ns=x\r\nc=IN IP4 127.0.0.1\r\nt=0 0\r\nm=audio %d RTP/AVP 96\r\n"
               "a=rtpmap:96 PCM/16000\r\n" % self.rtp_port)
        self.send("INVITE sip:9901@127.0.0.1:%d SIP/2.0\r\nVia: SIP/2.0/UDP %s;branch=z9hG4bK%s\r\n"
                  "From: <sip:8001@station>;tag=t-%s\r\nTo: <sip:9901@server>\r\nCall-ID: %s\r\nCSeq: 1 INVITE\r\n"
                  "Contact: <sip:8001@%s>\r\nContent-Type: application/sdp\r\nContent-Length: %d\r\n\r\n%s"
                  % (self.sip_port, self.me, call_id, call_id, call_id, self.me, len(sdp), sdp))

    def cancel(self, call_id):
        self.send("CANCEL sip:9901@127.0.0.1:%d SIP/2.0\r\nVia: SIP/2.0/UDP %s;branch=z9hG4bK%s\r\n"
                  "From: <sip:8001@station>;tag=t-%s\r\nTo: <sip:9901@server>\r\nCall-ID: %s\r\nCSeq: 1 CANCEL\r\n"
                  "Content-Length: 0\r\n\r\n" % (self.sip_port, self.me, call_id, call_id, call_id))

    def _rtp_rx(self):
        self.rtp.settimeout(0.2)
        while not self.stop_ev.is_set():
            try:
                data, _ = self.rtp.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                return
            if len(data) > 12:
                self.pcm += data[12:]
                self.rx_packets += 1
                self.pcm_times.append((now(), len(self.pcm)))

    def _rtp_tx(self):
        seq, ts, ph = 0, 0, 0.0
        nxt = now()
        while not self.stop_ev.is_set():
            if not self.sending.is_set() or not self.server_rtp:
                time.sleep(0.01)
                nxt = now()
                continue
            d = nxt - now()
            if d > 0:
                time.sleep(d)
            samples = []
            for _ in range(320):
                samples.append(int(8000 * math.sin(ph)))
                ph += 2 * math.pi * self.tone_hz / 16000.0
            hdr = struct.pack(">BBHII", 0x80, 96, seq & 0xFFFF, ts & 0xFFFFFFFF, 0xDEADBEEF)
            try:
                self.rtp.sendto(hdr + struct.pack("<320h", *samples), self.server_rtp)
                self.tx_packets += 1
            except OSError:
                pass
            seq += 1
            ts += 320
            nxt += 0.02

    def stop(self):
        self.stop_ev.set()


# ---------------------------------------------------------------------------------------------
# HTTP side: the page talks to the harness, the harness talks to the transport
# ---------------------------------------------------------------------------------------------
class State:
    def __init__(self):
        self.reports, self.logs, self.done, self.fatal = {}, {}, set(), {}
        self.ice_results = []  # (time, sid, response)
        self.offer_results = []
        self.lock = threading.Lock()


def make_handler(tr, st, sid_call, page_path):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _json(self, obj, code=200):
            b = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            if u.path in ("/", "/page.html"):
                b = open(page_path, "rb").read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)
            elif u.path == "/cands":
                time.sleep(1.0)  # the transport never trickles its own candidates (non-trickle answer)
                q = urllib.parse.parse_qs(u.query)
                self._json({"c": [], "n": int(q.get("since", ["0"])[0])})
            else:
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()

        def do_POST(self):
            u = urllib.parse.urlparse(self.path)
            sid = urllib.parse.parse_qs(u.query).get("sid", ["1"])[0]
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n) or b"{}")
            call = sid_call.get(sid, "interop-%s" % sid)
            if u.path == "/offer":
                t0 = now()
                r = tr.call("webrtc_offer", {"call_id": call, "sdp": body["sdp"]})
                with st.lock:
                    st.offer_results.append((t0, now(), sid, r.get("error") or "ok"))
                if "result" in r:
                    self._json({"sdp": r["result"]["sdp"]})
                else:
                    self._json({"error": json.dumps(r.get("error"))})
            elif u.path == "/cand":
                r = tr.call("webrtc_ice", {"call_id": call, "candidate": body["c"]})
                with st.lock:
                    st.ice_results.append((now(), sid, body["c"][:60], r.get("error") or "ok"))
                self._json({})
            elif u.path == "/bye":
                self._json({})
            elif u.path == "/report":
                with st.lock:
                    if body.get("done"):
                        st.done.add(sid)
                    elif body.get("fatal"):
                        st.fatal[sid] = body["fatal"]
                    else:
                        body["srv_t"] = now()
                        st.reports.setdefault(sid, []).append(body)
                self._json({})
            elif u.path == "/log":
                with st.lock:
                    body["srv_t"] = now()
                    st.logs.setdefault(sid, []).append(body)
                self._json({})
            else:
                self._json({}, 404)

    return H


def write_ff_profile(d):
    os.makedirs(d, exist_ok=True)
    prefs = {
        "media.navigator.streams.fake": True,
        "media.navigator.permission.disabled": True,
        "media.peerconnection.ice.obfuscate_host_addresses": False,
        "media.peerconnection.ice.obfuscate_host_addresses.blocklist": "",
        "media.autoplay.default": 0,
        "media.autoplay.blocking_policy": 0,
        "datareporting.policy.dataSubmissionEnabled": False,
        "app.update.enabled": False,
        "browser.shell.checkDefaultBrowser": False,
        "toolkit.telemetry.reportingpolicy.firstRun": False,
        "network.captive-portal-service.enabled": False,
        "network.connectivity-service.enabled": False,
        "media.peerconnection.ice.loopback": False,
        "browser.startup.homepage_override.mstone": "ignore",
    }
    with open(os.path.join(d, "user.js"), "w") as f:
        for k, v in prefs.items():
            f.write('user_pref("%s", %s);\n' % (k, json.dumps(v)))


class Browser:
    def __init__(self, kind, url, name, profile_dir, logpath, chrome_flags=()):
        self.kind, self.name = kind, name
        self.log = open(logpath, "w")
        if kind == "firefox":
            write_ff_profile(profile_dir)
            env = dict(os.environ, HOME=CACHE, MOZ_HEADLESS="1")
            self.p = subprocess.Popen(["/usr/bin/firefox", "--headless", "--no-remote", "--profile", profile_dir, url],
                                      stdout=self.log, stderr=subprocess.STDOUT, env=env, start_new_session=True)
        else:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True)
            cmd = ["docker", "run", "--rm", "--name", name, "--network", "host", "zenika/alpine-chrome:latest",
                   "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
                   "--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream",
                   "--autoplay-policy=no-user-gesture-required", "--no-first-run",
                   "--user-data-dir=/tmp/chrome-profile",
                   "--disable-features=WebRtcHideLocalIpsWithMdns",
                   "--remote-debugging-port=%d" % (9400 + int(name.rsplit("-", 1)[1]) % 90)] + list(chrome_flags) + [url]
            self.p = subprocess.Popen(cmd, stdout=self.log, stderr=subprocess.STDOUT, start_new_session=True)
        self.pid = self.p.pid

    def kill(self):
        """Exact: the process group of the PID we started, or the container we named."""
        t = now()
        if self.kind == "firefox":
            try:
                os.killpg(self.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        else:
            subprocess.run(["docker", "kill", self.name], capture_output=True)
        return t

    def stop(self):
        if self.kind == "firefox":
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(self.pid, sig)
                except ProcessLookupError:
                    break
                time.sleep(1)
        else:
            subprocess.run(["docker", "rm", "-f", self.name], capture_output=True)
        try:
            self.p.wait(timeout=10)
        except Exception:
            pass


# ---------------------------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------------------------
SCEN = {
    # name: duration after connected, browser tone, station tone, page codecs, page mode, extras
    "A": dict(duration=60, browser_hz=5000, station_hz=6000, codecs="default", mode="normal", want_codec="G722"),
    "B": dict(duration=20, browser_hz=1000, station_hz=440, codecs="pcmu", mode="normal", want_codec="PCMU"),
    "C": dict(duration=15, browser_hz=1000, station_hz=440, codecs="pcmufirst", mode="normal", want_codec="PCMU"),
    "D": dict(duration=45, browser_hz=5000, station_hz=6000, codecs="default", mode="normal", want_codec="G722", restart_at=20),
    "E": dict(duration=300, browser_hz=1000, station_hz=440, codecs="default", mode="normal", want_codec="G722", kill_at=15),
    "F": dict(duration=30, browser_hz=5000, station_hz=6000, codecs="default", mode="normal", want_codec="G722", doors=2),
    "H1": dict(duration=15, browser_hz=1000, station_hz=440, codecs="default", mode="normal", want_codec="G722"),
    "H2": dict(duration=15, browser_hz=1000, station_hz=440, codecs="default", mode="candsdp", want_codec="G722"),
    "H3": dict(duration=15, browser_hz=1000, station_hz=440, codecs="default", mode="candfirst", want_codec="G722"),
}


def door_browser_hz(sc, i):
    """The tone of door i's browser: distinct per door, so a leak between two calls shows."""
    return sc["browser_hz"] - 2000 * i


def door_station_hz(sc, i):
    return sc["station_hz"] - 4000 * i


def codec_of(sdp):
    maps = {}
    for l in sdp.splitlines():
        if l.startswith("a=rtpmap:"):
            a, b = l[9:].split(" ", 1)
            maps[a] = b
    for l in sdp.splitlines():
        if l.startswith("m=audio"):
            return [(pt, maps.get(pt, "?")) for pt in l.split()[3:]]
    return []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--binary", required=True)
    ap.add_argument("--browser", choices=["firefox", "chrome"], required=True)
    ap.add_argument("--scenario", default="A", choices=sorted(SCEN))
    ap.add_argument("--label", default="rs")
    ap.add_argument("--out", default=os.environ.get("INTEROP_OUT") or tempfile.mkdtemp(prefix="interop-"))
    ap.add_argument("--duration", type=float, default=0)
    ap.add_argument("--env", action="append", default=[], help="KEY=VALUE for the transport process")
    ap.add_argument("--wait-after-kill", type=float, default=70)
    args = ap.parse_args()
    sc = dict(SCEN[args.scenario])
    if args.duration:
        sc["duration"] = args.duration
    run = "%s-%s-%s-%s" % (args.label, args.browser, args.scenario, time.strftime("%H%M%S"))
    out = os.path.join(args.out, run)
    os.makedirs(out, exist_ok=True)

    env = dict(kv.split("=", 1) for kv in args.env)
    tr = Transport(args.binary, out, env)
    result = {"run": run, "scenario": args.scenario, "browser": args.browser, "scenario_params": sc}
    time.sleep(0.5)
    result["rss_idle_kb"] = tr.rss_kb()

    doors = sc.get("doors", 1)
    stations, sid_call, devs = [], {}, []
    for i in range(doors):
        sip_port, rtp_port = free_udp(), free_udp()
        dev = "gate-%d" % (i + 1)
        r = tr.call("configure", {"driver": "gate_sip_server", "config": {
            "device_id": dev, "sip_listen": "127.0.0.1:%d" % sip_port, "rtp_port": rtp_port, "password": "s3cretpass"}})
        if "result" not in r:
            print(json.dumps({"fatal": "configure failed", "r": r}))
            tr.stop()
            sys.exit(2)
        stn = Station(sip_port, door_station_hz(sc, i))
        if not stn.register():
            print(json.dumps({"fatal": "station REGISTER failed"}))
            tr.stop()
            sys.exit(2)
        stations.append(stn)
        devs.append(dev)
        sid_call[str(i + 1)] = "call-%s-%d" % (run, i + 1)
    result["rss_configured_kb"] = tr.rss_kb()

    st = State()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(tr, st, sid_call, HERE + "/page.html"))
    srv.daemon_threads = True
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    # the doors ring
    t_ring = {}
    for i, stn in enumerate(stations):
        sid = str(i + 1)
        t0 = now()
        stn.invite(sid_call[sid])
        n = tr.wait_note("ringing", lambda p, c=sid_call[sid]: p.get("call_id") == c, 10, t0)
        t_ring[sid] = n[0] if n else None
    result["ringing_notified"] = {k: v is not None for k, v in t_ring.items()}

    browsers, rss, cpu = [], [], []
    t_start = now()
    kill_t = None
    ok_run = True
    try:
        for i in range(doors):
            sid = str(i + 1)
            qs = urllib.parse.urlencode({"sid": sid, "src": "osc", "hz": door_browser_hz(sc, i), "mode": sc["mode"],
                                         "duration": sc["duration"], "restart_at": sc.get("restart_at", 0),
                                         "codecs": sc["codecs"]})
            url = "http://127.0.0.1:%d/?%s" % (port, qs)
            browsers.append(Browser(args.browser, url, "interop-chrome-%d-%d" % (os.getpid(), i),
                                    "%s/ffprofile-%s-%d" % (CACHE, run, i), "%s/browser-%d.log" % (out, i)))
            time.sleep(0.5)
        for stn in stations:
            stn.sending.set()  # the station talks from the start (its audio is dropped until the call is answered)
        deadline = now() + sc["duration"] + 60 + (args.wait_after_kill if "kill_at" in sc else 0)
        while now() < deadline:
            time.sleep(0.5)
            r = tr.rss_kb()
            if r:
                rss.append((round(now() - t_start, 1), r))
            cpu.append((now(), tr.cpu_ticks()))
            with st.lock:
                if st.fatal:
                    break
                done = len(st.done) >= doors
                kill_now = False
                if "kill_at" in sc and kill_t is None:
                    firsts = [x["sinceConnectedMs"] for rs in st.reports.values() for x in rs if x.get("sinceConnectedMs") is not None]
                    kill_now = bool(firsts) and max(firsts) / 1000 >= sc["kill_at"]
            if kill_now:
                kill_t = browsers[0].kill()
                result["browser_killed_at"] = kill_t
                # wait for the station to be hung up
                got = stations[0].wait_msg(lambda m: m.startswith("BYE "), args.wait_after_kill, kill_t)
                result["station_bye_after_s"] = round(got[0] - kill_t, 2) if got else None
                time.sleep(2)
                break
            if done:
                time.sleep(3)
                break
        result["page_done"] = sorted(st.done)
        result["page_fatal"] = st.fatal
    finally:
        # the transport side of the end of the call, before the browsers are torn down
        result["transport_lines"] = [(round(t - t_start, 2), l) for t, l in tr.lines]
        for b in browsers:
            b.stop()
        for stn in stations:
            stn.sending.clear()

    # ---------------- the station leg
    result["stations"] = []
    for i, stn in enumerate(stations):
        sid = str(i + 1)
        call = sid_call[sid]
        s = {"sid": sid, "call_id": call, "rx_packets": stn.rx_packets, "tx_packets": stn.tx_packets}
        ok200 = stn.wait_msg(lambda m: m.startswith("SIP/2.0 200") and "INVITE" in stn._hdr(m, "CSeq") and call in m, 0.1)
        s["t_200"] = ok200[0] if ok200 else None
        s["bye_seen"] = stn.seen_bye is not None
        s["t_bye"] = stn.seen_bye
        # FFT of what the station heard, in 4096-sample windows (0.256 s), skipping the first 3 s of PCM
        open(os.path.join(out, "station-%s.pcm" % sid), "wb").write(bytes(stn.pcm))  # raw 16 kHz LE int16, for offline analysis
        pcm = struct.unpack("<%dh" % (len(stn.pcm) // 2), bytes(stn.pcm[:len(stn.pcm) // 2 * 2]))
        w = windows(list(pcm)) if len(pcm) > 8192 else []
        loud = [(f, r) for f, r in w if r > 300]
        runs, last = [], None
        for f, r_ in w:  # run-length encoded dominant frequency (rounded to 50 Hz) of every window
            k = round(f / 50) * 50 if r_ > 300 else 0
            if runs and runs[-1][0] == k:
                runs[-1][1] += 1
            else:
                runs.append([k, 1])
        s["station_dominant_runs"] = runs
        s["station_timeline"] = [[round(f), round(r)] for f, r in w[:12]] + [["..."]] + [[round(f), round(r)] for f, r in w[-6:]]
        s["station_hears"] = {"samples": len(pcm), "windows": len(w), "loud_windows": len(loud),
                              "dominant_hz_hist": hist([round(f / 50) * 50 for f, _ in loud]),
                              "max_rms": max([r for _, r in w] + [0])}
        result["stations"].append(s)

    # ---------------- the browser leg (from the page's reports)
    sessions = []
    for sid in sorted(st.reports):
        if sid == "0":
            continue
        rs = [r for r in st.reports[sid] if "in" in r or "out" in r]
        o = {"sid": sid, "reports": len(rs)}
        if rs:
            last = rs[-1]
            o.update({"final_cs": last.get("cs"), "in": last.get("in"), "out": last.get("out"),
                      "codecs_in": sorted({(r.get("in") or {}).get("codec") for r in rs if r.get("in")} - {None}),
                      "codecs_out": sorted({(r.get("out") or {}).get("codec") for r in rs if r.get("out")} - {None}),
                      "dom_hz": hist([r.get("domHz") for r in rs if r.get("domHz") is not None]),
                      "max_stall_ms": max([r.get("maxStallMs") or 0 for r in rs] + [0]),
                      "pair": next((r["pair"] for r in reversed(rs) if r.get("pair")), None)})
        logs = sorted(st.logs.get(sid, []), key=lambda x: x.get("pt", 0))
        o["states"] = [[l["pt"] / 1000.0, l["m"]] for l in logs if l.get("m", "").startswith(
            ("connectionState", "iceConnectionState", "restartIce", "restart ", "answer received", "offer failed", "page error", "codec preferences"))]
        conn = [l["pt"] / 1000.0 for l in logs if l.get("m") == "connectionState connected"]
        o["t_connected"] = conn[0] if conn else None
        for l in logs:
            if l.get("sdp") and l["m"].startswith("offer"):
                o["offer_codecs"] = codec_of(l["sdp"])
                break
        for l in logs:
            if l.get("sdp") and l["m"].startswith("answer received"):
                o["answer_codecs"] = codec_of(l["sdp"])
                o["answer_candidates"] = l["sdp"].count("a=candidate:")
                o["answer_has_end_of_candidates"] = "a=end-of-candidates" in l["sdp"]
                break
        sessions.append(o)
    result["sessions"] = sessions
    result["offer_results"] = [(round(a - t_start, 2), round(b - t_start, 2), sid, str(e)) for a, b, sid, e in st.offer_results]
    result["ice_results"] = [(round(t - t_start, 2), sid, c, str(e)) for t, sid, c, e in st.ice_results]
    cs = [(t, c) for t, c in cpu if c is not None]
    if len(cs) > 4:
        # CPU of the transport while the call runs: ticks used per second of wall clock, in percent
        result["cpu_percent"] = round((cs[-1][1] - cs[0][1]) / 100.0 / (cs[-1][0] - cs[0][0]) * 100, 2)
        result["cpu_percent_per_5s"] = [round((cs[i + 5][1] - cs[i][1]) / 100.0 / (cs[i + 5][0] - cs[i][0]) * 100, 1) for i in range(0, len(cs) - 5, 5)]
    result["rss_in_call_kb"] = {"min": min([r for _, r in rss] + [0]), "max": max([r for _, r in rss] + [0]), "samples": len(rss)}

    # after the call: the slot is free and the session is gone
    if "kill_at" in sc:
        stn = stations[0]
        call = sid_call["1"]
        r = tr.call("webrtc_ice", {"call_id": call, "candidate": ""})
        result["ice_after_end"] = r.get("error") or r.get("result")
        t0 = now()
        stn.invite(call + "-again")
        n = tr.wait_note("ringing", lambda p: p.get("call_id") == call + "-again", 8, t0)
        result["slot_freed_second_ring_notified"] = n is not None
        stn.cancel(call + "-again")
    result["transport_alive"] = tr.alive()
    result["rss_end_kb"] = tr.rss_kb()
    result["wall_s"] = round(now() - t_start, 1)
    for stn in stations:
        stn.stop()
    srv.shutdown()
    tr.stop()
    open(os.path.join(out, "transport.log"), "w").write("\n".join("%.2f %s" % (t - t_start, l) for t, l in tr.lines))
    result["verdict"] = check(args.scenario, sc, result, t_start)
    json.dump(result, open(os.path.join(out, "result.json"), "w"), indent=1, default=str)
    print(json.dumps({"run": run, "out": out, "verdict": result["verdict"]}, indent=1))
    sys.exit(0 if not result["verdict"]["failed"] else 1)


def hist(xs):
    h = {}
    for x in xs:
        h[str(x)] = h.get(str(x), 0) + 1
    return dict(sorted(h.items(), key=lambda kv: -kv[1])[:5])


def check(name, sc, r, t_start):
    """The pass/fail criteria of each scenario. Every failed criterion is listed."""
    passed, failed = [], []

    def need(ok, what):
        (passed if ok else failed).append(what)

    need(all(r.get("ringing_notified", {}).values()), "ringing notification for every door")
    need(r.get("transport_alive"), "the transport process is alive at the end")
    need(not r.get("page_fatal"), "no fatal page error: %s" % r.get("page_fatal"))
    for s in r["sessions"]:
        sid = s["sid"]
        i = int(sid) - 1
        stn = r["stations"][i]
        ac = [c for _, c in s.get("answer_codecs", [])]
        want = sc["want_codec"]
        names = [c.split("/")[0] for c in ac]
        if want == "G722":
            need(names == ["G722", "PCMU"], "[%s] answer names G722 then PCMU only: %s" % (sid, ac))
        elif name == "C":
            need(names == ["PCMU", "G722"], "[%s] answer follows the offerer's order (PCMU, G722): %s" % (sid, ac))
        else:
            need(names == ["PCMU"], "[%s] answer names PCMU only: %s" % (sid, ac))
        need(s.get("answer_has_end_of_candidates") and s.get("answer_candidates", 0) > 0, "[%s] non-trickle answer with host candidates" % sid)
        need(s.get("final_cs") in ("connected", "closed") or name == "E", "[%s] browser connection state %s" % (sid, s.get("final_cs")))
        need(any(want.lower() in c.lower() for c in s.get("codecs_in", [])), "[%s] browser receives %s: %s" % (sid, want, s.get("codecs_in")))
        need(any(want.lower() in c.lower() for c in s.get("codecs_out", [])), "[%s] browser sends %s: %s" % (sid, want, s.get("codecs_out")))
        # the station is answered only after the browser connected
        if s.get("t_connected") and stn.get("t_200"):
            need(stn["t_200"] >= s["t_connected"] - 0.3, "[%s] 200 OK to the station (%.2f) not before the browser connected (%.2f)" % (sid, stn["t_200"] - t_start, s["t_connected"] - t_start))
        else:
            need(False, "[%s] 200 OK to the station and browser connected both observed" % sid)
        # tones, both ways
        bh = {int(k) for k in s.get("dom_hz", {})}
        near = lambda hz, tol: any(abs(h - hz) <= tol for h in bh)
        want_s, want_b = door_station_hz(sc, i), door_browser_hz(sc, i)
        need(near(want_s, 30), "[%s] the browser hears its own door's %d Hz: dominant %s" % (sid, want_s, s.get("dom_hz")))
        sh = stn["station_hears"]
        good = sum(c for k, c in sh["dominant_hz_hist"].items() if abs(int(k) - want_b) <= 60)
        need(sh["loud_windows"] > 10 and good >= 0.7 * sh["loud_windows"],
             "[%s] the station hears its own browser's %d Hz (%d of %d loud windows): %s" % (sid, want_b, good, sh["loud_windows"], sh["dominant_hz_hist"]))
        if name == "D":
            need(s.get("max_stall_ms", 99999) < 1000, "[D] ICE restart without an audible stall: max stall %s ms" % s.get("max_stall_ms"))
    if name == "E":
        need(r.get("station_bye_after_s") is not None, "[E] the station leg was hung up after the browser was killed: %s s" % r.get("station_bye_after_s"))
        need(r.get("slot_freed_second_ring_notified"), "[E] the call slot was freed (a new ring is announced)")
        need((r.get("ice_after_end") or {}).get("code") == "unknown_session", "[E] the dead call's session left the table: %s" % r.get("ice_after_end"))
    else:
        for i, stn in enumerate(r["stations"]):
            need(stn.get("bye_seen"), "[%d] the station was hung up when the browser closed" % (i + 1))
    if name == "H3":
        need(any("unknown_session" in str(x[3]) for x in r["ice_results"]) or not r["ice_results"], "[H3] candidates before the offer are answered unknown_session, as Go")
    return {"passed": passed, "failed": failed}


if __name__ == "__main__":
    main()
