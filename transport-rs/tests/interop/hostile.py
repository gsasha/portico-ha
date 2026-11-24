#!/usr/bin/env python3
"""Scenario G: hostile offers, candidates and answers over the transport's IPC. The process must
survive every one, answer each with an error (never a hang or a crash) using Go's codes, and still
answer a valid offer afterwards. Python 3 standard library only.

  hostile.py BINARY [KEY=VALUE ...]      (KEY=VALUE are set in the transport's environment)

Prints one line per case and a final JSON verdict; exit status 0 when everything held. Pure IPC:
no browser.
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
FX = os.path.join(HERE, "fixtures")


def crlf(t):
    return t.replace("\r\n", "\n").replace("\n", "\r\n")


CHROME = crlf(open(os.path.join(FX, "chrome124_offer.sdp")).read())
FIREFOX = crlf(open(os.path.join(FX, "firefox153_offer.sdp")).read())


class Ipc:
    def __init__(self, binary, env):
        self.tmp = tempfile.mkdtemp(prefix="hostile-", dir=os.environ.get("INTEROP_OUT"))
        self.sock = os.path.join(self.tmp, "t.sock")
        e = dict(os.environ)
        e.update(env)
        self.p = subprocess.Popen([binary, "-socket", self.sock], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                  env=e, start_new_session=True)
        for _ in range(100):
            if os.path.exists(self.sock):
                break
            time.sleep(0.05)
        self.s = socket.socket(socket.AF_UNIX)
        self.s.connect(self.sock)
        self.f = self.s.makefile("rwb")
        self.i = 0

    def call(self, method, params, timeout=20):
        self.i += 1
        line = json.dumps({"id": self.i, "method": method, "params": params}) + "\n"
        self.s.settimeout(timeout)
        self.s.sendall(line.encode())
        while True:
            raw = self.f.readline()
            if not raw:
                return {"error": {"code": "closed", "message": "connection closed"}}
            m = json.loads(raw)
            if m.get("id") == self.i:
                return m

    def broken_line(self, data):
        """A line that is not a valid request, on a connection of its own (the server may drop it)."""
        c = socket.socket(socket.AF_UNIX)
        c.connect(self.sock)
        c.sendall(data)
        time.sleep(0.3)
        c.close()

    def alive(self):
        return self.p.poll() is None

    def stop(self):
        try:
            os.killpg(self.p.pid, 15)
        except ProcessLookupError:
            pass
        self.p.wait(timeout=10)


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    # the flood below keeps 60 valid calls alive for the 30 s connect timeout: lift the default cap of 8
    env = {"PORTICO_SFU_MAX_SESSIONS": "64"}
    env.update(dict(a.split("=", 1) for a in sys.argv[2:] if "=" in a))
    ipc = Ipc(sys.argv[1], env)
    failures = []
    n = [0]

    def case(name, method, params, want_codes, timeout=20):
        n[0] += 1
        t0 = time.time()
        r = ipc.call(method, params, timeout)
        dt = time.time() - t0
        code = (r.get("error") or {}).get("code", "ok" if "result" in r else "?")
        msg = ((r.get("error") or {}).get("message") or "")[:90].replace("\n", " ")
        good = code in want_codes and ipc.alive() and dt < timeout
        print("%-4s %-52s -> %-16s %5.2fs  %s" % ("ok" if good else "FAIL", name, code, dt, msg), flush=True)
        if not good:
            failures.append(name)
        return r

    sdp = CHROME
    big = sdp + "a=x-pad:" + "y" * 70000 + "\r\n"
    many = sdp + "a=x-pad:1\r\n" * 1500
    mega = "v=0\r\n" + "a=x:" + "z" * (900 * 1024) + "\r\n"
    cases = [
        ("empty sdp", {"call_id": "g", "sdp": ""}, ["bad_params"]),
        ("not an sdp", {"call_id": "g1", "sdp": "garbage"}, ["webrtc_failed"]),
        ("a binary blob", {"call_id": "g2", "sdp": "".join(chr(i % 256) for i in range(5000))}, ["webrtc_failed"]),
        ("NULs after v=0", {"call_id": "g3", "sdp": "v=0\r\n\x00\x00\x00\x01\x02"}, ["webrtc_failed"]),
        ("LF-only line endings", {"call_id": "g4", "sdp": sdp.replace("\r\n", "\n")}, ["webrtc_failed", "ok"]),
        ("cut in half", {"call_id": "g5", "sdp": sdp[: len(sdp) // 2]}, ["webrtc_failed"]),
        ("cut after the m-line", {"call_id": "g6", "sdp": sdp[: sdp.index("m=audio") + 40]}, ["webrtc_failed"]),
        ("no m-line", {"call_id": "g7", "sdp": "v=0\r\no=- 1 2 IN IP4 127.0.0.1\r\ns=-\r\nt=0 0\r\n"}, ["webrtc_failed"]),
        ("fingerprint of garbage", {"call_id": "g8", "sdp": sdp.replace("a=fingerprint:sha-256 ", "a=fingerprint:sha-256 zz")}, ["webrtc_failed", "ok"]),
        ("a=setup:bogus", {"call_id": "g9", "sdp": sdp.replace("a=setup:actpass", "a=setup:bogus")}, ["webrtc_failed", "ok"]),
        ("payload type 99999 in the m-line", {"call_id": "g10", "sdp": sdp.replace("SAVPF 111", "SAVPF 99999 111")}, ["webrtc_failed", "ok"]),
        ("absurd rtpmap rate", {"call_id": "g11", "sdp": sdp.replace("PCMU/8000", "PCMU/99999999999")}, ["webrtc_failed", "ok"]),
        ("video-only offer", {"call_id": "g12", "sdp": sdp.replace("m=audio", "m=video")}, ["webrtc_failed", "ok"]),
        ("Opus only", {"call_id": "g13", "sdp": sdp.replace("SAVPF 111 63 9 0 8 13 110 126", "SAVPF 111")}, ["webrtc_failed"]),
        ("64 KiB+ of attributes", {"call_id": "g14", "sdp": big}, ["webrtc_failed"]),
        ("1500 lines", {"call_id": "g15", "sdp": many}, ["webrtc_failed"]),
        ("900 KiB single line", {"call_id": "g16", "sdp": mega}, ["webrtc_failed"]),
        ("15000 candidates in the offer (a 1 MiB IPC line ends the server, as in Go)", {"call_id": "g17", "sdp": sdp + "a=candidate:1 1 udp 1 1.2.3.4 5 typ host\r\n" * 15000}, ["webrtc_failed"]),
        ("call_id of the wrong type", {"call_id": 5, "sdp": "x"}, ["bad_params"]),
        ("sdp of the wrong type", {"call_id": "g18", "sdp": 5}, ["bad_params"]),
        ("a 100 KiB call id", {"call_id": "c" * 100000, "sdp": "garbage"}, ["webrtc_failed"]),
    ]
    for name, params, want in cases:
        case(name, "webrtc_offer", params, want)
    # candidates and answers against sessions in different states
    case("candidate for no session", "webrtc_ice", {"call_id": "nobody", "candidate": "x"}, ["unknown_session"])
    case("answer for no session", "webrtc_answer", {"call_id": "nobody", "sdp": "x"}, ["unknown_session"])
    case("candidate after a refused first offer (its session is gone)", "webrtc_ice", {"call_id": "g1", "candidate": ""}, ["unknown_session"])
    case("answer after a refused first offer (its session is gone)", "webrtc_answer", {"call_id": "g1", "sdp": sdp}, ["unknown_session"])
    case("a VALID offer after all of that", "webrtc_offer", {"call_id": "valid-1", "sdp": sdp}, ["ok"])
    for name, c, want in [
        ("garbage candidate", "garbage", ["webrtc_failed"]),
        ("candidate with 5 fields", "candidate:1 1 udp 1 1.2.3.4", ["webrtc_failed"]),
        ("candidate with a 70000 port", "candidate:1 1 udp 1 1.2.3.4 70000 typ host", ["webrtc_failed", "ok"]),
        ("candidate with a 1 MB tail", "candidate:1 1 udp 1 1.2.3.4 5 typ host " + "x" * 1000000, ["webrtc_failed", "ok"]),
        ("an mDNS candidate", "candidate:1 1 udp 2130706431 abcd-1234.local 5000 typ host", ["ok"]),
        ("end-of-candidates (empty)", "", ["ok"]),
        ("end-of-candidates (marker)", "a=end-of-candidates", ["ok"]),
        ("a good candidate", "candidate:1 1 udp 2130706431 127.0.0.1 5000 typ host", ["ok"]),
    ]:
        case(name, "webrtc_ice", {"call_id": "valid-1", "candidate": c}, want)
    bad_reneg = 0
    for _ in range(50):  # the same call renegotiated fifty times
        r = ipc.call("webrtc_offer", {"call_id": "valid-1", "sdp": sdp})
        if "result" not in r:
            bad_reneg += 1
    print("%-4s %-52s (%d errors)" % ("ok" if not bad_reneg else "FAIL", "50 renegotiations of one call", bad_reneg), flush=True)
    if bad_reneg:
        failures.append("renegotiations")
    t0 = time.time()
    bad = 0
    for i in range(50):  # a flood of distinct calls
        r = ipc.call("webrtc_offer", {"call_id": "flood-%d" % i, "sdp": FIREFOX if i % 2 else CHROME})
        if "result" not in r:
            bad += 1
    print("%-4s %-52s %5.2fs  (%d errors)" % ("ok" if bad == 0 and ipc.alive() else "FAIL", "50 distinct calls", time.time() - t0, bad), flush=True)
    if bad:
        failures.append("flood")
    ipc.broken_line(b'{"id":7,"method":"webrtc_offer","params":{"call_id":"x","sdp":"' + b"A" * 2000 + b'"\n')  # broken JSON
    print("%-4s %-52s" % ("ok" if ipc.alive() else "FAIL", "a truncated JSON line"), flush=True)
    r = ipc.call("webrtc_offer", {"call_id": "after-all", "sdp": sdp})
    good = "result" in r and ipc.alive()
    print("%-4s %-52s" % ("ok" if good else "FAIL", "a valid offer at the very end"), flush=True)
    if not good:
        failures.append("final valid offer")
    survived = ipc.alive()
    ipc.stop()
    print(json.dumps({"scenario": "G", "cases": n[0] + 3, "failed": failures, "process_survived": survived}))
    sys.exit(1 if failures or not survived else 0)


if __name__ == "__main__":
    main()
