#!/usr/bin/env python3
"""Scenario G: hostile / garbled offers against an answerer's line-JSON socket (no browser).
usage: hostile.py <answerer binary> [more answerer args]   -> prints one JSON result.
Each case is sent on its own connection; the answerer process must stay alive and must answer each case
with {"t":"error"} (or, for a syntactically valid offer, an answer), never die. After the cases a good
offer (a real libwebrtc-shaped one) is sent to prove the process still serves."""
import json, os, random, signal, socket, subprocess, sys, time

GOOD = "\r\n".join([
 "v=0", "o=- 4611731400430051336 2 IN IP4 127.0.0.1", "s=-", "t=0 0", "a=group:BUNDLE 0", "a=msid-semantic: WMS x",
 "m=audio 9 UDP/TLS/RTP/SAVPF 111 63 9 0 8 110 126", "c=IN IP4 0.0.0.0", "a=rtcp:9 IN IP4 0.0.0.0",
 "a=ice-ufrag:abcd", "a=ice-pwd:0123456789012345678901", "a=ice-options:trickle",
 "a=fingerprint:sha-256 " + ":".join(["AB"] * 32), "a=setup:actpass", "a=mid:0", "a=sendrecv", "a=rtcp-mux",
 "a=rtpmap:111 opus/48000/2", "a=fmtp:111 minptime=10;useinbandfec=1", "a=rtpmap:63 red/48000/2", "a=rtpmap:9 G722/8000",
 "a=rtpmap:0 PCMU/8000", "a=rtpmap:8 PCMA/8000", "a=rtpmap:110 telephone-event/48000", "a=rtpmap:126 telephone-event/8000",
 "a=ssrc:1234 cname:x", ""])
CAND = "a=candidate:%d 1 udp 2113937151 192.168.%d.%d %d typ host generation 0\r\n"

def cases():
    r = random.Random(7)
    c = {}
    c["empty"] = ""
    c["truncated-half"] = GOOD[: len(GOOD) // 2]
    c["truncated-in-m-line"] = GOOD[: GOOD.index("m=audio") + 14]
    c["truncated-in-fingerprint"] = GOOD[: GOOD.index("a=fingerprint") + 25]
    c["no-m-line"] = GOOD.split("m=audio")[0] + "a=ice-ufrag:abcd\r\na=ice-pwd:0123456789012345678901\r\n"
    c["binary-random-4k"] = bytes(r.randrange(256) for _ in range(4096)).decode("latin-1")
    c["binary-nul-and-ctrl"] = "v=0\r\n" + "\x00\x01\x02\x1b[2J\r\n" * 50
    c["garbage-lines"] = "\r\n".join("zz=%d" % i for i in range(200))
    c["huge-3MB-attrs"] = GOOD + "a=x-pad:" + "A" * (3 * 1024 * 1024) + "\r\n"
    c["huge-lines-100k"] = GOOD + "".join("a=x-%d:y\r\n" % i for i in range(100000))
    c["absurd-candidates-50k"] = GOOD + "".join(CAND % (i, i % 250, i // 250 % 250, 1024 + i % 60000) for i in range(50000))
    c["absurd-candidates-ipv6-20k"] = GOOD + "".join("a=candidate:%d 1 udp 2113937151 2001:db8::%x %d typ host\r\n" % (i, i, 1024 + i % 60000) for i in range(20000))
    media = GOOD[GOOD.index("m=audio"):]
    c["duplicate-mids"] = GOOD + media                       # two complete m-sections, both a=mid:0
    c["many-m-lines-200-distinct-mids"] = GOOD + "".join(media.replace("a=mid:0", "a=mid:%d" % (i + 1)) for i in range(200))
    c["many-m-lines-200-same-mid"] = GOOD + media * 200
    c["bad-payload-types"] = GOOD.replace("m=audio 9 UDP/TLS/RTP/SAVPF 111 63 9 0 8 110 126", "m=audio 9 UDP/TLS/RTP/SAVPF 999999 -1 abc 0")
    c["m-port-overflow"] = GOOD.replace("m=audio 9 ", "m=audio 99999999999999999999 ")
    c["rtpmap-no-slash"] = GOOD.replace("a=rtpmap:0 PCMU/8000", "a=rtpmap:0 PCMU")
    c["rtpmap-rate-overflow"] = GOOD.replace("a=rtpmap:0 PCMU/8000", "a=rtpmap:0 PCMU/99999999999999999999/99999999999")
    c["fingerprint-garbage"] = GOOD.replace("a=fingerprint:sha-256 " + ":".join(["AB"] * 32), "a=fingerprint:sha-999 ZZ:ZZ")
    c["setup-bogus"] = GOOD.replace("a=setup:actpass", "a=setup:bogus")
    c["ssrc-flood-100k"] = GOOD + "".join("a=ssrc:%d cname:x\r\n" % i for i in range(100000))
    c["deep-fmtp-1MB"] = GOOD.replace("a=fmtp:111 minptime=10;useinbandfec=1", "a=fmtp:111 " + ";".join("k%d=v" % i for i in range(100000)))
    c["lf-only-line-endings"] = GOOD.replace("\r\n", "\n")
    c["INJECTED-panic-inside-accept_offer (needs S1B_INJECT_PANIC=1)"] = GOOD + "a=x-s1b-panic-now:1\r\n"
    c["utf8-emoji"] = GOOD.replace("cname:x", "cname:\U0001F4A5é中")
    return c

def one(addr, payload_obj, raw=None, timeout=20):
    s = socket.create_connection(addr)
    s.settimeout(timeout)
    data = raw if raw is not None else (json.dumps(payload_obj) + "\n").encode()
    t0 = time.time()
    try:
        s.sendall(data)
    except Exception as e:
        # the answerer closed while we were still sending (e.g. an oversized line): that is a refusal
        return {"reply": "closed-while-sending", "e": str(e)[:60], "ms": int((time.time() - t0) * 1000)}
    f = s.makefile("r", encoding="utf-8", errors="replace")
    out = None
    try:
        while True:
            line = f.readline()
            if not line:
                out = {"reply": "closed-no-reply"}
                break
            m = json.loads(line)
            if m.get("t") in ("answer", "error"):
                out = {"reply": m["t"], "msg": (m.get("msg") or "")[:120], "answer_m_lines": [l for l in m.get("sdp", "").splitlines() if l.startswith("m=")]}
                break
    except socket.timeout:
        out = {"reply": "timeout"}
    except OSError as e:
        out = {"reply": "reset-by-peer-after-close", "e": str(e)[:60]}
    out["ms"] = int((time.time() - t0) * 1000)
    try:
        s.close()
    except Exception:
        pass
    return out

def main():
    binary, extra = sys.argv[1], sys.argv[2:]
    log = open(os.environ.get("HOSTILE_LOG", "hostile.answerer.log"), "w")
    p = subprocess.Popen([binary, "--listen", "127.0.0.1:0"] + extra, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    addr = None
    for _ in range(100):
        time.sleep(0.1)
        for line in open(log.name):
            if '"LISTEN"' in line:
                a = json.loads(line)["addr"]; h, port = a.rsplit(":", 1); addr = (h, int(port))
        if addr: break
    res = {"pid": p.pid, "cases": {}}
    try:
        for name, sdp in cases().items():
            r = one(addr, {"t": "offer", "sdp": sdp})
            r["alive_after"] = p.poll() is None
            r["size"] = len(sdp)
            res["cases"][name] = r
        # not an SDP problem: non-JSON, a JSON that is not an object, an oversized signalling line
        for name, raw in [("signalling-binary", os.urandom(2048) + b"\n"), ("signalling-json-array", b"[1,2,3]\n"), ("signalling-offer-without-sdp", b'{"t":"offer"}\n'),
                          ("signalling-offer-sdp-is-number", b'{"t":"offer","sdp":12345}\n'), ("signalling-line-12MB", b'{"t":"offer","sdp":"' + b"A" * (12 * 1024 * 1024) + b'"}\n')]:
            r = one(addr, None, raw=raw)
            r["alive_after"] = p.poll() is None
            res["cases"][name] = r
        # good offer afterwards
        r = one(addr, {"t": "offer", "sdp": GOOD}); r["alive_after"] = p.poll() is None
        res["cases"]["GOOD-after-all-hostile"] = r
        # also the good offer with /1 channel count
        res["cases"]["GOOD-PCMU-8000-1"] = one(addr, {"t": "offer", "sdp": GOOD.replace("a=rtpmap:0 PCMU/8000", "a=rtpmap:0 PCMU/8000/1")})
    finally:
        res["alive_at_end"] = p.poll() is None
        try:
            os.killpg(p.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    res["survived_all"] = res["alive_at_end"] and all(c.get("alive_after", True) for c in res["cases"].values())
    res["no_reply"] = [k for k, v in res["cases"].items() if v.get("reply") in ("closed-no-reply", "timeout")]
    res["refused_by_close"] = [k for k, v in res["cases"].items() if v.get("reply") in ("closed-while-sending", "reset-by-peer-after-close")]
    res["answered_not_error"] = [k for k, v in res["cases"].items() if v.get("reply") == "answer"]
    res["errors"] = [k for k, v in res["cases"].items() if v.get("reply") == "error"]
    print(json.dumps(res, indent=1))

main()
