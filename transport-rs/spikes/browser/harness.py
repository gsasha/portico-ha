#!/usr/bin/env python3
"""S1b harness (DEV-764): a REAL browser is the WebRTC offerer, a Rust spike answerer is the answerer.

Python 3 stdlib only. It serves page.html on 127.0.0.1, relays offer / answer / trickle between the page (HTTP)
and the answerer's line-JSON TCP socket (the S1 protocol), launches the browser (host Firefox as a plain process
in its own process group, or Chromium in `docker run --rm --network host`), collects the page's getStats()
reports and the answerer's JSON log, and prints one JSON result.

  harness.py --answerer BIN --browser firefox|chrome --scenario A|B|C|D|E|F|H --label NAME [options]

Everything is written under --out (default $SPIKE_LOGDIR or the cache dir), never into the worktree.
"""
import argparse, json, os, signal, socket, subprocess, sys, threading, time, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.environ.get("S1B_CACHE", "/home/agent/.claude/jobs/adec9d89/tmp/cache")


class Bridge:
    """One TCP connection to the answerer per session id."""

    def __init__(self, addr):
        self.addr = addr
        self.lock = threading.Lock()
        self.cv = threading.Condition(self.lock)
        self.conn = {}      # sid -> socket
        self.answers = {}   # sid -> [sdp,...] FIFO
        self.errors = {}
        self.cands = {}     # sid -> [cand,...] from the answerer
        self.closed = set()

    def get(self, sid):
        with self.lock:
            if sid in self.conn:
                return self.conn[sid]
            s = socket.create_connection(self.addr)
            self.conn[sid] = s
            self.answers[sid], self.cands[sid], self.errors[sid] = [], [], []
        threading.Thread(target=self._read, args=(sid, s), daemon=True).start()
        return s

    def _read(self, sid, s):
        f = s.makefile("r", encoding="utf-8", errors="replace")
        for line in f:
            try:
                m = json.loads(line)
            except Exception:
                continue
            with self.cv:
                if m.get("t") == "answer":
                    self.answers[sid].append(m["sdp"])
                elif m.get("t") == "cand":
                    self.cands[sid].append(m["c"])
                elif m.get("t") == "error":
                    self.errors[sid].append(m.get("msg", ""))
                self.cv.notify_all()
        with self.cv:
            self.closed.add(sid)
            self.cv.notify_all()

    def send(self, sid, obj):
        s = self.get(sid)
        s.sendall((json.dumps(obj) + "\n").encode())

    def wait_answer(self, sid, timeout):
        end = time.time() + timeout
        with self.cv:
            while True:
                if self.answers[sid]:
                    return self.answers[sid].pop(0), None
                if self.errors[sid]:
                    return None, self.errors[sid].pop(0)
                if sid in self.closed:
                    return None, "answerer closed the connection"
                left = end - time.time()
                if left <= 0:
                    return None, "timeout waiting for answer"
                self.cv.wait(left)


class State:
    def __init__(self):
        self.reports = {}   # sid -> [report,...]
        self.logs = {}      # sid -> [log,...]
        self.done = set()
        self.fatal = {}
        self.lock = threading.Lock()


def make_handler(bridge, st, page_path):
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
            q = urllib.parse.parse_qs(u.query)
            if u.path in ("/", "/page.html"):
                b = open(page_path, "rb").read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)
            elif u.path == "/cands":
                sid, since = q["sid"][0], int(q["since"][0])
                end = time.time() + 3
                while time.time() < end:
                    with bridge.lock:
                        c = bridge.cands.get(sid, [])
                        if len(c) > since:
                            return self._json({"c": c[since:], "n": len(c)})
                    time.sleep(0.1)
                with bridge.lock:
                    n = len(bridge.cands.get(sid, []))
                self._json({"c": [], "n": max(n, since)})
            else:
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()

        def do_POST(self):
            u = urllib.parse.urlparse(self.path)
            sid = urllib.parse.parse_qs(u.query).get("sid", ["1"])[0]
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n) or b"{}")
            if u.path == "/offer":
                bridge.send(sid, {"t": "offer", "sdp": body["sdp"]})
                sdp, err = bridge.wait_answer(sid, 30)
                self._json({"sdp": sdp} if sdp else {"error": err})
            elif u.path == "/cand":
                bridge.send(sid, {"t": "cand", "c": body["c"]})
                self._json({})
            elif u.path == "/bye":
                try:
                    bridge.send(sid, {"t": "bye"})
                except Exception:
                    pass
                self._json({})
            elif u.path == "/report":
                with st.lock:
                    if body.get("done"):
                        st.done.add(sid)
                    elif body.get("fatal"):
                        st.fatal[sid] = body["fatal"]
                    else:
                        body["srv_t"] = time.time()
                        st.reports.setdefault(sid, []).append(body)
                self._json({})
            elif u.path == "/log":
                with st.lock:
                    body["srv_t"] = time.time()
                    st.logs.setdefault(sid, []).append(body)
                self._json({})
            else:
                self._json({}, 404)

    return H


def start_answerer(binary, extra, logpath):
    log = open(logpath, "w")
    p = subprocess.Popen([binary, "--listen", "127.0.0.1:0", "--snap", "5"] + extra, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    for _ in range(100):
        time.sleep(0.1)
        for line in open(logpath):
            if '"ev":"LISTEN"' in line.replace(" ", ""):
                addr = json.loads(line)["addr"]
                h, port = addr.rsplit(":", 1)
                return p, (h, int(port)), json.loads(line)
    raise RuntimeError("answerer did not start")


def read_log(path):
    out = []
    for line in open(path, errors="replace"):
        try:
            out.append(json.loads(line))
        except Exception:
            out.append({"raw": line.strip()})
    return out


def write_ff_profile(d, obfuscate):
    os.makedirs(d, exist_ok=True)
    prefs = {
        "media.navigator.streams.fake": True,
        "media.navigator.permission.disabled": True,
        "media.peerconnection.ice.obfuscate_host_addresses": obfuscate,
        "media.peerconnection.ice.obfuscate_host_addresses.blocklist": "",
        "media.autoplay.default": 0,
        "datareporting.policy.dataSubmissionEnabled": False,
        "app.update.enabled": False,
        "browser.shell.checkDefaultBrowser": False,
        "toolkit.telemetry.reportingpolicy.firstRun": False,
        "network.captive-portal-service.enabled": False,
        "network.connectivity-service.enabled": False,
        "media.peerconnection.ice.loopback": True,
        "media.peerconnection.ice.no_host": False,
        "browser.startup.homepage_override.mstone": "ignore",
    }
    with open(os.path.join(d, "user.js"), "w") as f:
        for k, v in prefs.items():
            f.write('user_pref("%s", %s);\n' % (k, json.dumps(v)))


class Browser:
    def __init__(self, kind, url, name, args, profile_dir, logpath):
        self.kind, self.url, self.name = kind, url, name
        self.log = open(logpath, "w")
        if kind == "firefox":
            write_ff_profile(profile_dir, args.obfuscate)
            env = dict(os.environ, HOME=CACHE + "/home", MOZ_HEADLESS="1", MOZ_DISABLE_CONTENT_SANDBOX="0")
            self.p = subprocess.Popen(["/usr/bin/firefox", "--headless", "--no-remote", "--profile", profile_dir, url],
                                      stdout=self.log, stderr=subprocess.STDOUT, env=env, start_new_session=True)
            self.pid = self.p.pid
        else:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True)
            cmd = ["docker", "run", "--rm", "--name", name, "--network", "host", "zenika/alpine-chrome:latest",
                   "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
                   "--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream",
                   "--autoplay-policy=no-user-gesture-required", "--no-first-run",
                   "--user-data-dir=/tmp/chrome-profile", "--remote-debugging-port=%d" % (9330 + int(name.rsplit("-", 1)[1]))] + args.chrome_flags + [url]
            self.p = subprocess.Popen(cmd, stdout=self.log, stderr=subprocess.STDOUT, start_new_session=True)
            self.pid = self.p.pid

    def kill(self):
        """Firefox: kill the process group of the PID we started (exact, never by name). Chrome: SIGKILL the
        container we named (docker kill), which is the exact container of this run."""
        t = time.time()
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
            try:
                os.killpg(self.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            time.sleep(1)
            try:
                os.killpg(self.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        else:
            subprocess.run(["docker", "rm", "-f", self.name], capture_output=True)
        try:
            self.p.wait(timeout=10)
        except Exception:
            pass


def summarise(sid, reports, logs):
    rs = [r for r in reports if "in" in r or "out" in r]
    res = {"sid": sid, "reports": len(rs)}
    if not rs:
        return res
    last = rs[-1]
    res["final_cs"] = last.get("cs")
    res["final_ics"] = last.get("ics")
    res["in"] = last.get("in")
    res["out"] = last.get("out")
    res["pair"] = next((r["pair"] for r in reversed(rs) if r.get("pair")), None)
    res["max_audioLevel"] = max([(r.get("in") or {}).get("audioLevel") or 0 for r in rs] + [0])
    res["max_ssrcAudioLevel"] = max([r.get("ssrcAudioLevel") or 0 for r in rs] + [0])
    res["max_stall_ms"] = max([r.get("maxStallMs") or 0 for r in rs] + [0])
    res["codecs_in"] = sorted({(r.get("in") or {}).get("codec") for r in rs if r.get("in")} - {None})
    res["codecs_out"] = sorted({(r.get("out") or {}).get("codec") for r in rs if r.get("out")} - {None})
    res["states"] = []
    res["dom_hz_browser"] = sorted({r.get("domHz") for r in rs if r.get("domHz") is not None})
    for l in sorted(logs, key=lambda x: x.get("pt", 0)):
        m = l.get("m", "")
        if m.startswith(("connectionState", "iceConnectionState", "restartIce", "restart ", "answer received", "offer failed", "page error")):
            res["states"].append([l["pt"], m])
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--answerer", required=True)
    ap.add_argument("--browser", choices=["firefox", "chrome"], required=True)
    ap.add_argument("--scenario", default="A", help="A plain | B opus-first (same flow, codec checked) | C candfirst | D restart | E kill | F two sessions | H mdns")
    ap.add_argument("--label", default="run")
    ap.add_argument("--duration", type=float, default=60)
    ap.add_argument("--restart-at", type=float, default=0)
    ap.add_argument("--kill-at", type=float, default=0)
    ap.add_argument("--browsers", type=int, default=1)
    ap.add_argument("--mode", default="normal")
    ap.add_argument("--src", default="fake")
    ap.add_argument("--obfuscate", type=lambda s: s == "true", default=True, help="Firefox media.peerconnection.ice.obfuscate_host_addresses")
    ap.add_argument("--chrome-flag", action="append", default=[], dest="chrome_flags")
    ap.add_argument("--answerer-arg", action="append", default=[])
    ap.add_argument("--out", default=os.environ.get("SPIKE_LOGDIR", CACHE + "/runs"))
    ap.add_argument("--wait-after-kill", type=float, default=40)
    args = ap.parse_args()

    run = "%s-%s-%s" % (args.label, args.scenario, time.strftime("%H%M%S"))
    out = os.path.join(args.out, run)
    os.makedirs(out, exist_ok=True)
    ap_proc, addr, listen = start_answerer(args.answerer, args.answerer_arg, out + "/answerer.log")
    bridge, st = Bridge(addr), State()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(bridge, st, HERE + "/page.html"))
    srv.daemon_threads = True
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    result = {"run": run, "args": vars(args), "answerer_listen": listen, "http_port": port, "out": out}

    browsers = []
    t_start = time.time()
    kill_t = None
    try:
        for i in range(args.browsers):
            sid = str(i + 1)
            qs = "sid=%s&src=%s&mode=%s&duration=%s&restart_at=%s" % (sid, args.src, args.mode, args.duration, args.restart_at)
            url = "http://127.0.0.1:%d/?%s" % (port, qs)
            b = Browser(args.browser, url, "s1b-chrome-%d-%d" % (os.getpid(), i), args, "%s/ffprofile-%s-%d" % (CACHE, run, i), "%s/browser-%d.log" % (out, i))
            browsers.append(b)
            result.setdefault("browser_pids", []).append(b.pid)
            time.sleep(0.5)
        deadline = time.time() + args.duration + 45 + (args.wait_after_kill if args.kill_at else 0)
        while time.time() < deadline:
            time.sleep(0.5)
            with st.lock:
                if st.fatal:
                    break
                if args.kill_at and kill_t is None:
                    firsts = [r["sinceConnectedMs"] for rs in st.reports.values() for r in rs if r.get("sinceConnectedMs") is not None]
                    if firsts and max(firsts) / 1000 >= args.kill_at:
                        kill_t = "now"
            if kill_t == "now":
                kill_t = browsers[0].kill()
                result["kill_t_ms"] = int(kill_t * 1000)
                time.sleep(args.wait_after_kill)
                break
            with st.lock:
                if len(st.done) >= args.browsers:
                    time.sleep(1.5)
                    break
        result["page_done"] = sorted(st.done)
        result["page_fatal"] = st.fatal
    finally:
        for b in browsers:
            b.stop()
        time.sleep(1)
        try:
            os.killpg(ap_proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            ap_proc.wait(timeout=10)
        except Exception:
            os.killpg(ap_proc.pid, signal.SIGKILL)
        srv.shutdown()

    alog = read_log(out + "/answerer.log")
    result["wall_s"] = round(time.time() - t_start, 1)
    result["sessions"] = [summarise(s, st.reports.get(s, []), st.logs.get(s, [])) for s in sorted((set(st.reports) | set(st.logs)) - {"0"})]
    # the answerer side
    result["answerer_reports"] = [r for r in alog if r.get("ev") == "REPORT"]
    result["answerer_states"] = [[r["t_ms"], r["id"], r["kind"], r["state"]] for r in alog if r.get("ev") == "state"]
    result["answerer_events"] = [r for r in alog if r.get("ev") in ("offer-accepted", "offer-rejected", "offer-PANIC-CAUGHT", "end-of-candidates", "remote-cand", "answer-sent", "SESSION_PANIC")
                                  and not (r.get("ev") == "offer-accepted" and False)]
    if "kill_t_ms" in result:
        for r in alog:
            if r.get("ev") == "state" and r.get("t_ms", 0) >= result["kill_t_ms"] and r.get("state") == "Disconnected" and r.get("kind") == "ice":
                result["disconnect_detected_after_ms"] = r["t_ms"] - result["kill_t_ms"]
                break
    # SDPs
    sd = {}
    for s, ls in st.logs.items():
        for l in ls:
            if l.get("sdp"):
                sd.setdefault(s, []).append({"what": l["m"], "t": l["srv_t"], "sdp": l["sdp"]})
    def codecs(sdp):
        maps = {}
        for l in sdp.splitlines():
            if l.startswith("a=rtpmap:"):
                a, b = l[9:].split(" ", 1)
                maps[a] = b
        for l in sdp.splitlines():
            if l.startswith("m=audio"):
                return [(pt, maps.get(pt, "?")) for pt in l.split()[3:]], l.split()[1]
        return [], None
    for s_, ls in sd.items():
        o = [x for x in ls if x["what"].startswith("offer")]
        a = [x for x in ls if x["what"].startswith("answer received")]
        if o and a:
            result.setdefault("codec_summary", {})[s_] = {"offer_pt_order": codecs(o[0]["sdp"])[0], "answer": codecs(a[0]["sdp"])[0], "answer_m_port": codecs(a[0]["sdp"])[1],
                                                          "offer_candidates_in_sdp": o[-1]["sdp"].count("a=candidate:"), "answer_candidates": a[0]["sdp"].count("a=candidate:")}
    json.dump(sd, open(out + "/sdp.json", "w"), indent=1)
    json.dump({k: [{kk: vv for kk, vv in l.items() if kk != "sdp"} for l in v] for k, v in st.logs.items()}, open(out + "/pagelog.json", "w"), indent=1)
    json.dump(result, open(out + "/result.json", "w"), indent=1)
    json.dump({s: st.reports.get(s, []) for s in st.reports}, open(out + "/reports.json", "w"))
    # compact print: drop SDP bodies
    for e in result["answerer_events"]:
        e.pop("answer_sdp", None)
    print(json.dumps(result, indent=1))


if __name__ == "__main__":
    main()
