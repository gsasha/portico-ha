#!/usr/bin/env python3
"""Black-box IPC smoke script for portico-transport (Go or Rust).

Usage: ipc_smoke.py <binary>

Spawns the binary with `-socket <tmpdir>/t.sock`, sends a fixed script of newline-delimited
JSON requests and prints every response normalized as sorted-key JSON (the raw line is shown
too when it differs from that canonical form, which exposes string-escaping differences).
Then it checks SIGTERM, client-disconnect, flag-error and stale-socket behaviour.

Run it against the Go binary and the Rust binary and diff the two outputs. Python 3 standard
library only. Takes about 15 seconds (it waits out the 5 s media idle timeout once).
"""
import base64
import hashlib
import json
import os
import re
import select
import signal
import socket
import ssl
import stat
import subprocess
import sys
import shutil
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

KNOWN_METHODS = [
    "configure", "deconfigure", "dial", "answer", "open_relay", "stream_url", "hangup",
    "keepalive", "webrtc_offer", "webrtc_answer", "webrtc_ice", "media_open", "media_frame",
    "media_close", "house_open", "house_close", "external_ring", "talk_health",
]

OUT = []


def say(s=""):
    OUT.append(s)
    print(s, flush=True)


def canon(line):
    try:
        return json.dumps(json.loads(line), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except ValueError:
        return "<not json> " + repr(line)


class Proc:
    def __init__(self, binary, tmp, extra=(), sock_name="t.sock", env=None):
        self.tmp = tmp
        self.sock = os.path.join(tmp, sock_name)
        self.log = open(os.path.join(tmp, "out-%s.log" % sock_name), "w+b")
        self.p = subprocess.Popen([binary, "-socket", self.sock, *extra], stdout=self.log,
                                  stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=env)
        self.s = None
        self.buf = b""

    def connect(self):
        for _ in range(100):
            if os.path.exists(self.sock):
                try:
                    s = socket.socket(socket.AF_UNIX)
                    s.connect(self.sock)
                    self.s = s
                    return
                except OSError:
                    s.close()
            time.sleep(0.05)
        raise SystemExit("could not connect to " + self.sock)

    def send(self, data):
        if isinstance(data, str):
            data = data.encode()
        self.s.sendall(data)

    def collect(self, n, timeout=3.0):
        """Read n newline-terminated lines (or until timeout)."""
        lines = []
        end = time.time() + timeout
        while len(lines) < n and time.time() < end:
            while b"\n" in self.buf and len(lines) < n:
                line, self.buf = self.buf.split(b"\n", 1)
                lines.append(line.decode("utf-8", "replace"))
            if len(lines) >= n:
                break
            r, _, _ = select.select([self.s], [], [], max(0.0, end - time.time()))
            if r:
                try:
                    d = self.s.recv(65536)
                except OSError:
                    break
                if not d:
                    break
                self.buf += d
        return lines

    def quiet(self, secs=0.4):
        """Anything arriving in the next `secs` seconds (should be nothing)."""
        return self.collect(10 ** 6, secs)

    def log_text(self):
        self.log.flush()
        self.log.seek(0)
        return self.log.read().decode("utf-8", "replace").replace(self.tmp, "<TMP>")

    def wait(self, timeout=5.0):
        try:
            return self.p.wait(timeout)
        except subprocess.TimeoutExpired:
            return None


def is_reply(line):
    """A response line (`id` and no `method`), as opposed to a notification."""
    try:
        d = json.loads(line)
    except ValueError:
        return False
    return isinstance(d, dict) and "method" not in d and "id" in d


def reply_floats(lines, after):
    """PARITY 360 to 362 (R2-order): the order of a reply against the notifications of its request,
    for a step where Go orders the NOTIFICATIONS (one goroutine or one queue raises them) but races
    the reply against them. The notifications keep their arrival order, the reply goes where it fits
    (it may come anywhere from after the `after`-th notification on) and is printed right after that
    one: `after` is the number of notifications Go writes in the handler's own goroutine, before the
    reply (0 = the reply is free). A reply that came earlier than that, or a step without exactly one
    reply, is printed as a `!!` line the reference transcript cannot contain."""
    reps = [i for i, l in enumerate(lines) if is_reply(l)]
    if len(reps) != 1:
        return list(lines), ["  !! expected exactly one reply, got %d" % len(reps)]
    i = reps[0]
    notes = lines[:i] + lines[i + 1:]
    bad = []
    if i < after:
        bad.append("  !! the reply came after %d notification(s), %d must precede it" % (i, after))
    return notes[:after] + [lines[i]] + notes[after:], bad


def show(title, lines, expected, ordered=True, reply_after=None):
    """`ordered`: True = arrival order, False = sorted (Go's order is open). `reply_after=k`: only the
    reply floats, see `reply_floats`."""
    say("## " + title)
    got = list(lines)
    bad = []
    if reply_after is not None:
        got, bad = reply_floats(got, reply_after)
    elif not ordered:
        got.sort(key=canon)
    for l in got:
        c = canon(l)
        say("  " + c)
        if l != c:
            say("    raw=" + repr(l))
    for b in bad:
        say(b)
    if len(lines) != expected:
        say("  !! expected %d line(s), got %d" % (expected, len(lines)))


def step(pr, title, payload, expected, ordered=True, timeout=3.0, reply_after=None):
    pr.send(payload)
    show(title, pr.collect(expected, timeout), expected, ordered, reply_after)


def j(**kw):
    return json.dumps(kw, separators=(",", ":"))


def main_script(binary, tmp):
    pr = Proc(binary, tmp)
    pr.connect()
    say("# main script")

    # 1. lines that must be dropped silently; a probe proves nothing came back for them.
    bad = [
        "not json", '{"id":1,', "[]", '"x"', "42", "true", "",
        '{"id":"s","method":"dial"}', '{"id":1.5,"method":"dial"}', '{"id":1,"method":5}',
        '{"id":1e2,"method":"dial"}', '{"id":null,"method":123}', "{}}",
    ]
    step(pr, "malformed lines then probe", "\n".join(bad) + '\n{"id":99,"method":"keepalive"}\n', 1)
    # CRLF line ending is accepted
    step(pr, "crlf line", '{"id":98,"method":"keepalive"}\r\n', 1)
    # whitespace-padded and no-trailing-newline lines are exercised via pipelining below

    # 2. null line = zero Request; unknown methods; escaping.
    step(pr, "null line", "null\n", 1)
    step(pr, "unknown method", j(id=2, method="nope") + "\n", 1)
    step(pr, "unknown method with escapes",
         '{"id":3,"method":"a<b>&\\u2028\\u2029\\b\\f\\u0001\\"\\\\é"}\n', 1)
    step(pr, "missing id", '{"method":"keepalive"}\n', 1)
    step(pr, "missing method", '{"id":7}\n', 1)
    step(pr, "case-insensitive keys", '{"ID":4,"METHOD":"keepalive"}\n', 1)
    step(pr, "duplicate keys, last wins", '{"id":1,"id":5,"method":"keepalive","method":"nope"}\n', 1)
    step(pr, "id null/negative/large (order open in Go: concurrent keyed requests)", '{"id":null,"method":"keepalive"}\n{"id":-3,"method":"keepalive"}\n'
         '{"id":9223372036854775807,"method":"keepalive"}\n{"id":9223372036854775808,"method":"keepalive"}\n'
         '{"id":99,"method":"keepalive"}\n', 4, ordered=False)

    # 3. each known method, empty state, a spread of params shapes.
    shapes = [
        None, "{}", "null", '{"device_id":"d1"}', '{"host":"h1","port":5060}', '"str"', "[1]", "7", "true",
        '{"device_id":5}', '{"port":1.5}', '{"device_id":["x"],"host":{}}', '{"DEVICE_ID":"cased"}',
        '{"call_id":"c1"}', '{"call_id":"c1","sdp":"v=0"}', '{"call_id":"c1","candidate":"x"}',
        '{"driver":"nonexistent","config":{}}', '{"driver":"gate_sip_server","config":{"device_id":"g1"}}',
        '{"driver":5}', '{"driver":"x","config":"s"}', '{"device_id":"d","call_id":"c","hold":10}',
        '{"device_id":"d","call_id":"c","hold":4000}', '{"device_id":"d","call_id":"c","hold":-1}',
        '{"device_id":"d","call_id":"c","hold":"x"}',
    ]
    for m in KNOWN_METHODS:
        if m.startswith("media_"):
            continue  # stateful; scripted below
        reqs = []
        for i, sh in enumerate(shapes, 1):
            if sh is None:
                reqs.append('{"id":%d,"method":"%s"}' % (i, m))
            else:
                reqs.append('{"id":%d,"method":"%s","params":%s}' % (i, m, sh))
        # webrtc_offer with call_id+sdp reaches Go's WebRTC stack: not comparable, see PARITY.md.
        if m == "webrtc_offer":
            reqs = [r for r in reqs if '"sdp":"v=0"' not in r]
        step(pr, "method " + m + " (order open in Go: concurrent keyed requests)", "\n".join(reqs) + "\n", len(reqs), ordered=False)

    # 3b. configure of gate_sip_server: validation is ported (slice 3a), so every refusal must
    # match Go byte for byte. A config that PASSES validation starts the server in both binaries
    # (since slice 3b-1); that is scripted separately in gate_sip_server_flow, because the Go
    # server logs to the process output and would put its ports into this script's log.
    gss = [
        'null', '{}', '{"password":"s3cretpass"}', '{"sip_listen":"127.0.0.1:5060"}',
        '{"sip_listen":"127.0.0.1:5060","password":"short"}',
        '{"sip_listen":"127.0.0.1:5060","password":"123456789012345678"}',
        '{"sip_listen":"0.0.0.0:5060","password":"s3cretpass"}',
        '{"sip_listen":"0.0.0.0:5060","password":"s3cretpass","allow_non_loopback":true}',
        '{"sip_listen":"0.0.0.0:5060","password":"s3cretpass","allow_non_loopback":true,"allow_from":["0.0.0.0/0"]}',
        '{"sip_listen":"127.0.0.1:5060","password":"s3cretpass","allow_from":["nope"]}',
        '{"sip_listen":"127.0.0.1:5060","password":"s3cretpass","allow_from":5}',
        '{"sip_listen":"127.0.0.1:5060","password":"s3cretpass","rtp_port":70000}',
        '{"sip_listen":"127.0.0.1:5060","password":"s3cretpass","ring_seconds":0}',
        '{"sip_listen":"127.0.0.1:5060","password":"s3cretpass","orphan_seconds":"x"}',
        '{"sip_listen":"127.0.0.1:5060","password":"s3cretpass","dial_enabled":"yes"}',
        '{"sip_listen":"127.0.0.1","password":"s3cretpass"}',
        '{"sip_listen":"127.0.0.1:99999","password":"s3cretpass"}',
        '{"sip_listen":"[::1","password":"s3cretpass"}',
    ]
    reqs = ['{"id":%d,"method":"configure","params":{"driver":"gate_sip_server","config":%s}}' % (i, c)
            for i, c in enumerate(gss, 1)]
    step(pr, "configure gate_sip_server refusals (order open in Go)", "\n".join(reqs) + "\n", len(reqs), ordered=False)

    # 4. media lane (inline, ordered).
    step(pr, "media_open bad", "\n".join([
        '{"id":1,"method":"media_open"}', '{"id":2,"method":"media_open","params":{}}',
        '{"id":3,"method":"media_open","params":{"call_id":"m1"}}',
        '{"id":4,"method":"media_open","params":{"call_id":"m1","codec":"opus"}}',
        '{"id":5,"method":"media_open","params":"x"}',
    ]) + "\n", 5, ordered=True)
    step(pr, "media_open pcmu", '{"id":1,"method":"media_open","params":{"call_id":"m1","codec":"pcmu"}}\n', 1)
    step(pr, "media_open again", '{"id":2,"method":"media_open","params":{"call_id":"m1","codec":"l16"}}\n', 1)
    import base64
    f320 = base64.b64encode(bytes(320)).decode()
    f1280 = base64.b64encode(bytes(1280)).decode()
    frames = [
        '{"id":1,"method":"media_frame","params":{"call_id":"m1","d":"%s"}}' % f320,
        '{"id":2,"method":"media_frame","params":{"call_id":"m1","d":"%s"}}' % f1280,
        '{"id":3,"method":"media_frame","params":{"call_id":"m1","d":"!!"}}',
        '{"id":4,"method":"media_frame","params":{"call_id":"nope","d":"%s"}}' % f320,
        '{"id":5,"method":"media_frame","params":{"call_id":"m1","d":"%s"}}' % f320,
        '{"id":6,"method":"media_frame"}',
    ]
    step(pr, "media_frame", "\n".join(frames) + "\n", 6, ordered=True)
    step(pr, "media_close (notification then response)",
         '{"id":1,"method":"media_close","params":{"call_id":"m1","reason":"bye"}}\n', 2, ordered=True)
    step(pr, "media_close unknown", '{"id":2,"method":"media_close","params":{"call_id":"m1"}}\n', 1)
    step(pr, "media_frame after close", frames[0].replace('"id":1', '"id":7') + "\n", 1)
    # idle close: no frame for 5 s
    pr.send('{"id":1,"method":"media_open","params":{"call_id":"m2","codec":"l16"}}\n')
    show("media_open l16 then idle close", pr.collect(2, 8.0), 2, ordered=True)

    # 5. pipelining: two requests in one write, plus a final line without a newline.
    step(pr, "pipelined pair", '{"id":10,"method":"keepalive"}\n{"id":11,"method":"nope"}\n', 2)
    step(pr, "no trailing newline is held until the newline arrives",
         '{"id":12,"method":"keepalive"}', 0, timeout=0.5)
    step(pr, "...newline", "\n", 1)

    # 6. long lines.
    def padded(total):
        head = '{"id":5,"method":"keepalive","params":"'
        tail = '"}'
        return head + "A" * (total - len(head) - len(tail)) + tail

    step(pr, "line of 1048575 bytes (+newline = 1 MiB) is accepted", padded(1048575) + "\n", 1, timeout=10)
    step(pr, "line of 600000 bytes", padded(600000) + "\n", 1, timeout=10)
    # over the limit: the scanner fails, the process exits 1 and leaves the socket file behind.
    try:
        pr.send(padded(1048576) + "\n")
    except OSError as e:
        say("send error: %r" % e)
    say("## over-limit line (1048576 bytes + newline)")
    rest = pr.collect(1, 3.0)
    for l in rest:
        say("  response: " + canon(l))
    code = pr.wait(5)
    say("  exit code: %s" % code)
    say("  socket file left behind: %s" % os.path.exists(pr.sock))
    say("  process output: " + repr(pr.log_text()))


def free_udp_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def md5(x):
    return hashlib.md5(x.encode()).hexdigest()


def gate_sip_server_flow(binary, tmp):
    """A valid configure of gate_sip_server, then a fake station over UDP loopback: REGISTER,
    INVITE, answer through the IPC, BYE; every IPC line (results and notifications) is printed.
    The same script runs against both binaries and must print the same."""
    say("# gate_sip_server: configure, a station, the device verbs, deconfigure")
    sip, rtp = free_udp_port(), free_udp_port()
    pr = Proc(binary, tmp, sock_name="s.sock")
    pr.connect()
    dev = "smoke-gss"
    cfg = '{"driver":"gate_sip_server","config":{"device_id":"%s","sip_listen":"127.0.0.1:%d","rtp_port":%d,"password":"s3cretpass","dial_enabled":true}}' % (dev, sip, rtp)
    step(pr, "configure (the reply is the Capabilities struct in field order)",
         '{"id":1,"method":"configure","params":%s}\n' % cfg, 1)
    step(pr, "configure the same device again", '{"id":2,"method":"configure","params":%s}\n' % cfg, 1)
    verbs = [
        ("dial", '{"device_id":"%s"}' % dev),
        ("open_relay", '{"device_id":"%s"}' % dev),
        ("answer", '{"device_id":"%s"}' % dev),
        ("answer", '{"device_id":"%s","host":"127.0.0.1","port":9}' % dev),
        ("hangup", '{"call_id":"nothing","reason":"x"}'),
        ("external_ring", '{"device_id":"%s","call_id":"c","hold":5}' % dev),
        ("talk_health", '{"device_id":"%s"}' % dev),
        ("stream_url", '{"device_id":"%s"}' % dev),
        ("answer", '{"device_id":"nope"}'),
    ]
    reqs = ['{"id":%d,"method":"%s","params":%s}' % (10 + i, m, p) for i, (m, p) in enumerate(verbs)]
    step(pr, "device verbs before any station (order open in Go)", "\n".join(reqs) + "\n", len(reqs), ordered=False)

    st = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    st.bind(("127.0.0.1", 0))
    st.settimeout(2.0)
    me = "127.0.0.1:%d" % st.getsockname()[1]

    def sip_send(text):
        st.sendto(text.encode(), ("127.0.0.1", sip))

    def sip_recv():
        try:
            data, _ = st.recvfrom(65535)
        except OSError:
            return ""
        return data.decode()

    def register(auth=""):
        a = "Authorization: %s\r\n" % auth if auth else ""
        sip_send("REGISTER sip:127.0.0.1:%d SIP/2.0\r\nVia: SIP/2.0/UDP %s;branch=z9hG4bKreg\r\n"
                 "From: <sip:8001@station>;tag=reg\r\nTo: <sip:8001@station>\r\nCall-ID: reg-1\r\n"
                 "CSeq: 1 REGISTER\r\nContact: <sip:8001@%s>\r\nExpires: 300\r\n%sContent-Length: 0\r\n\r\n"
                 % (sip, me, me, a))

    register()
    ch = sip_recv()
    nonce = re.search(r'nonce="([^"]*)"', ch).group(1)
    say("## station: REGISTER -> %s" % ch.split("\r\n")[0])
    uri = "sip:127.0.0.1"
    resp = md5(md5("8001:VDP:s3cretpass") + ":" + nonce + ":" + md5("REGISTER:" + uri))
    register('Digest username="8001", realm="VDP", nonce="%s", uri="%s", response="%s", algorithm=MD5' % (nonce, uri, resp))
    say("## station: authorised REGISTER -> %s" % sip_recv().split("\r\n")[0])
    # the registration makes CanDial true: one capabilities_changed, struct field order
    show("capabilities_changed after the REGISTER", pr.collect(1, 3.0), 1)

    sdp = "v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\ns=x\r\nc=IN IP4 127.0.0.1\r\nt=0 0\r\nm=audio %d RTP/AVP 8\r\na=rtpmap:8 PCMA/8000\r\n" % free_udp_port()
    sip_send("INVITE sip:9901@127.0.0.1:%d SIP/2.0\r\nVia: SIP/2.0/UDP %s;branch=z9hG4bKinv\r\n"
             "From: <sip:8001@station>;tag=t-call-S1\r\nTo: <sip:9901@server>\r\nCall-ID: call-S1\r\n"
             "CSeq: 1 INVITE\r\nContact: <sip:8001@%s>\r\nContent-Type: application/sdp\r\nContent-Length: %d\r\n\r\n%s"
             % (sip, me, me, len(sdp), sdp))
    say("## station: INVITE -> %s, %s" % (sip_recv().split("\r\n")[0], sip_recv().split("\r\n")[0]))
    show("ringing notification", pr.collect(1, 3.0), 1)
    pr.send('{"id":30,"method":"answer","params":{"device_id":"%s"}}\n' % dev)
    show("answer: the result and call_state_changed answered (the reply floats: PARITY 361)", pr.collect(2, 3.0), 2, reply_after=0)
    say("## station after the answer -> %s" % sip_recv().split("\r\n")[0])
    step(pr, "hangup of the answered call (the driver BYEs it) (the reply floats: PARITY 361)",
         '{"id":31,"method":"hangup","params":{"call_id":"call-S1","reason":"smoke"}}\n', 2, reply_after=0)
    say("## station after the hangup -> %s" % re.sub(r"127\.0\.0\.1:\d+", "127.0.0.1:<port>", sip_recv().split("\r\n")[0]))
    step(pr, "hangup again: the call is over", '{"id":32,"method":"hangup","params":{"call_id":"call-S1"}}\n', 1)

    # ---- the outbound call (slice 3b-2): `dial` reaches the driver, which INVITEs the address
    # the station REGISTERed from. The hub's out- id is random; everything printed is masked.
    def mask(text):
        text = re.sub(r"out-[0-9a-f]{16}", "out-<ID>", text)
        text = re.sub(r"z9hG4bK[A-Za-z0-9]+", "z9hG4bK<B>", text)
        text = re.sub(r"tag=[A-Za-z0-9]{8}\b", "tag=<TAG>", text)
        text = re.sub(r"127\.0\.0\.1:\d+", "127.0.0.1:<port>", text)
        return re.sub(r"m=audio \d+", "m=audio <RTP>", text)

    def hdr(msg, name):
        m = re.search(r"(?im)^%s:[ \t]*(.*?)\r?$" % re.escape(name), msg)
        return m.group(1) if m else ""

    def station_reply(req, code, reason, tag="", extra=(), body=""):
        to = hdr(req, "To") + (";tag=" + tag if tag and ";tag=" not in hdr(req, "To") else "")
        lines = ["SIP/2.0 %d %s" % (code, reason), "Via: " + hdr(req, "Via"), "From: " + hdr(req, "From"),
                 "To: " + to, "Call-ID: " + hdr(req, "Call-ID"), "CSeq: " + hdr(req, "CSeq"),
                 "Contact: <sip:9901@%s>" % me, *extra]
        if body:
            lines.append("Content-Type: application/sdp")
        lines.append("Content-Length: %d" % len(body))
        sip_send("\r\n".join(lines) + "\r\n\r\n" + body)

    def seen(title, msg):
        say("## station gets %s: %s | CSeq %s | To %s | has Authorization: %s" % (
            title, mask(msg.split("\r\n")[0]), hdr(msg, "CSeq"), mask(hdr(msg, "To")),
            "Authorization" in msg))

    def ipc_masked(n, title, timeout=4.0, reply_after=None):
        lines = [mask(l) for l in pr.collect(n, timeout)]
        show(title, lines, n, reply_after=reply_after)

    answer_sdp = "v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\ns=x\r\nc=IN IP4 127.0.0.1\r\nt=0 0\r\nm=audio %d RTP/AVP 96 8\r\na=rtpmap:96 PCM/16000\r\na=rtpmap:8 PCMA/8000\r\n" % free_udp_port()

    # A: an INVITE challenge answered with the registration credentials, 200, hung up through IPC
    pr.send('{"id":40,"method":"dial","params":{"device_id":"%s"}}\n' % dev)
    inv1 = sip_recv()
    seen("the INVITE", inv1)
    say("## its SDP offer: %s" % mask(inv1.split("\r\n\r\n", 1)[1].replace("\r\n", " / ")))
    station_reply(inv1, 401, "Unauthorized", "st1",
                  ['WWW-Authenticate: Digest realm="VDP", nonce="smokenonce0123456789", algorithm=MD5'])
    seen("the ACK of the 401", sip_recv())
    inv2 = sip_recv()
    seen("the INVITE again", inv2)
    say("## its Authorization: %s" % mask(hdr(inv2, "Authorization")))
    station_reply(inv2, 100, "Trying")
    station_reply(inv2, 200, "OK", "st1", body=answer_sdp)
    seen("the ACK of the 200", sip_recv())
    out_lines = pr.collect(2, 4.0)
    call_a = re.search(r"out-[0-9a-f]{16}", "".join(out_lines)).group(0)
    show("dial: the result carries the out- id, and call_state_changed answered (the reply floats: PARITY 361)", [mask(l) for l in out_lines], 2, reply_after=0)
    pr.send('{"id":41,"method":"hangup","params":{"call_id":"%s","reason":"smoke"}}\n' % call_a)
    seen("the BYE", sip_recv())
    ipc_masked(2, "hangup of the dialled call by its id (the driver BYEs as the caller), and call_state_changed ended (the reply floats: PARITY 361)", reply_after=0)

    # B: the station refuses
    pr.send('{"id":42,"method":"dial","params":{"device_id":"%s"}}\n' % dev)
    inv = sip_recv()
    seen("the INVITE", inv)
    station_reply(inv, 486, "Busy Here", "st1")
    seen("the ACK of the 486", sip_recv())
    ipc_masked(1, "dial refused by the station: dial_failed, no events")
    say("## nothing else arrives: %s" % pr.quiet(0.4))

    # C: answered, then the station hangs up
    pr.send('{"id":43,"method":"dial","params":{"device_id":"%s"}}\n' % dev)
    inv = sip_recv()
    station_reply(inv, 200, "OK", "st1", body=answer_sdp)
    seen("the ACK of the 200", sip_recv())
    out_lines = pr.collect(2, 4.0)
    call_c = re.search(r"out-[0-9a-f]{16}", "".join(out_lines)).group(0)
    show("dial: result and answered (the reply floats: PARITY 361)", [mask(l) for l in out_lines], 2, reply_after=0)
    sip_send("BYE sip:8001@127.0.0.1:%d SIP/2.0\r\nVia: SIP/2.0/UDP %s;branch=z9hG4bKb\r\n"
             "From: <sip:9901@127.0.0.1>;tag=st1\r\nTo: %s\r\nCall-ID: %s\r\nCSeq: 3 BYE\r\nContent-Length: 0\r\n\r\n"
             % (sip, me, hdr(inv, "From"), call_c))
    say("## station's BYE -> %s" % sip_recv().split("\r\n")[0])
    ipc_masked(1, "call_state_changed ended (the station hung up)")
    pr.send('{"id":44,"method":"hangup","params":{"call_id":"%s"}}\n' % call_c)
    ipc_masked(1, "hangup of the finished dialled call")
    step(pr, "deconfigure", '{"id":33,"method":"deconfigure","params":{"device_id":"%s"}}\n' % dev, 1)
    step(pr, "deconfigure again", '{"id":34,"method":"deconfigure","params":{"device_id":"%s"}}\n' % dev, 1)
    # the ports are free again: configure the same device once more, then stop with SIGTERM
    step(pr, "configure again after the deconfigure", '{"id":35,"method":"configure","params":%s}\n' % cfg, 1)
    pr.p.send_signal(signal.SIGTERM)
    say("  exit code: %s" % pr.wait(5))
    say("  socket file gone: %s" % (not os.path.exists(pr.sock)))
    st.close()


class DahuaStation:
    """A fake Dahua VTO on a loopback alias (127.0.0.9:5060: the Go driver has no port setting, so
    the device port is always 5060). It answers REGISTER (401, then 200; 403/486/silent on
    request), answers an INVITE of the transport (200 with an SDP, 486, or nothing) and a BYE, and
    queues everything else. Nothing leaves 127.0.0.0/8."""

    def __init__(self, ip):
        import queue
        self.ip = ip
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((ip, 5060))
        self.sock.settimeout(0.2)
        self.rtp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.rtp.bind((ip, 0))
        self.q = queue.Queue()
        self.drv = None
        self.reg_mode, self.inv_mode = "ok", "none"
        self.regs = self.deregs = 0
        self.live = True
        self.t = threading.Thread(target=self.run, daemon=True)
        self.t.start()

    @staticmethod
    def hdr(msg, name):
        m = re.search(r"(?im)^%s:[ \t]*(.*?)\r?$" % re.escape(name), msg)
        return m.group(1) if m else ""

    def echo(self, req, code, reason, tag="", body="", extra=()):
        head = req.split("\r\n\r\n", 1)[0]
        lines = ["SIP/2.0 %d %s" % (code, reason)]
        lines += [l for l in head.split("\r\n") if l.lower().startswith("via:")]
        to = self.hdr(req, "To")
        if tag and "tag=" not in to:
            to += ";tag=" + tag
        lines += ["From: " + self.hdr(req, "From"), "To: " + to, "Call-ID: " + self.hdr(req, "Call-ID"),
                  "CSeq: " + self.hdr(req, "CSeq"), *extra]
        if body:
            lines.append("Content-Type: application/sdp")
        lines.append("Content-Length: %d" % len(body))
        return "\r\n".join(lines) + "\r\n\r\n" + body

    def run(self):
        while self.live:
            try:
                data, frm = self.sock.recvfrom(65535)
            except OSError:
                continue
            req = data.decode("utf-8", "replace")
            self.drv = self.drv or frm

            def reply(x):
                self.sock.sendto(x.encode(), frm)

            if req.startswith("REGISTER "):
                if self.reg_mode == "403":
                    reply(self.echo(req, 403, "Forbidden"))
                elif self.reg_mode == "486":
                    reply(self.echo(req, 486, "Busy Here"))
                elif self.reg_mode == "silent":
                    pass
                elif "Authorization:" not in req:
                    reply(self.echo(req, 401, "Unauthorized", extra=['WWW-Authenticate: Digest realm="r", nonce="n1", algorithm=MD5']))
                elif "Expires: 0" in req:
                    self.deregs += 1
                    reply(self.echo(req, 200, "OK", extra=["Expires: 0"]))
                else:
                    self.regs += 1
                    reply(self.echo(req, 200, "OK", extra=["Expires: 3600"]))
                continue
            if req.startswith("INVITE "):
                if self.inv_mode == "200":
                    sdp = "v=0\r\no=- 0 0 IN IP4 %s\r\ns=x\r\nc=IN IP4 %s\r\nt=0 0\r\nm=audio %d RTP/AVP 96\r\na=rtpmap:96 L16/16000\r\n" % (self.ip, self.ip, self.rtp.getsockname()[1])
                    reply(self.echo(req, 200, "OK", "devicetag", sdp))
                elif self.inv_mode == "486":
                    reply(self.echo(req, 486, "Busy Here", "devicetag"))
            elif req.startswith("BYE "):
                reply(self.echo(req, 200, "OK"))
            self.q.put(req)

    def send(self, text):
        self.sock.sendto(text.encode(), self.drv)

    def get(self, timeout=2.0):
        import queue
        try:
            return self.q.get(timeout=timeout)
        except queue.Empty:
            return ""

    def wait_regs(self, timeout=4.0):
        end = time.time() + timeout
        while self.regs == 0 and time.time() < end:
            time.sleep(0.02)
        return self.regs > 0

    def close(self):
        self.live = False
        # Join the reader first: a close() under a blocked recvfrom keeps the port bound until the
        # 0.2 s timeout, and the next flow (wsmedia_flow) then found 127.0.0.9:5060 "in use" and
        # silently skipped itself in every full run.
        self.t.join(2.0)
        self.sock.close()
        self.rtp.close()


def dahua_flow(binary, tmp):
    """dahua_sip: configure, the registration, a ring (180, retransmit, answer through the IPC,
    the station's BYE), a ring taken by a webrtc_offer (never answered before the browser
    connects, then a CANCEL: 481 and no `ended`, a Go oddity both binaries share), open_relay,
    a dial, a rejected account (driver_error), deconfigure (the clean Expires: 0) and SIGTERM.
    Every line is the same on the Go and the Rust binary."""
    say("# dahua_sip: configure, the registration, rings, the relay path, dial, deconfigure")
    try:
        a, b = DahuaStation("127.0.0.9"), DahuaStation("127.0.0.10")
    except OSError as e:
        say("  skipped: cannot bind 127.0.0.9:5060 / 127.0.0.10:5060 (%s)" % e)
        return
    here = os.path.dirname(os.path.abspath(__file__))
    chrome = open(os.path.join(here, "..", "tests", "interop", "fixtures", "chrome124_offer.sdp")).read().replace("\r\n", "\n").replace("\n", "\r\n")
    pr = Proc(binary, tmp, extra=("-host", "127.0.0.1"), sock_name="d.sock")
    pr.connect()
    mask_tag = lambda t: re.sub(r"tag=[0-9a-f]{16}\b", "tag=<TAG>", t)
    mask = lambda t: re.sub(r"[0-9a-f]{16}@127\.0\.0\.\d+", "<CALLID>", re.sub(r"z9hG4bK[A-Za-z0-9]{16}", "z9hG4bK<B>", mask_tag(re.sub(r"127\.0\.0\.\d+:\d+", "127.0.0.x:<port>", t))))
    mask_media = lambda t: re.sub(r"m=audio \d+", "m=audio <RTP>", t)

    def seen(title, msg):
        say("## station gets %s: %s | CSeq %s | To %s" % (title, mask(msg.split("\r\n")[0]), a.hdr(msg, "CSeq"), mask(a.hdr(msg, "To"))))

    def invite(call_id, branch, sdp=True):
        body = "v=0\r\no=- 1 1 IN IP4 127.0.0.9\r\ns=-\r\nc=IN IP4 127.0.0.9\r\nt=0 0\r\nm=audio %d RTP/AVP 0\r\n" % a.rtp.getsockname()[1] if sdp else ""
        return ("INVITE sip:8001@127.0.0.9 SIP/2.0\r\nVia: SIP/2.0/UDP 127.0.0.9;branch=z9hG4bK%s\r\n"
                "From: <sip:vto@127.0.0.9>;tag=a\r\nTo: <sip:8001@127.0.0.9>\r\nCall-ID: %s\r\nCSeq: 1 INVITE\r\n%sContent-Length: %d\r\n\r\n%s"
                % (branch, call_id, "Content-Type: application/sdp\r\n" if body else "", len(body), body))

    cfg = '{"driver":"dahua_sip","config":{"host":"127.0.0.9","sip_user":"8001#1","sip_password":"pw"}}'
    step(pr, "configure with a missing field", '{"id":1,"method":"configure","params":{"driver":"dahua_sip","config":{"host":"127.0.0.9"}}}\n', 1)
    pr.send('{"id":2,"method":"configure","params":%s}\n' % cfg)
    show("configure: the Capabilities struct, then driver_availability", pr.collect(2, 6.0), 2)
    step(pr, "configure the same host again", '{"id":3,"method":"configure","params":%s}\n' % cfg, 1)
    say("## registered: %s" % a.wait_regs())

    # a ring: 180, the retransmit, the answer through the IPC, the station's BYE
    a.send(invite("ring-s1", "inv1"))
    seen("the 180", a.get())
    show("ringing", pr.collect(1, 3.0), 1)
    a.send(invite("ring-s1", "inv1"))
    seen("the 180 again (retransmit)", a.get())
    say("## no second ringing: %s" % pr.quiet(0.3))
    step(pr, "answer the ring through the IPC", '{"id":4,"method":"answer","params":{"device_id":"127.0.0.9","host":"127.0.0.1","port":5000}}\n', 1)
    m = a.get()
    say("## station gets the 200: %s | tag same as the 180: %s" % (mask(mask_media(m.split("\r\n")[0])), "tag=" in a.hdr(m, "To")))
    say("## its SDP: %s" % mask_media(m.split("\r\n\r\n", 1)[1].replace("\r\n", " / ")))
    step(pr, "answer again: no ring", '{"id":5,"method":"answer","params":{"device_id":"127.0.0.9","host":"127.0.0.1","port":5000}}\n', 1)
    a.send("BYE sip:8001@127.0.0.9 SIP/2.0\r\nVia: SIP/2.0/UDP 127.0.0.9;branch=z9hG4bKbye1\r\nFrom: <sip:vto@127.0.0.9>;tag=a\r\nTo: <sip:8001@127.0.0.9>;tag=b\r\nCall-ID: ring-s1\r\nCSeq: 2 BYE\r\nContent-Length: 0\r\n\r\n")
    seen("the BYE answered", a.get())
    show("call_state_changed ended (the station hung up), stamped with the device", pr.collect(1, 3.0), 1)

    # a ring taken by a browser offer: never answered before the browser connects
    a.send(invite("ring-s2", "inv2"))
    seen("the 180", a.get())
    show("ringing", pr.collect(1, 3.0), 1)
    pr.send(json.dumps({"id": 6, "method": "webrtc_offer", "params": {"call_id": "ring-s2", "sdp": chrome}}) + "\n")
    lines = pr.collect(1, 8.0)
    say("## webrtc_offer: result with an sdp: %s" % all("sdp" in (json.loads(l).get("result") or {}) for l in lines))
    say("## the station is not answered before the browser connects: %r" % a.get(1.0))
    step(pr, "answer: the offer took the ring", '{"id":7,"method":"answer","params":{"device_id":"127.0.0.9","host":"127.0.0.1","port":5000}}\n', 1)
    a.send("CANCEL sip:8001@127.0.0.9 SIP/2.0\r\nVia: SIP/2.0/UDP 127.0.0.9;branch=z9hG4bKinv2\r\nFrom: <sip:vto@127.0.0.9>;tag=a\r\nTo: <sip:8001@127.0.0.9>\r\nCall-ID: ring-s2\r\nCSeq: 1 CANCEL\r\nContent-Length: 0\r\n\r\n")
    seen("the CANCEL answered (481: the ring was taken)", a.get())
    say("## and no `ended` reaches Python: %s" % pr.quiet(0.4))

    step(pr, "open_relay", '{"id":8,"method":"open_relay","params":{"device_id":"127.0.0.9"}}\n', 1)
    step(pr, "hangup: no dahua driver ends calls", '{"id":9,"method":"hangup","params":{"call_id":"x"}}\n', 1)

    # dial: the transport INVITEs the device; it answers 200; its BYE ends the call
    a.inv_mode = "200"
    pr.send('{"id":10,"method":"dial","params":{"device_id":"127.0.0.9","host":"127.0.0.1","port":6000}}\n')
    inv = a.get(4.0)
    seen("the INVITE", inv)
    say("## its SDP offer: %s" % mask(mask_media(inv.split("\r\n\r\n", 1)[1].replace("\r\n", " / "))))
    seen("the ACK", a.get())
    step_lines = [mask(l) for l in pr.collect(1, 4.0)]
    show("dial result", step_lines, 1)
    a.send("BYE sip:8001@127.0.0.9 SIP/2.0\r\nVia: SIP/2.0/UDP 127.0.0.9;branch=z9hG4bKb\r\nFrom: <sip:9901@127.0.0.9>;tag=devicetag\r\nTo: %s\r\nCall-ID: %s\r\nCSeq: 3 BYE\r\nContent-Length: 0\r\n\r\n"
           % (a.hdr(inv, "From"), a.hdr(inv, "Call-ID")))
    seen("the BYE answered", a.get())
    show("call_state_changed ended for the dialled call", [mask(l) for l in pr.collect(1, 3.0)], 1)
    a.inv_mode = "486"
    pr.send('{"id":11,"method":"dial","params":{"device_id":"127.0.0.9","host":"127.0.0.1","port":6000}}\n')
    seen("the INVITE", a.get(4.0))
    seen("the ACK of the refusal", a.get())
    show("dial refused: dial_failed", pr.collect(1, 4.0), 1)

    # a second device whose account is refused
    b.reg_mode = "403"
    pr.send('{"id":12,"method":"configure","params":{"driver":"dahua_sip","config":{"host":"127.0.0.10","sip_user":"8001#1","sip_password":"pw"}}}\n')
    show("configure of a device whose credentials are refused: the reply, driver_availability false, driver_error", pr.collect(3, 6.0), 3)

    step(pr, "deconfigure the first device", '{"id":13,"method":"deconfigure","params":{"device_id":"127.0.0.9"}}\n', 1)
    say("## the device saw %d clean de-register(s) by the time deconfigure returned" % a.deregs)
    step(pr, "deconfigure again", '{"id":14,"method":"deconfigure","params":{"device_id":"127.0.0.9"}}\n', 1)
    pr.p.send_signal(signal.SIGTERM)
    say("  exit code: %s" % pr.wait(8))
    say("  socket file gone: %s" % (not os.path.exists(pr.sock)))
    a.close()
    b.close()


def wsmedia_flow(binary, tmp):
    """wsmedia (slice 8): an away phone's `media_open` joined to a dahua_sip ring. The VTO is not
    answered before the leg exists and is answered at once when it does (a WebSocket leg is
    connected the moment it is opened); the VTO's 16 kHz L16 reaches Python as 320-byte mu-law
    `media_frame` notifications, Python's mu-law frames reach the VTO as 1280-byte L16 RTP; a
    `media_close` notifies `media_closed` BEFORE its response and hangs the VTO up; a second ring
    whose VTO hangs up first ends the call (`ended`, our own BYE) and leaves the leg Python's to
    close. Every line is the same on the Go and the Rust binary (the RTP header fields that are
    random are not printed; payloads are printed as md5)."""
    say("# wsmedia: media_open joins an away phone to a dahua_sip ring")
    try:
        a = DahuaStation("127.0.0.9")
    except OSError as e:
        say("  skipped: cannot bind 127.0.0.9:5060 (%s)" % e)
        return
    import struct
    pr = Proc(binary, tmp, extra=("-host", "127.0.0.1"), sock_name="w.sock")
    pr.connect()
    mask_tag = lambda t: re.sub(r"tag=[0-9a-f]{16}\b", "tag=<TAG>", t)
    mask = lambda t: re.sub(r"[0-9a-f]{16}@127\.0\.0\.\d+", "<CALLID>", re.sub(r"z9hG4bK[A-Za-z0-9]{16}", "z9hG4bK<B>", mask_tag(re.sub(r"127\.0\.0\.\d+:\d+", "127.0.0.x:<port>", t))))
    mask_media = lambda t: re.sub(r"m=audio \d+", "m=audio <RTP>", t)

    def seen(title, msg):
        say("## station gets %s: %s | CSeq %s | To %s" % (title, mask(msg.split("\r\n")[0]), a.hdr(msg, "CSeq"), mask(a.hdr(msg, "To"))))

    def invite(call_id, branch):
        body = "v=0\r\no=- 1 1 IN IP4 127.0.0.9\r\ns=-\r\nc=IN IP4 127.0.0.9\r\nt=0 0\r\nm=audio %d RTP/AVP 0\r\n" % a.rtp.getsockname()[1]
        return ("INVITE sip:8001@127.0.0.9 SIP/2.0\r\nVia: SIP/2.0/UDP 127.0.0.9;branch=z9hG4bK%s\r\n"
                "From: <sip:vto@127.0.0.9>;tag=a\r\nTo: <sip:8001@127.0.0.9>\r\nCall-ID: %s\r\nCSeq: 1 INVITE\r\nContent-Type: application/sdp\r\nContent-Length: %d\r\n\r\n%s"
                % (branch, call_id, len(body), body))

    def relay_port(ok200):
        m = re.search(r"m=audio (\d+)", ok200)
        return int(m.group(1))

    def rtp_payloads(window):
        import hashlib
        a.rtp.settimeout(0.1)
        end = time.time() + window
        got = []
        while time.time() < end:
            try:
                data, _ = a.rtp.recvfrom(4096)
            except OSError:
                continue
            if len(data) > 12:
                got.append((data[1] & 0x7f, len(data) - 12, hashlib.md5(data[12:]).hexdigest()))
        return got

    def silent(line):
        try:
            m = json.loads(line)
        except ValueError:
            return False
        if m.get("method") != "media_frame":
            return False
        b = base64.b64decode(m["params"]["d"])
        return len(set(b)) == 1 and b[0] in (0x00, 0xFF)

    def take(pr, n, timeout=3.0):
        """Like Proc.collect, but a mu-law/PCM silence frame is not a line: the relay's jitter
        buffer fills a gap with silence, a number of them that depends on timing (Go and Rust
        both do it)."""
        got = []
        end = time.time() + timeout
        while len(got) < n and time.time() < end:
            for l in pr.collect(1, max(0.0, end - time.time())):
                if not silent(l):
                    got.append(l)
        return got

    def fstep(pr, title, payload, expected, ordered=True, timeout=3.0):
        pr.send(payload)
        show(title, take(pr, expected, timeout), expected, ordered)

    cfg = '{"driver":"dahua_sip","config":{"host":"127.0.0.9","sip_user":"8001#1","sip_password":"pw"}}'
    pr.send('{"id":1,"method":"configure","params":%s}\n' % cfg)
    show("configure: the Capabilities struct, then driver_availability", take(pr, 2, 6.0), 2)
    say("## registered: %s" % a.wait_regs())

    # a ring taken by an away phone
    a.send(invite("ring-w1", "inv1"))
    seen("the 180", a.get())
    show("ringing", take(pr, 1, 3.0), 1)
    say("## the VTO is not answered before the leg exists: %r" % a.get(0.6))
    fstep(pr, "media_open pcmu", '{"id":2,"method":"media_open","params":{"call_id":"ring-w1","codec":"pcmu"}}\n', 1)
    m = a.get(4.0)
    say("## station gets the 200 at once: %s" % mask(mask_media(m.split("\r\n")[0])))
    say("## its SDP: %s" % mask(mask_media(m.split("\r\n\r\n", 1)[1].replace("\r\n", " / "))))
    fstep(pr, "media_open again", '{"id":3,"method":"media_open","params":{"call_id":"ring-w1","codec":"l16"}}\n', 1)
    port = relay_port(m)
    # VTO -> phone: one 16 kHz L16 frame, a known tone
    tone = b"".join(struct.pack("<h", ((i % 64) - 32) * 200) for i in range(640))
    for i in range(3):
        pkt = struct.pack("!BBHII", 0x80, 96, 1 + i, 640 * i, 0x1234) + tone
        a.rtp.sendto(pkt, ("127.0.0.1", port))
        time.sleep(0.04)
    show("the VTO's audio reaches Python as media_frame (320 bytes of mu-law)", take(pr, 3, 3.0), 3)
    # phone -> VTO: a known 320-byte mu-law frame, four times
    f320 = base64.b64encode(bytes((7 + i) & 255 for i in range(320))).decode()
    lines = ['{"id":%d,"method":"media_frame","params":{"call_id":"ring-w1","d":"%s"}}' % (10 + i, f320) for i in range(4)]
    fstep(pr, "media_frame x4", "\n".join(lines) + "\n", 4)
    say("## RTP at the station (payload type, bytes, md5): %s" % sorted(set(rtp_payloads(1.0))))
    fstep(pr, "media_close (notification then response)",
         '{"id":20,"method":"media_close","params":{"call_id":"ring-w1","reason":"user"}}\n', 2, ordered=True)
    seen("the BYE of the closed leg", a.get(3.0))
    fstep(pr, "media_frame after close", '{"id":21,"method":"media_frame","params":{"call_id":"ring-w1","d":"%s"}}\n' % f320, 1)

    # a second ring whose VTO hangs up first
    a.send(invite("ring-w2", "inv2"))
    seen("the 180", a.get())
    show("ringing", take(pr, 1, 3.0), 1)
    fstep(pr, "media_open l16", '{"id":30,"method":"media_open","params":{"call_id":"ring-w2","codec":"l16"}}\n', 1)
    m = a.get(4.0)
    say("## station gets the 200: %s" % mask(mask_media(m.split("\r\n")[0])))
    a.send("BYE sip:8001@127.0.0.9 SIP/2.0\r\nVia: SIP/2.0/UDP 127.0.0.9;branch=z9hG4bKbyew\r\nFrom: <sip:vto@127.0.0.9>;tag=a\r\nTo: <sip:8001@127.0.0.9>;tag=b\r\nCall-ID: ring-w2\r\nCSeq: 2 BYE\r\nContent-Length: 0\r\n\r\n")
    seen("the BYE answered", a.get())
    show("call_state_changed ended (the VTO hung up)", [mask(l) for l in take(pr, 1, 3.0)], 1)
    f1280 = base64.b64encode(bytes(i & 255 for i in range(1280))).decode()
    fstep(pr, "a frame after the VTO hung up is still accepted", '{"id":31,"method":"media_frame","params":{"call_id":"ring-w2","d":"%s"}}\n' % f1280, 1)
    fstep(pr, "media_close", '{"id":32,"method":"media_close","params":{"call_id":"ring-w2"}}\n', 2, ordered=True)
    pr.p.send_signal(signal.SIGTERM)
    say("  exit code: %s" % pr.wait(8))
    a.close()


class UnifiConsole:
    """A fake Protect console on 127.0.0.1 over TLS with a throw-away self-signed certificate
    (the config says verify_ssl: false): the events websocket, the rtsps-stream call, and a
    switch that refuses the next upgrades with a status. Nothing leaves 127.0.0.1."""

    KEY = "SMOKE-API-KEY"

    def __init__(self, tmp):
        cert, key = os.path.join(tmp, "c.pem"), os.path.join(tmp, "k.pem")
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", key, "-out", cert,
                        "-days", "2", "-subj", "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1"],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.ctx.load_cert_chain(cert, key)
        self.srv = socket.socket()
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(8)
        self.port = self.srv.getsockname()[1]
        self.lock = threading.Lock()
        self.conns = []
        self.status = 0
        self.attempts = 0
        self.upgrades = 0
        self.closed = False
        threading.Thread(target=self.accept_loop, daemon=True).start()

    def host(self):
        return "127.0.0.1:%d" % self.port

    def accept_loop(self):
        while not self.closed:
            try:
                raw, _ = self.srv.accept()
            except OSError:
                return
            threading.Thread(target=self.serve, args=(raw,), daemon=True).start()

    def serve(self, raw):
        try:
            c = self.ctx.wrap_socket(raw, server_side=True)
        except (ssl.SSLError, OSError):
            raw.close()
            return
        try:
            buf = b""
            while b"\r\n\r\n" not in buf:
                d = c.recv(4096)
                if not d:
                    c.close()
                    return
                buf += d
            head, rest = buf.split(b"\r\n\r\n", 1)
            lines = head.decode("latin-1").split("\r\n")
            method, path = lines[0].split(" ")[:2]
            hdr = {k.strip().lower(): v.strip() for k, v in (l.split(":", 1) for l in lines[1:] if ":" in l)}
            n = int(hdr.get("content-length", "0"))
            while len(rest) < n:
                rest += c.recv(4096)
            with self.lock:
                self.attempts += 1
                status = self.status
            if status:
                body = b"TOKEN-IN-ERROR-BODY"
                c.sendall(b"HTTP/1.1 %d X\r\nContent-Length: %d\r\nConnection: close\r\n\r\n%s" % (status, len(body), body))
                c.close()
                return
            if hdr.get("x-api-key") != self.KEY:
                c.sendall(b"HTTP/1.1 401 Unauthorized\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                c.close()
                return
            if path.endswith("/talkback-session"):
                body = json.dumps({"url": "rtp://192.168.2.9:7004", "codec": "opus", "samplingRate": 24000}).encode()
                with self.lock:
                    self.talkback_posts = getattr(self, "talkback_posts", 0) + 1
                c.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\nConnection: close\r\n\r\n%s" % (len(body), body))
                c.close()
                return
            if path.endswith("/rtsps-stream"):
                body = json.dumps({"high": "rtsps://127.0.0.1:7441/SMOKETOKEN?enableSrtp"}).encode()
                c.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\nConnection: close\r\n\r\n%s" % (len(body), body))
                c.close()
                return
            if path == "/proxy/protect/integration/v1/subscribe/events" and hdr.get("upgrade", "").lower() == "websocket":
                acc = base64.b64encode(hashlib.sha1((hdr["sec-websocket-key"] + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
                c.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: %s\r\n\r\n" % acc).encode())
                with self.lock:
                    self.conns.append(c)
                    self.upgrades += 1
                self.read_client(c, rest)
                return
            c.sendall(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            c.close()
        except (OSError, ssl.SSLError, ValueError, KeyError):
            try:
                c.close()
            except OSError:
                pass

    @staticmethod
    def frame(op, payload):
        b = bytes([0x80 | op])
        if len(payload) < 126:
            b += bytes([len(payload)])
        else:
            b += bytes([126]) + len(payload).to_bytes(2, "big")
        return b + payload

    def read_client(self, c, buf):
        try:
            while True:
                while len(buf) >= 2:
                    n, h = buf[1] & 0x7F, 2
                    if n == 126:
                        n, h = int.from_bytes(buf[2:4], "big"), 4
                    if len(buf) < h + 4 + n:
                        break
                    mask = buf[h:h + 4]
                    p = bytes(x ^ mask[i % 4] for i, x in enumerate(buf[h + 4:h + 4 + n]))
                    op = buf[0] & 0x0F
                    buf = buf[h + 4 + n:]
                    if op == 9:
                        c.sendall(self.frame(10, p))
                    elif op == 8:
                        c.sendall(self.frame(8, b""))
                        c.close()
                        return
                d = c.recv(4096)
                if not d:
                    return
                buf += d
        except (OSError, ssl.SSLError):
            return

    def send(self, text):
        with self.lock:
            c = self.conns[-1]
        c.sendall(self.frame(1, text.encode()))

    def drop(self):
        with self.lock:
            cs, self.conns = self.conns, []
        for c in cs:
            try:
                c.shutdown(socket.SHUT_RDWR)  # wakes the thread that is reading it
            except OSError:
                pass
            try:
                c.close()
            except OSError:
                pass

    def close(self):
        self.closed = True
        self.drop()
        self.srv.close()


def unifi_flow(binary, tmp):
    """unifi_protect: configure (and its refusals), the events websocket over TLS (availability,
    a ring, a second ring ignored, a hang-up, a drop that is refused and recovers), stream_url
    and its refusal, dial/answer, deconfigure and SIGTERM. Every line is the same on the Go and
    the Rust binary (the ring `end` hold, 10 s, and open_relay, which needs ffmpeg in Go, are not
    exercised here; tests/unifi_ipc.rs covers the first, PARITY.md the second)."""
    say("# unifi_protect: configure, the events websocket, rings, stream_url, hangup, deconfigure")
    if not shutil.which("openssl"):
        say("  skipped: no openssl to make a certificate")
        return
    con = UnifiConsole(tmp)
    pr = Proc(binary, tmp, sock_name="u.sock")
    pr.connect()
    host = con.host()
    mask = lambda t: t.replace(host, "<HOST>")

    def call(title, method, params, expected=1, timeout=4.0):
        step.n += 1
        pr.send(json.dumps({"id": step.n, "method": method, "params": params}) + "\n")
        show(title, [mask(l) for l in pr.collect(expected, timeout)], expected)

    step.n = 100
    step.n += 1
    pr.send(json.dumps({"id": step.n, "method": "configure", "params": {"driver": "unifi_protect", "config": {"nvr_host": host}}}) + "\n")
    show("configure with missing fields", [mask(l) for l in pr.collect(1, 3.0)], 1)
    for title, extra in (("a host with a slash", {"nvr_host": "h/x"}), ("ring_events that is not a bool", {"ring_events": "yes"})):
        step.n += 1
        c = {"nvr_host": host, "nvr_api_key": UnifiConsole.KEY, "camera_id": "cam1"}
        c.update(extra)
        pr.send(json.dumps({"id": step.n, "method": "configure", "params": {"driver": "unifi_protect", "config": c}}) + "\n")
        show("configure with " + title, [mask(l) for l in pr.collect(1, 3.0)], 1)

    step.n += 1
    pr.send(json.dumps({"id": step.n, "method": "configure", "params": {"driver": "unifi_protect", "config": {"nvr_host": host, "nvr_api_key": UnifiConsole.KEY, "camera_id": "cam1", "verify_ssl": False, "ring_events": True}}}) + "\n")
    show("configure with ring_events: the Capabilities struct, then driver_availability", [mask(l) for l in pr.collect(2, 6.0)], 2)
    say("## the console saw %d upgrade(s)" % con.upgrades)
    call("configure the same host again", "configure", {"driver": "unifi_protect", "config": {"nvr_host": host, "nvr_api_key": UnifiConsole.KEY, "camera_id": "cam1"}})

    ring = lambda i, typ="add": json.dumps({"type": typ, "item": {"id": i, "modelKey": "event", "type": "ring", "start": 1, "device": "cam1"}})
    con.send(json.dumps({"type": "add", "item": {"id": "m1", "modelKey": "event", "type": "motion", "start": 1, "device": "cam1"}}))
    con.send(ring("e1"))
    show("ringing", [mask(l) for l in pr.collect(1, 3.0)], 1)
    con.send(ring("e2"))
    say("## a second ring while ringing: %s" % pr.quiet(0.4))

    call("stream_url", "stream_url", {"device_id": host})
    con.status = 401
    call("stream_url refused", "stream_url", {"device_id": host})
    con.status = 0
    call("stream_url of an unknown device", "stream_url", {"device_id": "nope"})
    call("dial", "dial", {"device_id": host, "host": "127.0.0.1", "port": 5000})
    call("answer", "answer", {"device_id": host, "host": "127.0.0.1", "port": 5000})

    call("hangup the ring: the reply and call_state_changed ended", "hangup", {"call_id": "unifi-e1", "reason": "smoke"}, expected=2)
    call("hangup again: nobody holds it", "hangup", {"call_id": "unifi-e1"})
    con.send(ring("e3"))
    show("a ring after the hang-up", [mask(l) for l in pr.collect(1, 3.0)], 1)
    call("hangup it: the reply and call_state_changed ended", "hangup", {"call_id": "unifi-e3"}, expected=2)

    # a drop that reconnects at once is not reported; a drop that is refused is, once; recovery too
    before = con.upgrades
    con.drop()
    time.sleep(2.5)
    say("## after a drop: reconnected %s, nothing reported %s" % (con.upgrades == before + 1, pr.quiet(0.2)))
    con.status = 503
    con.drop()
    show("driver_availability false: refused (HTTP 503)", [mask(l) for l in pr.collect(1, 8.0)], 1)
    time.sleep(2.5)
    say("## still refused: nothing more reported %s" % pr.quiet(0.2))
    con.status = 0
    show("driver_availability true again", [mask(l) for l in pr.collect(1, 12.0)], 1)
    con.send(ring("e4"))
    show("a ring on the new connection", [mask(l) for l in pr.collect(1, 3.0)], 1)

    call("deconfigure", "deconfigure", {"device_id": host})
    call("deconfigure again", "deconfigure", {"device_id": host})
    say("## nothing else arrives: %s" % pr.quiet(0.3))
    pr.p.send_signal(signal.SIGTERM)
    say("  exit code: %s" % pr.wait(8))
    say("  socket file gone: %s" % (not os.path.exists(pr.sock)))
    con.close()


class FakeGuard:
    """bell-talk's /health on loopback: {"sessionUp": <state the script edits>}."""

    def __init__(self):
        self.up = False
        self.hits = 0
        guard = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self):
                guard.hits += 1
                body = json.dumps({"ok": True, "sessionUp": guard.up}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.http = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.http.daemon_threads = True
        threading.Thread(target=self.http.serve_forever, daemon=True).start()

    @property
    def url(self):
        return "http://127.0.0.1:%d/health" % self.http.server_address[1]

    def close(self):
        self.http.shutdown()


def talk_flow(binary, tmp):
    """unifi_protect_talk: configure and its refusals, external_ring (and that it announces
    nothing), a superseded ring, a hang-up with its reason, talk_health, a call out (dial) through
    a fake guard, a fake Protect console over TLS and a fake `ffmpeg` script on PATH, the guard
    refusing, dial_enabled false, deconfigure and SIGTERM. Every line is the same on the Go and the
    Rust binary. The answer of a ring needs a browser (webrtc_offer): tests/talk_hub.rs and
    tests/talk_media.rs cover it against fakes."""
    say("# unifi_protect_talk: configure, external_ring, hangup, talk_health, dial, deconfigure")
    if not shutil.which("openssl"):
        say("  skipped: no openssl to make a certificate")
        return
    shim = os.path.join(tmp, "talkshim")
    os.makedirs(shim, exist_ok=True)
    with open(os.path.join(shim, "ffmpeg"), "w") as f:
        f.write("#!/bin/sh\ncase \"$*\" in\n*pipe:1*) while [ ! -e %s/go ]; do sleep 0.02; done; exec sleep 30;;\n*) cat > %s/stdin;;\nesac\n" % (shim, shim))
    os.chmod(os.path.join(shim, "ffmpeg"), 0o755)
    env = dict(os.environ, PATH=shim + ":/usr/bin:/bin")
    con = UnifiConsole(tmp)
    guard = FakeGuard()
    pr = Proc(binary, tmp, sock_name="k.sock", env=env)
    pr.connect()
    host = con.host()
    mask = lambda t: re.sub(r"out-[0-9a-f]+", "<OUT>", t.replace(host, "<HOST>"))
    dev = "ha_event:front"

    def call(title, method, params, settle=0.35, timeout=6.0):
        """Sends one request, reads until its reply has arrived, then everything else that follows
        within `settle` seconds (the notifications: call_state_changed, call_reason, ...), and shows
        the lot in canonical order (the order a reply and its notifications reach the socket in is
        not a contract)."""
        step.n += 1
        want = step.n
        pr.send(json.dumps({"id": want, "method": method, "params": params}) + "\n")
        lines, got, end = [], False, time.time() + timeout
        while not got and time.time() < end:
            for l in pr.collect(1, 0.5):
                lines.append(l)
                try:
                    got = got or json.loads(l).get("id") == want
                except ValueError:
                    pass
        lines += pr.quiet(settle)
        show(title, [mask(l) for l in lines], len(lines))
        return lines

    def cfg(**extra):
        c = {"device_id": dev, "protect_host": host, "protect_api_key": UnifiConsole.KEY, "camera_id": "cam1",
             "guard_url": guard.url, "downlink_url": "rtsp://127.0.0.1:1/doorbell"}
        c.update(extra)
        return {"driver": "unifi_protect_talk", "config": c}

    step.n = 200
    call("configure with missing fields", "configure", {"driver": "unifi_protect_talk", "config": {"device_id": dev}})
    call("configure with a gain out of range", "configure", cfg(gain=40))
    call("configure with a bad downlink_url", "configure", cfg(downlink_url="http://x/y"))
    call("configure with a non-bool dial_enabled", "configure", cfg(dial_enabled="yes"))
    call("configure: the Capabilities struct, then driver_availability", "configure", cfg(), settle=0.5)
    call("configure the same door again", "configure", cfg())
    call("talk_health before any call", "talk_health", {"device_id": dev})
    call("talk_health of an unknown door", "talk_health", {"device_id": "nope"})
    call("talk_health without a device", "talk_health", {})

    call("external_ring", "external_ring", {"device_id": dev, "call_id": "ha-event-1", "hold": 30})
    say("## nothing announced by external_ring: %s" % pr.quiet(0.4))
    call("a new press supersedes it: the reply and call_state_changed ended", "external_ring", {"device_id": dev, "call_id": "ha-event-2", "hold": 30}, settle=0.5)
    call("external_ring without a call_id", "external_ring", {"device_id": dev})
    call("external_ring with a negative hold", "external_ring", {"device_id": dev, "call_id": "x", "hold": -1})
    call("external_ring of an unknown door", "external_ring", {"device_id": "nope", "call_id": "x"})
    call("hangup with the reason bell_pressed: the reply and call_state_changed ended", "hangup", {"call_id": "ha-event-2", "reason": "bell_pressed"}, settle=0.5)
    call("talk_health: why the last call ended", "talk_health", {"device_id": dev})
    call("hangup again: nobody holds it", "hangup", {"call_id": "ha-event-2", "reason": "bell_pressed"})

    call("answer", "answer", {"device_id": dev, "host": "127.0.0.1", "port": 5000})
    call("open_relay", "open_relay", {"device_id": dev})

    dial_lines = call("dial: the reply, then ringing, answered, active", "dial", {"device_id": dev}, settle=0.6)
    out_id = next((m.group(0) for l in dial_lines for m in [re.search(r"out-[0-9a-f]+", l)] if m), "out-none")
    say("## the console saw %d talkback-session POST(s), the guard %d GET(s)" % (getattr(con, "talkback_posts", 0), guard.hits))
    call("dial while the door is in a call", "dial", {"device_id": dev})
    call("talk_health during the call out", "talk_health", {"device_id": dev})
    call("hangup the call out with the reason bell_pressed", "hangup", {"call_id": out_id, "reason": "bell_pressed"}, settle=0.5)
    call("talk_health after the call out", "talk_health", {"device_id": dev})

    guard.up = True
    call("dial while the old intercom holds the sink", "dial", {"device_id": dev})
    say("## nothing reported by the refused dial: %s" % pr.quiet(0.4))
    guard.up = False

    call("configure a second door with dial_enabled false: the Capabilities struct, then driver_availability", "configure", cfg(device_id="ha_event:side", dial_enabled=False), settle=0.5)
    call("dial a door with dial out switched off", "dial", {"device_id": "ha_event:side"})
    call("deconfigure", "deconfigure", {"device_id": dev})
    call("deconfigure again", "deconfigure", {"device_id": dev})
    call("deconfigure the second door", "deconfigure", {"device_id": "ha_event:side"})
    say("## nothing else arrives: %s" % pr.quiet(0.3))
    pr.p.send_signal(signal.SIGTERM)
    say("  exit code: %s" % pr.wait(8))
    say("  socket file gone: %s" % (not os.path.exists(pr.sock)))
    leftover = subprocess.run(["pgrep", "-f", shim + "/ffmpeg"], capture_output=True, text=True).stdout.split()
    say("  ffmpeg processes left behind: %d" % len(leftover))
    guard.close()
    con.close()


def ordered_flow(binary, tmp):
    """R1-2 (Phase R round 1): the ORDER of a reply and the events a driver emits synchronously
    inside the same handler. Go's `Notify` writes in the calling goroutine, so `driver_availability`
    (from `Start`) and `ringing`, `answered`, `active` (from `DialCall`) are on the wire BEFORE the
    reply of `configure` / `dial`. Every other flow canonicalises the order of a reply and its
    notifications; this one prints the lines exactly as they arrived."""
    say("# ordered: a reply versus the events emitted inside its handler (arrival order)")
    if not shutil.which("openssl"):
        say("  skipped: no openssl to make a certificate")
        return
    shim = os.path.join(tmp, "ordshim")
    os.makedirs(shim, exist_ok=True)
    with open(os.path.join(shim, "ffmpeg"), "w") as f:
        f.write("#!/bin/sh\ncase \"$*\" in\n*pipe:1*) while [ ! -e %s/go ]; do sleep 0.02; done; exec sleep 30;;\n*) cat > %s/stdin;;\nesac\n" % (shim, shim))
    os.chmod(os.path.join(shim, "ffmpeg"), 0o755)
    env = dict(os.environ, PATH=shim + ":/usr/bin:/bin")
    con = UnifiConsole(tmp)
    guard = FakeGuard()
    pr = Proc(binary, tmp, sock_name="o.sock", env=env)
    pr.connect()
    host = con.host()
    mask = lambda t: re.sub(r"out-[0-9a-f]+", "<OUT>", t.replace(host, "<HOST>"))
    dev = "ha_event:front"

    def call(title, rid, method, params, expected, settle=0.5):
        pr.send(json.dumps({"id": rid, "method": method, "params": params}) + "\n")
        lines = pr.collect(expected, 6.0)
        lines += pr.quiet(settle)
        show(title, [mask(l) for l in lines], expected, ordered=True)

    cfg = {"driver": "unifi_protect_talk", "config": {
        "device_id": dev, "protect_host": host, "protect_api_key": UnifiConsole.KEY, "camera_id": "cam1",
        "guard_url": guard.url, "downlink_url": "rtsp://127.0.0.1:1/doorbell"}}
    call("configure: driver_availability, then the reply", 1, "configure", cfg, 2)
    call("dial: ringing, answered, active, then the reply", 2, "dial", {"device_id": dev}, 4)
    call("deconfigure", 3, "deconfigure", {"device_id": dev}, 1)
    say("## nothing else arrives: %s" % pr.quiet(0.3))
    pr.p.send_signal(signal.SIGTERM)
    say("  exit code: %s" % pr.wait(8))
    guard.close()
    con.close()


def webrtc_flow(binary, tmp):
    """The webrtc_* verbs. Error paths and garbage offers are printed as exact JSON lines and must
    be byte-identical between Go and Rust. A valid offer is answered with a DIFFERENT SDP by each
    stack (pion vs str0m: ids, ufrag, fingerprint, candidates, attribute order), so for those only
    the structure is printed: the m=audio line, the rtpmap lines, whether the answer carries host
    candidates and an end-of-candidates marker. Text that differs by design (str0m's own parse
    errors of malformed offers, the wording of a rejected candidate) is printed as its error code
    only; PARITY.md lists each."""
    say("# webrtc_*: error paths byte for byte, valid offers structurally")
    here = os.path.dirname(os.path.abspath(__file__))
    fx = os.path.join(here, "..", "tests", "interop", "fixtures")
    chrome = open(os.path.join(fx, "chrome124_offer.sdp")).read().replace("\r\n", "\n").replace("\n", "\r\n")
    firefox = open(os.path.join(fx, "firefox153_offer.sdp")).read().replace("\r\n", "\n").replace("\n", "\r\n")
    opus_only = chrome.replace("SAVPF 111 63 9 0 8 13 110 126", "SAVPF 111").replace("a=rtpmap:111 opus/48000/2", "a=rtpmap:111 opus/48000/2")
    pr = Proc(binary, tmp, sock_name="w.sock")
    pr.connect()
    n = [0]

    def req(method, **params):
        n[0] += 1
        return '{"id":%d,"method":"%s","params":%s}\n' % (n[0], method, json.dumps(params))

    def exact(title, text):
        step(pr, title, text, 1)

    def code_only(title, text):
        pr.send(text)
        lines = pr.collect(1, 5.0)
        say("## " + title)
        for l in lines:
            m = json.loads(l)
            say("  " + json.dumps({"id": m.get("id"), "error_code": (m.get("error") or {}).get("code"),
                                   "ok": "result" in m}, sort_keys=True))

    def structure(title, text):
        pr.send(text)
        lines = pr.collect(1, 8.0)
        say("## " + title)
        for l in lines:
            m = json.loads(l)
            if "result" not in m:
                say("  " + canon(l))
                continue
            sdp = m["result"]["sdp"]
            ls = sdp.split("\r\n")
            maudio = [x for x in ls if x.startswith("m=audio")]
            say("  keys: %s" % sorted(m["result"]))
            say("  m=audio payload types: %s" % [x.split()[3:] for x in maudio])
            say("  rtpmap: %s" % sorted(x for x in ls if x.startswith("a=rtpmap:")))
            say("  has host candidates: %s; end-of-candidates: %s; ice-ufrag: %s; ice-pwd: %s; fingerprint: %s; setup: %s; rtcp-mux: %s" % (
                any(x.startswith("a=candidate:") for x in ls), "a=end-of-candidates" in ls,
                any(x.startswith("a=ice-ufrag:") for x in ls), any(x.startswith("a=ice-pwd:") for x in ls),
                any(x.startswith("a=fingerprint:sha-256 ") for x in ls), sorted(x for x in ls if x.startswith("a=setup:")),
                "a=rtcp-mux" in ls))
            say("  answer ends with CRLF: %s; opus anywhere: %s" % (sdp.endswith("\r\n"), "opus" in sdp.lower()))

    exact("ice, no session", req("webrtc_ice", call_id="w-none", candidate="candidate:1 1 udp 1 127.0.0.1 9 typ host"))
    exact("answer, no session", req("webrtc_answer", call_id="w-none", sdp="v=0"))
    exact("offer without sdp", req("webrtc_offer", call_id="w-1", sdp=""))
    exact("offer without call id", req("webrtc_offer", call_id="", sdp="v=0"))
    exact("offer, call_id of the wrong type", '{"id":%d,"method":"webrtc_offer","params":{"call_id":5,"sdp":"x"}}\n' % (n[0] + 1))
    n[0] += 1
    exact("offer, sdp of the wrong type", '{"id":%d,"method":"webrtc_offer","params":{"call_id":"w-1","sdp":5}}\n' % (n[0] + 1))
    n[0] += 1
    exact("offer that is not an SDP at all", req("webrtc_offer", call_id="w-bad", sdp="garbage"))
    exact("offer 'x=1'", req("webrtc_offer", call_id="w-bad2", sdp="x=1\r\n"))
    code_only("candidate after the failed offer (Go keeps the session: webrtc_failed; transport-rs drops it: unknown_session)", req("webrtc_ice", call_id="w-bad", candidate="candidate:1 1 udp 1 127.0.0.1 9 typ host"))
    code_only("end-of-candidates after the failed offer (same difference)", req("webrtc_ice", call_id="w-bad", candidate=""))
    code_only("answer after the failed offer (same difference)", req("webrtc_answer", call_id="w-bad", sdp="v=0\r\n"))
    code_only("garbage answer (same difference)", req("webrtc_answer", call_id="w-bad", sdp="garbage"))
    exact("Opus-only offer", req("webrtc_offer", call_id="w-opus", sdp=opus_only))
    code_only("offer with no media section (str0m and pion word it differently)", req("webrtc_offer", call_id="w-nom", sdp="v=0\r\no=- 1 2 IN IP4 127.0.0.1\r\ns=-\r\nt=0 0\r\n"))
    structure("a real Chrome 124 offer", req("webrtc_offer", call_id="w-chrome", sdp=chrome))
    structure("a real Firefox 153 offer, same process, second call", req("webrtc_offer", call_id="w-ff", sdp=firefox))
    structure("the Chrome offer again for the same call (a renegotiation reuses the session)", req("webrtc_offer", call_id="w-chrome", sdp=chrome))
    exact("candidate after the answer", req("webrtc_ice", call_id="w-chrome", candidate="candidate:1 1 UDP 2130706431 127.0.0.1 12345 typ host"))
    exact("end-of-candidates after the answer (empty candidate)", req("webrtc_ice", call_id="w-chrome", candidate=""))
    exact("an mDNS candidate after the answer", req("webrtc_ice", call_id="w-chrome", candidate="candidate:1 1 udp 2130706431 abcd-1234.local 5000 typ host"))
    code_only("a garbage candidate (the error text is the library's)", req("webrtc_ice", call_id="w-chrome", candidate="garbage"))
    exact("answer when stable", req("webrtc_answer", call_id="w-chrome", sdp=chrome))
    code_only("an offer cut in half (the error text is the library's)", req("webrtc_offer", call_id="w-cut", sdp=chrome[:len(chrome) // 2]))
    exact("the other call is untouched: a candidate for it", req("webrtc_ice", call_id="w-ff", candidate=""))
    say("## the process is alive after all of it: %s" % (pr.p.poll() is None))
    pr.p.send_signal(signal.SIGTERM)
    say("  exit code: %s" % pr.wait(5))


def sigterm_connected(binary, tmp):
    say("# SIGTERM with a live connection")
    pr = Proc(binary, tmp, sock_name="u.sock")
    pr.connect()
    step(pr, "round trip", '{"id":1,"method":"keepalive"}\n', 1)
    st = os.stat(pr.sock)
    say("  socket mode: %o" % stat.S_IMODE(st.st_mode))
    pr.p.send_signal(signal.SIGTERM)
    say("  exit code: %s" % pr.wait(5))
    say("  socket file gone: %s" % (not os.path.exists(pr.sock)))
    say("  process output: " + repr(pr.log_text()))


def sigint_idle(binary, tmp):
    say("# SIGINT with no connection")
    pr = Proc(binary, tmp, sock_name="v.sock")
    for _ in range(100):
        if os.path.exists(pr.sock):
            break
        time.sleep(0.05)
    say("  socket mode: %o" % stat.S_IMODE(os.stat(pr.sock).st_mode))
    pr.p.send_signal(signal.SIGINT)
    say("  exit code: %s" % pr.wait(5))
    say("  socket file gone: %s" % (not os.path.exists(pr.sock)))
    say("  process output: " + repr(pr.log_text()))


def disconnect(binary, tmp):
    say("# client disconnects")
    pr = Proc(binary, tmp, sock_name="w.sock")
    pr.connect()
    step(pr, "round trip", '{"id":1,"method":"keepalive"}\n', 1)
    pr.s.close()
    say("  exit code: %s" % pr.wait(5))
    say("  socket file gone: %s" % (not os.path.exists(pr.sock)))
    say("  process output: " + repr(pr.log_text()))


def stale_socket(binary, tmp):
    say("# stale socket file is replaced")
    path = os.path.join(tmp, "x.sock")
    old = socket.socket(socket.AF_UNIX)
    old.bind(path)
    old.close()  # leaves the file behind, like a crashed process
    say("  stale file present before start: %s" % os.path.exists(path))
    pr = Proc(binary, tmp, sock_name="x.sock")
    pr.connect()
    step(pr, "round trip", '{"id":1,"method":"keepalive"}\n', 1)
    say("  socket mode: %o" % stat.S_IMODE(os.stat(path).st_mode))
    pr.s.close()
    say("  exit code: %s" % pr.wait(5))


def flag_matrix(binary, tmp):
    say("# flags")
    cases = [
        [], ["-h"], ["--help"], ["-help"], ["--bogus"], ["-socket"], ["-listen=x", "-socket=y"],
        ["-socket=" + os.path.join(tmp, "nodir", "x.sock")], ["--house-calls=maybe", "-socket=a"],
        ["---x"], ["-=x"], ["x", "-socket", "a"],
        ["-secrets-env=/nonexistent", "-socket=a"],
    ]
    for c in cases:
        p = subprocess.run([binary, *c], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, timeout=10)
        text = p.stdout.decode("utf-8", "replace").replace(binary, "<BIN>").replace(tmp, "<TMP>")
        say("## args %r" % [a.replace(tmp, "<TMP>") for a in c])
        say("  exit code: %d" % p.returncode)
        for l in text.splitlines():
            say("  | " + l)
    # accepted flags with a valid socket: they must not stop the server from answering.
    for c in (["-host", "10.0.0.1"], ["--host=10.0.0.1"], ["-ipc-allow-non-loopback"],
              ["-ipc-allow-from", "1.2.3.4"], ["-ipc-token-file", "/nonexistent"]):
        pr = Proc(binary, tmp, extra=c, sock_name="f.sock")
        pr.connect()
        pr.send('{"id":1,"method":"keepalive"}\n')
        say("## accepted flags %r -> %s" % (c, [canon(l) for l in pr.collect(1)]))
        pr.p.send_signal(signal.SIGTERM)
        pr.wait(5)
    pr = Proc(binary, tmp, extra=["-house-calls"], sock_name="g.sock")
    pr.connect()
    pr.send('{"id":1,"method":"house_open","params":{}}\n')
    say("## -house-calls house_open -> %s" % [canon(l) for l in pr.collect(1)])
    pr.p.send_signal(signal.SIGTERM)
    pr.wait(5)


def house_flow(binary, tmp):
    """house calls (slice 9, `-house-calls`): two frame-channel legs joined in the hub. The
    bridge carries a 40 ms L16 frame from the `from` phone to the `to` phone as 320 bytes of
    mu-law and back; wrong frames are refused as the door path refuses them; `house_close` and a
    phone's goodbye tell Python (`media_closed`, `house_leg_state`); the cap, the owner cap and
    the refusals; a WebRTC leg whose offer cannot be answered ends the call by event; SIGTERM
    closes every bridge. The random session ids are printed as house-S1, house-S2, ... in order
    of first appearance. Every line is the same on the Go and the Rust binary."""
    say("# house calls: -house-calls")
    pr = Proc(binary, tmp, extra=("-house-calls",), sock_name="h.sock")
    pr.connect()
    sids = []

    def norm(l):
        def sub(m):
            if m.group(0) not in sids:
                sids.append(m.group(0))
            return "house-S%d" % (sids.index(m.group(0)) + 1)
        return re.sub(r"house-[0-9a-f]{16}", sub, l)

    def real(t):
        for i, sid in enumerate(sids):
            t = t.replace("{S%d}" % (i + 1), sid)
        return t

    def hstep(title, payload, expected, ordered=True, timeout=3.0, reply_after=None):
        pr.send(real(payload))
        show(title, [norm(l) for l in pr.collect(expected, timeout)], expected, ordered, reply_after)

    frames = lambda n, f: base64.b64encode(bytes((f(i) & 0xFF) for i in range(n))).decode()
    two = ('[{"side":"from","kind":"frames","codec":"l16"},'
           '{"side":"to","kind":"frames","codec":"pcmu"}]')
    hstep("house_open hso-1", '{"id":1,"method":"house_open","params":{"call_id":"hso-1","legs":%s}}\n' % two, 1)
    hstep("l16 frame from the from-phone: first_frame, mu-law to the to-phone, reply (the reply floats: PARITY 360)",
          '{"id":2,"method":"media_frame","params":{"call_id":"{S1}-from","d":"%s"}}\n'
          % frames(1280, lambda i: (i * 37) % 251), 3, reply_after=0)
    hstep("mu-law frame from the to-phone: first_frame, l16 to the from-phone, reply (the reply floats: PARITY 360)",
          '{"id":3,"method":"media_frame","params":{"call_id":"{S1}-to","d":"%s"}}\n' % frames(320, lambda i: i), 3, reply_after=0)
    hstep("wrong size", '{"id":4,"method":"media_frame","params":{"call_id":"{S1}-from","d":"%s"}}\n' % frames(7, lambda i: i), 1)
    hstep("not base64", '{"id":5,"method":"media_frame","params":{"call_id":"{S1}-from","d":"***"}}\n', 1)
    hstep("unknown house leg", '{"id":6,"method":"media_frame","params":{"call_id":"house-0000000000000000-from","d":"AAAA"}}\n', 1)
    hstep("a door id still goes to the door path", '{"id":7,"method":"media_frame","params":{"call_id":"gso-1","d":"AAAA"}}\n', 1)
    hstep("house_close: media_closed for both legs BEFORE the reply",
          '{"id":8,"method":"house_close","params":{"call_id":"hso-1"}}\n', 3, ordered=True)
    hstep("house_close again", '{"id":9,"method":"house_close","params":{"call_id":"hso-1"}}\n', 1)
    hstep("house_close without an id", '{"id":10,"method":"house_close","params":{}}\n', 1)
    for i, p in enumerate(['{}', 'null', '{"call_id":"hso-2"}', '{"call_id":"gso-2","legs":%s}' % two,
                           '{"call_id":"hso-2","legs":5}', '{"call_id":"hso-2","legs":[{"side":3}]}',
                           '{"call_id":"hso-2","legs":[{"side":"from","kind":"frames","codec":"mp3"},{"side":"to","kind":"frames","codec":"l16"}]}',
                           '{"call_id":"hso-2","legs":[{"side":"from","kind":"webrtc"},{"side":"to","kind":"frames","codec":"l16"}]}']):
        hstep("house_open refused: %s" % p[:60], '{"id":%d,"method":"house_open","params":%s}\n' % (20 + i, p), 1)
    hstep("house_open hso-3", '{"id":30,"method":"house_open","params":{"call_id":"hso-3","legs":%s}}\n' % two, 1)
    hstep("duplicate", '{"id":31,"method":"house_open","params":{"call_id":"hso-3","legs":%s}}\n' % two, 1)
    hstep("a phone's goodbye: both media_closed BEFORE the reply, then both lost (the reply floats after the media_closed: PARITY 360)",
          '{"id":32,"method":"media_close","params":{"call_id":"{S3}-from","reason":"phone_hung_up"}}\n', 5, reply_after=2)
    pr.quiet(0.3)
    own = lambda c, a, b: ('{"id":%d,"method":"house_open","params":{"call_id":"%s","legs":[{"side":"from","kind":"frames","codec":"l16","owner":"%s"},'
                           '{"side":"to","kind":"frames","codec":"l16","owner":"%s"}]}}\n' % (40 + int(c[-1]), c, a, b))
    hstep("alice's call", own("hso-4", "alice", "alice"), 1)
    hstep("alice's third leg: too_many_calls", own("hso-5", "alice", ""), 1)
    for i in (6, 7, 8):
        hstep("house_open hso-%d" % i, '{"id":%d,"method":"house_open","params":{"call_id":"hso-%d","legs":%s}}\n' % (50 + i, i, two), 1)
    hstep("the fifth call: too_many_calls", '{"id":60,"method":"house_open","params":{"call_id":"hso-9","legs":%s}}\n' % two, 1)
    hstep("a door still gets its leg while the house is full",
          '{"id":61,"method":"media_open","params":{"call_id":"gso-1","codec":"pcmu"}}\n', 1)
    hstep("house_close hso-4: the reply and both media_closed",
          '{"id":62,"method":"house_close","params":{"call_id":"hso-4"}}\n', 3)
    hstep("a webrtc leg with a garbage offer: the open answers first",
          '{"id":63,"method":"house_open","params":{"call_id":"hso-10","legs":[{"side":"from","kind":"webrtc","sdp":"not an sdp"},{"side":"to","kind":"frames","codec":"l16"}]}}\n', 4)
    pr.send('{"id":64,"method":"house_open","params":{"call_id":"hso-11","legs":%s}}\n' % two)
    show("hso-11 opened", [norm(l) for l in pr.collect(1)], 1)
    say("## nothing else arrives: %s" % pr.quiet(0.3))
    pr.p.send_signal(signal.SIGTERM)
    show("SIGTERM closes every bridge (order open in Go: CloseAll walks a map, house.go:453-466)", [norm(l) for l in pr.collect(8, 4.0)], 8, ordered=False)
    say("  exit code: %s" % pr.wait(5))


class TcpConn:
    """A client of the TCP IPC (`-listen`): the same line helpers as Proc, over a TCP socket."""
    send = Proc.send
    collect = Proc.collect
    quiet = Proc.quiet

    def __init__(self, port):
        self.s = socket.create_connection(("127.0.0.1", port), timeout=3)
        self.buf = b""

    def closed(self, timeout=1.0):
        """True when the server has closed the connection (EOF or reset) within `timeout`."""
        end = time.time() + timeout
        while time.time() < end:
            r, _, _ = select.select([self.s], [], [], max(0.0, end - time.time()))
            if not r:
                continue
            try:
                d = self.s.recv(65536)
            except OSError:
                return True
            if not d:
                return True
            self.buf += d
        return False


def free_tcp_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


FAKE_TOKEN = "smoke-token-0123456789abcdefghij"
FAKE_SIP_PASSWORD = "smokepass123"


def standalone_flow(binary, tmp):
    """The standalone/container mode (slice 10): `-listen` (the TCP IPC with its token), `-sip-from-env`
    (one gate_sip_server started from PORTICO_SIP_* before the hub connects; the hub's configure adopts
    it and its deconfigure only detaches), `-secrets-env`, and the start-up refusals. Every line is the
    same on the Go and the Rust binary, except the texts that name `netip`'s own parse error (not
    exercised here). The ports are masked; no secret is real."""
    say("# standalone: -listen, -sip-from-env, -secrets-env")
    port, sip_port = free_tcp_port(), free_udp_port()
    def mask(t):
        t = t.replace(str(port), "<PORT>").replace(str(sip_port), "<SIPPORT>").replace(tmp, "<TMP>").replace(binary, "<BIN>")
        t = re.sub(r"\d{4}/\d\d/\d\d \d\d:\d\d:\d\d", "<TIME>", t)  # Go logs local time, Rust UTC
        return re.sub(r"RTP on :\d+", "RTP on :<RTP>", t)

    def start(args, env, name):
        base = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}  # no PORTICO_* from the caller
        log = open(os.path.join(tmp, "sa-%s.log" % name), "w+b")
        p = subprocess.Popen([binary, *args], stdout=log, stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL, env={**base, **env})
        return p, log

    def logtext(log):
        log.flush()
        log.seek(0)
        return mask(log.read().decode("utf-8", "replace"))

    def wait_port(p, prt):
        for _ in range(100):
            if p.poll() is not None:
                return False
            try:
                socket.create_connection(("127.0.0.1", prt), timeout=0.2).close()
                return True
            except OSError:
                time.sleep(0.05)
        return False

    env = {"PORTICO_IPC_TOKEN": FAKE_TOKEN, "PORTICO_SIP_LISTEN": "127.0.0.1:%d" % sip_port,
           "PORTICO_SIP_PASSWORD": FAKE_SIP_PASSWORD, "PORTICO_SIP_RTP_PORT": "0",
           "PORTICO_SIP_DEVICE_ID": "gate-sa"}
    p, log = start(["-listen=127.0.0.1:%d" % port, "-sip-from-env"], env, "a")
    say("  listening: %s" % wait_port(p, port))
    auth = lambda tok: '{"id":1,"method":"auth","params":{"token":"%s"}}\n' % tok
    # refused before anything is served
    c = TcpConn(port)
    c.send('{"id":1,"method":"keepalive"}\n')
    say("## a first line that is not auth: reply=%r closed=%s" % (c.collect(1, 0.5), c.closed()))
    c = TcpConn(port)
    c.send(auth("wrong-token-wrong-token-wrong"))
    show("a wrong token", c.collect(1), 1)
    say("  closed after the refusal: %s" % c.closed())
    c = TcpConn(port)
    c.send("garbage\n")
    say("## garbage first line: reply=%r closed=%s" % (c.collect(1, 0.5), c.closed()))
    # the hub
    c = TcpConn(port)
    step(c, "auth", auth(FAKE_TOKEN), 1)
    step(c, "keepalive", '{"id":2,"method":"keepalive"}\n', 1)
    step(c, "unknown method", '{"id":3,"method":"nope"}\n', 1)
    step(c, "configure of the preloaded device is adopted (the hub's own config is ignored)",
         '{"id":4,"method":"configure","params":{"driver":"gate_sip_server","config":{"device_id":"gate-sa","sip_listen":"0.0.0.0:5060","password":"x"}}}\n', 1)
    step(c, "deconfigure of the preloaded device only detaches", '{"id":5,"method":"deconfigure","params":{"device_id":"gate-sa"}}\n', 1)
    step(c, "the device is still there: external_ring reaches its driver",
         '{"id":6,"method":"external_ring","params":{"device_id":"gate-sa"}}\n', 1)
    step(c, "deconfigure of a device nobody owns", '{"id":7,"method":"deconfigure","params":{"device_id":"other"}}\n', 1)
    step(c, "configure of another driver takes the normal path", '{"id":8,"method":"configure","params":{"driver":"nope","config":{"device_id":"other"}}}\n', 1)
    step(c, "configure of another gate_sip_server is validated by the driver",
         '{"id":9,"method":"configure","params":{"driver":"gate_sip_server","config":{"device_id":"second","sip_listen":"0.0.0.0:0","password":"short"}}}\n', 1)
    c.s.close()
    time.sleep(0.3)
    say("  still running after the hub left: %s" % (p.poll() is None))
    c = TcpConn(port)
    step(c, "auth again", auth(FAKE_TOKEN), 1)
    step(c, "re-adopt after the reconnect", '{"id":2,"method":"configure","params":{"driver":"gate_sip_server","config":{"device_id":"gate-sa"}}}\n', 1)
    p.send_signal(signal.SIGTERM)
    say("  exit code: %s" % p.wait(8))
    say("  the port is closed after the exit: %s" % (not wait_port(p, port)))
    say("  process output: %r" % logtext(log))

    # -secrets-env supplies the token and the SIP password
    sec = os.path.join(tmp, "box.env")
    with open(sec, "w") as f:
        f.write("# box\nGATE_SIP_PASSWORD=%s\nGATE_SIP_PEER_TOKEN=%s\nGATE_SIP_TOKEN=owner-secret-never-read-000000\nOTHER=x\n"
                % (FAKE_SIP_PASSWORD, FAKE_TOKEN))
    p, log = start(["-listen=127.0.0.1:%d" % port, "-sip-from-env", "-secrets-env=" + sec],
                   {"PORTICO_SIP_LISTEN": "127.0.0.1:%d" % sip_port, "PORTICO_SIP_RTP_PORT": "0"}, "b")
    say("  listening with -secrets-env: %s" % wait_port(p, port))
    c = TcpConn(port)
    step(c, "auth with the file's token", auth(FAKE_TOKEN), 1)
    step(c, "adopt the device named by the default id", '{"id":2,"method":"configure","params":{"driver":"gate_sip_server","config":{"device_id":"gate-sip-server:127.0.0.1:%d"}}}\n' % sip_port, 1)
    p.send_signal(signal.SIGTERM)
    say("  exit code: %s" % p.wait(8))
    say("  process output: %r" % logtext(log))

    # start-up refusals: every one exits 1 with one line
    good = {"PORTICO_IPC_TOKEN": FAKE_TOKEN}
    bad_env = os.path.join(tmp, "bad.env")
    with open(bad_env, "w") as f:
        f.write("GATE_SIP_PASSWORD=short\n")
    cases = [
        (["-listen=127.0.0.1:0"], {}),
        (["-listen=127.0.0.1:0"], {"PORTICO_IPC_TOKEN": FAKE_TOKEN[:23]}),
        (["-listen=0.0.0.0:0"], good),
        (["-listen=0.0.0.0:0", "-ipc-allow-non-loopback"], good),
        (["-listen=127.0.0.1:0", "-ipc-allow-from=0.0.0.0/0"], good),
        (["-listen=127.0.0.1"], good),
        (["-listen=127.0.0.1:70000"], good),
        (["-listen=127.0.0.1:1:2"], good),
        (["-listen=127.0.0.1:0", "-ipc-token-file=/nonexistent/tok"], good),
        (["-listen=127.0.0.1:0", "-sip-from-env"], good),
        (["-listen=127.0.0.1:0", "-sip-from-env"], {**good, "PORTICO_SIP_LISTEN": "127.0.0.1:0", "PORTICO_SIP_RTP_PORT": "x"}),
        (["-listen=127.0.0.1:0", "-sip-from-env"], {**good, "PORTICO_SIP_LISTEN": "0.0.0.0:5060", "PORTICO_SIP_PASSWORD": FAKE_SIP_PASSWORD}),
        (["-listen=127.0.0.1:0", "-sip-from-env"], {**good, "PORTICO_SIP_LISTEN": "127.0.0.1:0", "PORTICO_SIP_PASSWORD": "short"}),
        (["-listen=127.0.0.1:0", "-sip-from-env"], {**good, "PORTICO_SIP_LISTEN": "127.0.0.1:0", "PORTICO_SIP_PASSWORD_FILE": "/nonexistent/pw"}),
        (["-listen=127.0.0.1:0", "-secrets-env=" + bad_env], good),
        (["-listen=127.0.0.1:0", "-secrets-env=/nonexistent/.env"], good),
        (["-listen=127.0.0.1:0", "-socket=" + os.path.join(tmp, "z.sock")], good),
        ([], good),
    ]
    for args, e in cases:
        base = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), **e}
        r = subprocess.run([binary, *args], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, timeout=10, env=base)
        say("## refused: %r env=%r" % ([mask(a) for a in args], sorted(k for k in e)))
        say("  exit code: %d" % r.returncode)
        for l in mask(r.stdout.decode("utf-8", "replace")).splitlines():
            say("  | " + l)


def main():
    if len(sys.argv) not in (2, 3):
        raise SystemExit("usage: ipc_smoke.py <binary> [unifi|talk|wsmedia|house|standalone]")
    binary = os.path.abspath(sys.argv[1])
    with tempfile.TemporaryDirectory(prefix="ipc-smoke-") as tmp:
        if len(sys.argv) == 3:  # one flow only, while developing it
            {"unifi": unifi_flow, "talk": talk_flow, "wsmedia": wsmedia_flow, "house": house_flow, "standalone": standalone_flow, "ordered": ordered_flow}[sys.argv[2]](binary, tmp)
            return
        main_script(binary, tmp)
        gate_sip_server_flow(binary, tmp)
        dahua_flow(binary, tmp)
        wsmedia_flow(binary, tmp)
        house_flow(binary, tmp)
        standalone_flow(binary, tmp)
        unifi_flow(binary, tmp)
        talk_flow(binary, tmp)
        ordered_flow(binary, tmp)
        webrtc_flow(binary, tmp)
        sigterm_connected(binary, tmp)
        sigint_idle(binary, tmp)
        disconnect(binary, tmp)
        stale_socket(binary, tmp)
        flag_matrix(binary, tmp)


if __name__ == "__main__":
    main()
