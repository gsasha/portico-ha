"""SIP timer scenarios of the trace-diff (DEV-764 Phase 2 item 4a). Each scenario starts ONE fresh
binary against a timestamped fake (tracelib.TStation for the dahua_sip driver, a bare UDP socket
for gate_sip_server), runs for a fixed time and returns a JSON-able trace of what the fake saw and
when. trace_diff.py compares the traces of the two binaries.

All addresses are loopback. The dahua fake binds <ip>:5060 (the dahua driver has no port setting),
so two scenarios must never use the same ip at once; trace_diff.py runs them one after the other,
except the long timer-C scenario, which takes its own ips (127.0.0.31 / 127.0.0.32).
"""
import json
import re
import signal
import socket
import time

import tracelib as T
from tracelib import S, TStation, Tap, ipc, hdr, ms

DAHUA_CFG = '{"driver":"dahua_sip","config":{"host":"%s","sip_user":"8001#1","sip_password":"pw"}}'


def start_dahua(binary, tmp, ip, name, reg_mode="ok", granted=3600, **modes):
    st = TStation(ip)
    st.reg_mode, st.granted = reg_mode, granted
    for k, v in modes.items():
        setattr(st, k, v)
    pr = S.Proc(binary, tmp, extra=("-host", "127.0.0.1"), sock_name=name + ".sock")
    pr.connect()
    tap = Tap(pr)
    return st, pr, tap


def stop(st, pr, tap):
    try:
        pr.p.send_signal(signal.SIGTERM)
        pr.wait(8)
    except Exception:
        pr.p.kill()
    tap.stop()
    st.close()


def rel(entries, t0=None):
    t0 = t0 if t0 is not None else (entries[0]["t"] if entries else 0)
    return t0


def tx_offsets(log, method, t0):
    """Group the requests of one method by CSeq: one transaction each, offsets of every copy."""
    txs, order = {}, []
    for e in log:
        if not e["line"].startswith(method + " "):
            continue
        key = (e["call_id"], e["cseq"], e["auth"])
        if key not in txs:
            txs[key] = {"call_id": e["call_id"], "cseq": e["cseq"], "auth": e["auth"], "offs": [], "raw": [], "expires": e["expires"]}
            order.append(key)
        txs[key]["offs"].append(round(ms(e["t"] - t0), 2))
        txs[key]["raw"].append(e["raw"])
    out = []
    for k in order:
        x = txs[k]
        x["identical_copies"] = len(set(x["raw"])) == 1
        del x["raw"]
        out.append(x)
    return out


# ---------------------------------------------------------------------------------------------
def register_silent(binary, tmp, secs=32.0, chaos_s=None):
    """The station never answers a REGISTER: Timer A/E retransmits, the 5 s give-up, the +-20 % jittered
    exponential retry (5 s, 10 s base), CSeq stepping, one Call-ID and From tag for all attempts."""
    st, pr, tap = start_dahua(binary, tmp, "127.0.0.9", "rs", reg_mode="silent")
    ipc(pr, id=1, method="configure", params=json.loads(DAHUA_CFG % "127.0.0.9"))
    t_cfg = time.time_ns()
    if chaos_s:  # SIGSTOP the binary for chaos_s seconds from 1.2 s on (discrimination run)
        T.chaos(pr, t_cfg + 1_200_000_000, chaos_s)
    time.sleep(secs)
    log = st.snapshot()
    t0 = log[0]["t"] if log else t_cfg
    txs = tx_offsets(log, "REGISTER", t0)
    lines = tap.snapshot()
    stop(st, pr, tap)
    post = [e for e in st.snapshot() if e["t"] > log[-1]["t"]] if log else []
    return {"tx": txs, "starts_ms": [x["offs"][0] for x in txs],
            "configure_to_first_ms": round(ms(t0 - t_cfg), 2) if log else None,
            "deregister_seen": any("Expires: 0" in e["raw"] for e in st.snapshot()),
            "same_call_id": len({x["call_id"] for x in txs}) == 1,
            "ipc_lines": [json.loads(l) for _, l in lines][:6]}


def register_cadence(binary, tmp, secs=12.0):
    """401 then 200 granting Expires: 4: the refresh interval is half the grant (2 s), each refresh
    again 401 + authenticated; SIGTERM sends the clean Expires: 0."""
    st, pr, tap = start_dahua(binary, tmp, "127.0.0.9", "rc", reg_mode="ok", granted=4)
    ipc(pr, id=1, method="configure", params=json.loads(DAHUA_CFG % "127.0.0.9"))
    time.sleep(secs)
    t_term = time.time_ns()
    pr.p.send_signal(signal.SIGTERM)
    pr.wait(8)
    time.sleep(0.3)
    log = st.snapshot()
    t0 = log[0]["t"]
    regs = [e for e in log if e["line"].startswith("REGISTER ")]
    unauth = [e for e in regs if not e["auth"] and "Expires: 0" not in e["raw"]]
    dereg = [e for e in regs if "Expires: 0" in e["raw"]]
    starts = [round(ms(e["t"] - t0), 2) for e in unauth]
    gaps = [round(b - a, 2) for a, b in zip(starts, starts[1:])]
    out = {"unauth_starts_ms": starts, "refresh_gaps_ms": gaps,
           "auth_follows_401": sum(1 for e in regs if e["auth"] and "Expires: 0" not in e["raw"]),
           "deregister_n": len(dereg),
           "deregister_after_sigterm_ms": round(ms(dereg[0]["t"] - t_term), 2) if dereg else None,
           "cseqs": [e["cseq"] for e in regs]}
    tap.stop()
    st.close()
    return out


def dial_invite(binary, tmp, mode, secs):
    """dial -> INVITE with inv_mode none (silent) or 100 (Trying only): the retransmit schedule and
    when the IPC answers."""
    st, pr, tap = start_dahua(binary, tmp, "127.0.0.9", "di", reg_mode="ok", inv_mode=mode)
    ipc(pr, id=1, method="configure", params=json.loads(DAHUA_CFG % "127.0.0.9"))
    st.wait_for(lambda e: e["auth"] and e["line"].startswith("REGISTER"), 5)
    time.sleep(0.2)
    n0 = len(st.snapshot())
    t_dial = time.time_ns()
    ipc(pr, id=10, method="dial", params={"device_id": "127.0.0.9", "host": "127.0.0.1", "port": 6000})
    time.sleep(secs)
    inv = [e for e in st.snapshot()[n0:] if e["line"].startswith("INVITE ")]
    resp = None
    for t, l in tap.snapshot():
        m = json.loads(l)
        if m.get("id") == 10:
            resp = {"at_ms": round(ms(t - t_dial), 2), "msg": m}
    out = {"invite_offsets_ms": [round(ms(e["t"] - t_dial), 2) for e in inv],
           "invite_identical_copies": len({e["raw"] for e in inv}) <= 1,
           "ipc_response": resp,
           "others_after_dial": [e["line"].split(" ")[0] for e in st.snapshot()[n0:] if not e["line"].startswith("INVITE ")]}
    stop(st, pr, tap)
    return out


def ring_invite(call_id, ip, port_rtp):
    body = "v=0\r\no=- 1 1 IN IP4 %s\r\ns=-\r\nc=IN IP4 %s\r\nt=0 0\r\nm=audio %d RTP/AVP 0\r\n" % (ip, ip, port_rtp)
    return ("INVITE sip:8001@%s SIP/2.0\r\nVia: SIP/2.0/UDP %s;branch=z9hG4bK%s\r\n"
            "From: <sip:vto@%s>;tag=a\r\nTo: <sip:8001@%s>\r\nCall-ID: %s\r\nCSeq: 1 INVITE\r\n"
            "Content-Type: application/sdp\r\nContent-Length: %d\r\n\r\n%s"
            % (ip, ip, call_id, ip, ip, call_id, len(body), body))


def bye_silent(binary, tmp, secs=8.0):
    """A ring joined by a wsmedia leg (answered at once), media_close -> the transport BYEs; the
    station never answers the BYE: Timer E retransmits and the give-up."""
    st, pr, tap = start_dahua(binary, tmp, "127.0.0.9", "by", reg_mode="ok")
    ipc(pr, id=1, method="configure", params=json.loads(DAHUA_CFG % "127.0.0.9"))
    st.wait_for(lambda e: e["auth"] and e["line"].startswith("REGISTER"), 5)
    time.sleep(0.3)
    st.send(ring_invite("ring-bye", "127.0.0.9", st.rtp.getsockname()[1]))
    st.wait_for(lambda e: False, 0.0)
    time.sleep(0.5)
    ipc(pr, id=2, method="media_open", params={"call_id": "ring-bye", "codec": "l16"})
    time.sleep(0.8)
    st.bye_mode = "silent"
    n0 = len(st.snapshot())
    t_close = time.time_ns()
    ipc(pr, id=3, method="media_close", params={"call_id": "ring-bye", "reason": "user"})
    time.sleep(secs)
    byes = [e for e in st.snapshot()[n0:] if e["line"].startswith("BYE ")]
    closed = [t for t, l in tap.snapshot() if '"media_closed"' in l]
    out = {"bye_offsets_ms": [round(ms(e["t"] - t_close), 2) for e in byes],
           "bye_identical_copies": len({e["raw"] for e in byes}) <= 1,
           "media_closed_at_ms": round(ms(closed[0] - t_close), 2) if closed else None}
    stop(st, pr, tap)
    return out


def ok200_no_ack(binary, tmp, secs=6.0):
    """The transport answers a ring; the station never ACKs: how many 200 OK copies does it send?
    (Go sends one: no Timer G.)"""
    st, pr, tap = start_dahua(binary, tmp, "127.0.0.9", "na", reg_mode="ok")
    ipc(pr, id=1, method="configure", params=json.loads(DAHUA_CFG % "127.0.0.9"))
    st.wait_for(lambda e: e["auth"] and e["line"].startswith("REGISTER"), 5)
    got = []
    # the station side of this scenario needs to see responses: a second reader on a fresh socket is
    # not possible (one port), so the TStation log is extended with responses via a hook
    st.react_orig = st.react

    def react(req, frm):
        if req.startswith("SIP/2.0 "):
            return
        st.react_orig(req, frm)

    st.react = react
    st.send(ring_invite("ring-noack", "127.0.0.9", st.rtp.getsockname()[1]))
    time.sleep(0.5)
    t0 = time.time_ns()
    ipc(pr, id=2, method="answer", params={"device_id": "127.0.0.9", "host": "127.0.0.1", "port": 5000})
    time.sleep(secs)
    ok = [e for e in st.snapshot() if e["line"].startswith("SIP/2.0 200") and "INVITE" in e["cseq"]]
    out = {"n_200_copies": len(ok), "offsets_ms": [round(ms(e["t"] - t0), 2) for e in ok],
           "n_180": len([e for e in st.snapshot() if e["line"].startswith("SIP/2.0 180")])}
    stop(st, pr, tap)
    return out


def timer_c(binary, tmp, ip, name):
    """INVITE answered 100 + 180, then silence: no retransmit, Timer C (3 min) then CANCEL; the
    CANCEL is answered with ONLY its own 200 (no 487): what does the transport send after it?
    PARITY rows 109 and 149 say Go ACKs that 200 as if it were the INVITE's and BYEs."""
    st, pr, tap = start_dahua(binary, tmp, ip, name, reg_mode="ok", inv_mode="180", cancel_mode="only200")
    ipc(pr, id=1, method="configure", params=json.loads(DAHUA_CFG % ip))
    st.wait_for(lambda e: e["auth"] and e["line"].startswith("REGISTER"), 5)
    time.sleep(0.3)
    n0 = len(st.snapshot())
    t_dial = time.time_ns()
    ipc(pr, id=10, method="dial", params={"device_id": ip, "host": "127.0.0.1", "port": 6000})
    time.sleep(186.0)
    after = st.snapshot()[n0:]
    resp = None
    for t, l in tap.snapshot():
        m = json.loads(l)
        if m.get("id") == 10:
            resp = {"at_ms": round(ms(t - t_dial), 2), "msg": m}
    out = {"events": [[round(ms(e["t"] - t_dial), 1), e["line"].split(" ")[0], e["cseq"]] for e in after],
           "ipc_response": resp}
    stop(st, pr, tap)
    return out


# ---------------------------------------------------------------------------------------------
# gate_sip_server (no fixed ports)
# ---------------------------------------------------------------------------------------------
class GssStation:
    """A bare station socket for gate_sip_server: registers, INVITEs, and logs what comes back."""

    def __init__(self, sip_port):
        self.sip = sip_port
        self.s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.s.bind(("127.0.0.1", 0))
        T.enable_ts(self.s)
        self.s.settimeout(0.1)
        self.me = "127.0.0.1:%d" % self.s.getsockname()[1]
        self.rx = []  # (t_ns, text)
        self.live = True
        self.auto = None  # callable(text) -> optional reply text
        import threading
        self.t = threading.Thread(target=self.run, daemon=True)
        self.t.start()

    def run(self):
        while self.live:
            try:
                d, _a, t = T.recv_ts(self.s)
            except OSError:
                continue
            txt = d.decode("utf-8", "replace")
            self.rx.append((t, txt))
            if self.auto:
                r = self.auto(txt)
                if r:
                    self.send(r)

    def send(self, text):
        self.s.sendto(text.encode(), ("127.0.0.1", self.sip))

    def register(self, auth=""):
        a = "Authorization: %s\r\n" % auth if auth else ""
        self.send("REGISTER sip:127.0.0.1:%d SIP/2.0\r\nVia: SIP/2.0/UDP %s;branch=z9hG4bKreg\r\n"
                  "From: <sip:8001@station>;tag=reg\r\nTo: <sip:8001@station>\r\nCall-ID: reg-1\r\n"
                  "CSeq: 1 REGISTER\r\nContact: <sip:8001@%s>\r\nExpires: 300\r\n%sContent-Length: 0\r\n\r\n"
                  % (self.sip, self.me, self.me, a))

    def wait(self, pred, timeout, start=0):
        end = time.time() + timeout
        while time.time() < end:
            for i, (t, x) in enumerate(list(self.rx)[start:], start):
                if pred(x):
                    return i, t, x
            time.sleep(0.005)
        return None

    def close(self):
        self.live = False
        self.t.join(2.0)
        self.s.close()


def gss_start(binary, tmp, name, extra_cfg=""):
    sip, rtp = S.free_udp_port(), S.free_udp_port()
    pr = S.Proc(binary, tmp, sock_name=name + ".sock")
    pr.connect()
    tap = Tap(pr)
    cfg = ('{"driver":"gate_sip_server","config":{"device_id":"gss","sip_listen":"127.0.0.1:%d","rtp_port":%d,'
           '"password":"s3cretpass","dial_enabled":true%s}}' % (sip, rtp, extra_cfg))
    ipc(pr, id=1, method="configure", params=json.loads(cfg))
    st = GssStation(sip)
    time.sleep(0.3)
    st.register()
    r = st.wait(lambda x: x.startswith("SIP/2.0 401"), 3)
    nonce = re.search(r'nonce="([^"]*)"', r[2]).group(1)
    uri = "sip:127.0.0.1"
    resp = S.md5(S.md5("8001:VDP:s3cretpass") + ":" + nonce + ":" + S.md5("REGISTER:" + uri))
    st.register('Digest username="8001", realm="VDP", nonce="%s", uri="%s", response="%s", algorithm=MD5' % (nonce, uri, resp))
    st.wait(lambda x: x.startswith("SIP/2.0 200") and "REGISTER" in x, 3)
    time.sleep(0.3)
    return pr, tap, st, sip, rtp


def gss_reply(req, code, reason, me, tag="st1", body="", extra=()):
    to = hdr(req, "To") + (";tag=" + tag if tag and ";tag=" not in hdr(req, "To") else "")
    lines = ["SIP/2.0 %d %s" % (code, reason), "Via: " + hdr(req, "Via"), "From: " + hdr(req, "From"),
             "To: " + to, "Call-ID: " + hdr(req, "Call-ID"), "CSeq: " + hdr(req, "CSeq"),
             "Contact: <sip:9901@%s>" % me, *extra]
    if body:
        lines.append("Content-Type: application/sdp")
    lines.append("Content-Length: %d" % len(body))
    return "\r\n".join(lines) + "\r\n\r\n" + body


def gss_dial_noanswer(binary, tmp, secs=26.0):
    """dial; the station is silent: the INVITE is sent ONCE (gate_sip_server has no Timer A), a
    CANCEL at the 20 s ring limit, then 2 s of waiting for the final answer, then dial_failed."""
    pr, tap, st, sip, rtp = gss_start(binary, tmp, "dn")
    n0 = len(st.rx)
    t0 = time.time_ns()
    ipc(pr, id=40, method="dial", params={"device_id": "gss"})
    time.sleep(secs)
    msgs = [[round(ms(t - t0), 1), x.split("\r\n")[0].split(" ")[0]] for t, x in st.rx[n0:]]
    resp = None
    for t, l in tap.snapshot():
        m = json.loads(l)
        if m.get("id") == 40:
            resp = {"at_ms": round(ms(t - t0), 1), "msg": m}
    out = {"station_got": msgs, "ipc_response": resp}
    st.close()
    stop_proc(pr, tap)
    return out


def stop_proc(pr, tap):
    try:
        pr.p.send_signal(signal.SIGTERM)
        pr.wait(8)
    except Exception:
        pr.p.kill()
    tap.stop()


def gss_dial_noattach(binary, tmp, secs=19.0):
    """dial answered with 200 at once; nobody joins the audio: the 15 s attach limit BYEs."""
    pr, tap, st, sip, rtp = gss_start(binary, tmp, "da")
    sdp = ("v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\ns=x\r\nc=IN IP4 127.0.0.1\r\nt=0 0\r\nm=audio %d RTP/AVP 96 8\r\n"
           "a=rtpmap:96 PCM/16000\r\na=rtpmap:8 PCMA/8000\r\n" % S.free_udp_port())

    def auto(txt):
        if txt.startswith("INVITE "):
            return gss_reply(txt, 200, "OK", st.me, body=sdp)
        if txt.startswith("BYE "):
            return gss_reply(txt, 200, "OK", st.me)
        return None

    n0 = len(st.rx)
    st.auto = auto
    t0 = time.time_ns()
    ipc(pr, id=41, method="dial", params={"device_id": "gss"})
    time.sleep(secs)
    msgs = [[round(ms(t - t0), 1), x.split("\r\n")[0].split(" ")[0]] for t, x in st.rx[n0:]]
    lines = [(round(ms(t - t0), 1), json.loads(l)) for t, l in tap.snapshot()]
    out = {"station_got": msgs,
           "ipc": [[t, m.get("method") or ("result:%s" % m.get("id")), (m.get("params") or {}).get("state")] for t, m in lines if t > 0]}
    st.close()
    stop_proc(pr, tap)
    return out


def gss_ring_limit(binary, tmp, secs=6.0):
    """An inbound INVITE nobody answers with ring_seconds=3: what the station is sent and when."""
    pr, tap, st, sip, rtp = gss_start(binary, tmp, "rl", extra_cfg=',"ring_seconds":3')
    sdp = ("v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\ns=x\r\nc=IN IP4 127.0.0.1\r\nt=0 0\r\nm=audio %d RTP/AVP 8\r\n"
           "a=rtpmap:8 PCMA/8000\r\n" % S.free_udp_port())
    n0 = len(st.rx)
    t0 = time.time_ns()
    st.send("INVITE sip:9901@127.0.0.1:%d SIP/2.0\r\nVia: SIP/2.0/UDP %s;branch=z9hG4bKinv\r\n"
            "From: <sip:8001@station>;tag=t-call-S1\r\nTo: <sip:9901@server>\r\nCall-ID: call-S1\r\n"
            "CSeq: 1 INVITE\r\nContact: <sip:8001@%s>\r\nContent-Type: application/sdp\r\nContent-Length: %d\r\n\r\n%s"
            % (sip, st.me, st.me, len(sdp), sdp))
    time.sleep(secs)
    msgs = [[round(ms(t - t0), 1), x.split("\r\n")[0]] for t, x in st.rx[n0:]]
    lines = [(round(ms(t - t0), 1), json.loads(l)) for t, l in tap.snapshot()]
    out = {"station_got": msgs,
           "ipc": [[t, m.get("method") or ("result:%s" % m.get("id")), (m.get("params") or {}).get("state")] for t, m in lines]}
    st.close()
    stop_proc(pr, tap)
    return out
