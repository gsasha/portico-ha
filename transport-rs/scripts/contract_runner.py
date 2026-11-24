#!/usr/bin/env python3
"""Black-box conformance runner for the portico-transport IPC contract (DEV-764 Phase 2).

    contract_runner.py --impl rust <binary> [--groups a,b] [--skip transcripts]
    contract_runner.py --impl rust <binary> --self-test
    contract_runner.py --impl rust <binary> --record      # rewrite the Rust patches
    contract_runner.py --audit                               # no binary: check the Go/Rust patch file

It drives EITHER binary (the Go one or the Rust one) over its real IPC socket, with fake stations,
consoles and gates on loopback only (the fakes of `ipc_smoke.py`, which this file imports). Python 3
standard library only. A full run takes about 45 s.

Four layers, each case id is quoted in the output and in `contract/transport_ipc_v1_parity_map.json`:

  W-*  wire cases written in `contract/transport_ipc_v1.json` (language-neutral data: framing,
       the error answer of every request on an empty hub, the media lane);
  S-*  soft contracts, also data in the spec (flags, exit codes, socket mode, signals, the 1 MiB
       line limit, bind failure, the stale socket, the house-call log line);
  T-*  flow transcripts: the long scripted conversations of `ipc_smoke.py` (one group per driver
       or topic), compared line by line with the reference transcripts in
       `transport-rs/conformance/transcripts/` (recorded from the Go binary). The Rust binary is
       compared with the same transcripts after the documented patches of `rust_patches.json`:
       each patch is one PARITY.md difference, named in `implementation_differences` of the spec;
  C-*  capabilities per driver (the bytes `configure` answers), read out of the spec.

`--self-test` is the discrimination check: it feeds deliberately wrong expectations to the same
code and requires every one of them to FAIL. Exit status 0 only if every case passed.
"""
import argparse
import contextlib
import copy
import difflib
import hashlib
import io
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))  # the repository root
sys.path.insert(0, HERE)
import ipc_smoke as S  # noqa: E402  (the seed: fake stations and the long scripted flows)

SPEC_PATH = os.path.join(ROOT, "contract", "transport_ipc_v1.json")
TRANSCRIPTS = os.path.join(ROOT, "transport-rs", "conformance", "transcripts")
PATCHES = os.path.join(TRANSCRIPTS, "rust_patches.json")

# group name -> flow function of ipc_smoke, in the order ipc_smoke.main() runs them.
GROUPS = [
    ("main", S.main_script), ("gate_sip_server", S.gate_sip_server_flow), ("dahua", S.dahua_flow),
    ("wsmedia", S.wsmedia_flow), ("house", S.house_flow), ("standalone", S.standalone_flow),
    ("unifi", S.unifi_flow), ("talk", S.talk_flow), ("ordered", S.ordered_flow), ("webrtc", S.webrtc_flow),
    ("sigterm", S.sigterm_connected), ("sigint", S.sigint_idle), ("disconnect", S.disconnect),
    ("stale_socket", S.stale_socket), ("flags", S.flag_matrix),
]


class Results:
    def __init__(self):
        self.rows = []

    def check(self, case, ok, detail=""):
        self.rows.append((case, bool(ok), detail))
        print(("PASS  " if ok else "FAIL  ") + case + ("" if ok else "  :: " + str(detail)[:1500]), flush=True)
        return bool(ok)

    def failed(self):
        return [r for r in self.rows if not r[1]]


def load_spec(path=SPEC_PATH):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------------------------
# matching
# ---------------------------------------------------------------------------------------------
def subset(want, got):
    """want is a dict/list/scalar; every key of a dict must be present in got and match."""
    if isinstance(want, dict):
        return isinstance(got, dict) and all(k in got and subset(v, got[k]) for k, v in want.items())
    if isinstance(want, list):
        return isinstance(got, list) and len(want) == len(got) and all(subset(a, b) for a, b in zip(want, got))
    return want == got


def line_matches(want, raw, mode="exact"):
    """The reply line equals the expected object, key for key (R2-122: an extra key, such as a `result` beside the
    `error`, fails). A step that means less says `"match": "subset"` and names only the keys it cares about."""
    try:
        got = json.loads(raw)
    except ValueError:
        return False
    return subset(want, got) if mode == "subset" else want == got


def match_lines(expect, lines, ordered, mode="exact"):
    """True if the response lines satisfy the expected matchers (all of them, nothing extra)."""
    if len(expect) != len(lines):
        return False
    if ordered:
        return all(line_matches(w, l, mode) for w, l in zip(expect, lines))
    left = list(lines)
    for w in expect:
        for i, l in enumerate(left):
            if line_matches(w, l, mode):
                del left[i]
                break
        else:
            return False
    return True


# ---------------------------------------------------------------------------------------------
# W-*: wire cases against a bare process
# ---------------------------------------------------------------------------------------------
WIRE_QUIET = 0.1  # seconds of silence required after the expected lines of a wire step unless the case says otherwise


def run_wire_case(binary, tmp, case, extra=(), impl="go"):
    """One case = one fresh process, a list of sends, each with the matchers of what comes back."""
    pr = S.Proc(binary, tmp, extra=extra, sock_name="w.sock")
    try:
        pr.connect()
        for k, step in enumerate(case["steps"]):
            if "send_padded" in step:
                head, tail = '{"id":5,"method":"keepalive","params":"', '"}'
                pr.send(head + "A" * (step["send_padded"] - len(head) - len(tail)) + tail + "\n")
            else:
                pr.send(step["send"])
            expect = step.get("expect_" + impl, step.get("expect", []))  # a documented difference may override
            n = len(expect)
            got = pr.collect(n, step.get("timeout", 3.0))
            if not match_lines(expect, got, step.get("ordered", False), step.get("match", "exact")):
                return False, "step %d: expected %s, got %s" % (k, json.dumps(expect), got)
            for frag in step.get("raw_contains", []):
                if not any(frag in g for g in got):
                    return False, "step %d: raw response lacks %r: %s" % (k, frag, got)
            # every step is followed by a short quiet period, so a stray extra line is seen (R1-139)
            if step.get("quiet", WIRE_QUIET):
                extra_lines = pr.quiet(step.get("quiet", WIRE_QUIET))
                if extra_lines:
                    return False, "step %d: unexpected lines %s" % (k, extra_lines)
        return True, ""
    finally:
        try:
            pr.p.terminate()
            pr.p.wait(5)
        except Exception:
            pr.p.kill()


def error_reply_is(d, i, row):
    """The reply is exactly `{"id": i, "error": {"code", "message"}}` of the table row: no `result`, no extra field."""
    return d == {"id": i, "error": {"code": row["code"], "message": row["message"]}}


def run_error_table(binary, tmp, table):
    """Every (method, params) -> (code, message) of the empty hub, in one process; matched by id."""
    pr = S.Proc(binary, tmp, sock_name="e.sock")
    bad = []
    try:
        pr.connect()
        for method, rows in table.items():
            lines = []
            for i, row in enumerate(rows, 1):
                params = row["params"]
                lines.append('{"id":%d,"method":"%s"%s}' % (i, method, "" if params is None else ',"params":' + params))
            pr.send("\n".join(lines) + "\n")
            got = pr.collect(len(rows), 5.0)
            by_id = {}
            for g in got:
                try:
                    d = json.loads(g)
                    by_id[d.get("id")] = d
                except ValueError:
                    pass
            for i, row in enumerate(rows, 1):
                d = by_id.get(i)
                # the WHOLE reply, not two fields of it: an extra field, or a `result` next to the error, fails (R1-139)
                if not error_reply_is(d, i, row):
                    bad.append("%s %s -> %s (want %s %r)" % (method, row["params"], d, row["code"], row["message"]))
        return not bad, "; ".join(bad[:5]) + (" ..." if len(bad) > 5 else "")
    finally:
        try:
            pr.p.terminate()
            pr.p.wait(5)
        except Exception:
            pr.p.kill()


# ---------------------------------------------------------------------------------------------
# S-*: soft contracts
# ---------------------------------------------------------------------------------------------
def run_process_case(binary, tmp, case):
    """A process that is expected to exit by itself (flags, start-up errors)."""
    env = dict(os.environ)
    for k in list(env):
        if k.startswith("PORTICO_") or k.startswith("GATE_SIP_"):
            del env[k]
    env.update(case.get("env", {}))
    args = [a.replace("{TMP}", tmp).replace("{BADBYTE}", os.fsdecode(b"\xff")) for a in case["args"]]
    try:
        r = subprocess.run([binary, *args], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, env=env, timeout=10, cwd=tmp)
    except subprocess.TimeoutExpired:
        return False, "did not exit within 10 s"
    out = r.stdout.decode("utf-8", "replace").replace(tmp, "{TMP}")
    if r.returncode != case["exit"]:
        return False, "exit %d, want %d; output %r" % (r.returncode, case["exit"], out[:300])
    for want in case.get("output_contains", []):
        if want not in out:
            return False, "output lacks %r: %r" % (want, out[:300])
    if "flag_count" in case:  # the usage lists exactly this many flags (a tenth, or a missing one, fails)
        flags = re.findall(r"^  -[A-Za-z][-A-Za-z0-9]*", out, re.M)
        if len(flags) != case["flag_count"]:
            return False, "%d flags in the usage, want %d: %s" % (len(flags), case["flag_count"], flags)
    if "output_exact" in case and out != case["output_exact"]:
        return False, "output %r, want %r" % (out[:300], case["output_exact"])
    return True, ""


def run_lifecycle(binary, tmp, case):
    """socket mode, stale socket, signals, disconnect, oversize line, bind failure."""
    kind = case["kind"]
    sock = os.path.join(tmp, "l-%s.sock" % case["id"])
    if kind == "bind_failure":
        sock = os.path.join(tmp, "no-such-dir", "x.sock")
    if kind == "stale_socket":
        open(sock, "w").close()
    log = open(os.path.join(tmp, "l-%s.log" % case["id"]), "w+b")
    p = subprocess.Popen([binary, "-socket", sock], stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
    pr = S.Proc.__new__(S.Proc)
    pr.sock, pr.s, pr.buf = sock, None, b""
    try:
        if kind == "bind_failure":
            code = p.wait(10)
            log.seek(0)
            out = log.read().decode("utf-8", "replace")
            want = case["output_regex"].replace("{SOCK}", re.escape(sock))
            ok = code == case["exit"] and re.search(want, out) is not None
            return ok, "exit %s output %r" % (code, out[:300])
        pr.connect()
        probe = '{"id":1,"method":"keepalive"}\n'
        pr.send(probe)
        got = pr.collect(1)
        if not got or json.loads(got[0]).get("error", {}).get("code") != "not_implemented":
            return False, "no round trip: %s" % got
        mode = oct(os.stat(sock).st_mode & 0o777)
        if mode != "0o600":
            return False, "socket mode %s" % mode
        if kind in ("sigterm", "sigint", "stale_socket"):
            p.send_signal(signal.SIGINT if kind == "sigint" else signal.SIGTERM)
        elif kind == "disconnect":
            pr.s.close()
        elif kind == "oversize_line":
            head, tail = '{"id":5,"method":"keepalive","params":"', '"}'
            line = head + "A" * (1048576 - len(head) - len(tail)) + tail + "\n"
            try:
                pr.s.sendall(line.encode())
            except OSError:
                pass
        code = p.wait(10)
        log.seek(0)
        out = log.read().decode("utf-8", "replace")
        if code != case["exit"]:
            return False, "exit %s, want %s; output %r" % (code, case["exit"], out[:300])
        gone = not os.path.exists(sock)
        if gone != case["socket_removed"]:
            return False, "socket removed=%s, want %s" % (gone, case["socket_removed"])
        for want in case.get("output_contains", []):
            if want not in out:
                return False, "output lacks %r: %r" % (want, out[:300])
        if case.get("output_empty") and out:
            return False, "output not empty: %r" % out[:300]
        return True, ""
    except (SystemExit, Exception) as e:  # noqa: BLE001
        return False, "exception %r" % (e,)
    finally:
        if p.poll() is None:
            p.kill()
            p.wait()
        log.close()


def run_house_log(binary, tmp, case):
    """The log line Python promotes to INFO: one summary per ended house call, with the reason, the cause and the
    duration the contract writes down (`soft_contracts.log.house_call_summary`)."""
    pr = S.Proc(binary, tmp, extra=("-house-calls",), sock_name="hl.sock")
    try:
        pr.connect()
        legs = ('[{"side":"from","kind":"frames","codec":"l16"},{"side":"to","kind":"frames","codec":"pcmu"}]')
        pr.send('{"id":1,"method":"house_open","params":{"call_id":"hso-log","legs":%s}}\n' % legs)
        if len(pr.collect(1)) != 1:
            return False, "house_open got no answer"
        pr.send('{"id":2,"method":"house_close","params":{"call_id":"hso-log"}}\n')
        got = pr.collect(3, 3.0)
        if not any(json.loads(g).get("id") == 2 and "result" in json.loads(g) for g in got if g.startswith("{")):
            return False, "house_close was not answered with a result: %s" % got
        want = re.compile(r"^%s" % re.escape(case["prefix"]) + r"hso-log ended reason=\S+ cause=\S+ dur=\S+( .*)?$")
        deadline = time.time() + 5.0
        lines = []
        while time.time() < deadline:  # wait for the line instead of a fixed sleep
            out = pr.log_text()
            lines = [l for l in out.splitlines() if want.match(l)]
            if lines:
                time.sleep(0.2)  # and a moment more, so a second summary would be seen
                lines = [l for l in pr.log_text().splitlines() if want.match(l)]
                break
            time.sleep(0.05)
        return len(lines) == 1, "log %r" % pr.log_text()[:400]
    finally:
        try:
            pr.p.terminate()
            pr.p.wait(5)
        except Exception:
            pr.p.kill()


def run_bind_conflict(binary, tmp):
    """configure of a gate_sip_server whose SIP (or RTP) port is already taken: start_failed with
    Go's net text, on a loopback port this runner binds itself (never an estate address)."""
    import socket
    u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    u.bind(("127.0.0.1", 0))
    port = u.getsockname()[1]
    pr = S.Proc(binary, tmp, sock_name="bc.sock")
    try:
        pr.connect()
        cfg = '{"sip_listen":"127.0.0.1:%d","password":"s3cretpass"%s}'
        pr.send('{"id":1,"method":"configure","params":{"driver":"gate_sip_server","config":%s}}\n' % (cfg % (port, "")))
        pr.send('{"id":2,"method":"configure","params":{"driver":"gate_sip_server","config":%s}}\n' % (cfg % (0, ',"rtp_port":%d' % port)))
        got = {json.loads(l)["id"]: json.loads(l) for l in pr.collect(2, 5.0)}
        w1 = "gate_sip_server: cannot listen on 127.0.0.1:%d: listen udp 127.0.0.1:%d: bind: address already in use" % (port, port)
        w2 = "gate_sip_server: cannot listen for RTP on 127.0.0.1:%d: listen udp 127.0.0.1:%d: bind: address already in use" % (port, port)
        ok = got.get(1, {}).get("error") == {"code": "start_failed", "message": w1} and \
            got.get(2, {}).get("error") == {"code": "start_failed", "message": w2}
        return ok, "got %s" % got
    finally:
        u.close()
        try:
            pr.p.terminate()
            pr.p.wait(5)
        except Exception:
            pr.p.kill()


# ---------------------------------------------------------------------------------------------
# C-*: capabilities per driver, answered by `configure` (all five drivers, loopback fakes)
# ---------------------------------------------------------------------------------------------
def capabilities_from(group_transcript, marker_line):
    for l in group_transcript.splitlines():
        if marker_line in l and '"result":' in l and not l.lstrip().startswith("raw="):
            return json.loads(l.strip())["result"]
    return None


# ---------------------------------------------------------------------------------------------
# T-*: transcripts
# ---------------------------------------------------------------------------------------------
def run_group(binary, tmp, name, fn):
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            fn(binary, tmp)
    except SystemExit as e:
        buf.write("!! SystemExit: %s\n" % (e,))
    except Exception as e:  # noqa: BLE001
        buf.write("!! exception: %r\n" % (e,))
    return buf.getvalue()


def apply_patches(go_lines, patches):
    """Apply the Rust patch list to the Go reference lines. Each patch replaces the Go lines
    `go` found at index `at` (an insertion has an empty `go`) with the Rust lines; patches are in
    file order. A patch whose Go lines are not where it says is an error (the reference moved)."""
    out, pos = [], 0
    for p in patches:
        at, g = p["at"], p["go"]
        if at < pos or go_lines[at:at + len(g)] != g:
            raise ValueError("patch %s: Go lines not found at %d: %r" % (p["id"], at, g[:2]))
        out.extend(go_lines[pos:at])
        out.extend(p["rust"])
        pos = at + len(g)
    out.extend(go_lines[pos:])
    return out


def expected_for(impl, name):
    with open(os.path.join(TRANSCRIPTS, name + ".txt"), encoding="utf-8") as f:
        go = f.read().split("\n")
    if impl == "go":
        return "\n".join(go)
    with open(PATCHES, encoding="utf-8") as f:
        allp = json.load(f)
    return "\n".join(apply_patches(go, [p for p in allp if p["group"] == name]))


def compare(actual, expected):
    """None if equal, else a short unified diff."""
    if actual == expected:
        return None
    d = difflib.unified_diff(expected.split("\n"), actual.split("\n"), "expected", "actual", lineterm="", n=1)
    return "\n".join(list(d)[:40])


def diff_patches(go_text, rs_text, group, diffs):
    """Opcodes between the Go and Rust transcripts of a group, each tied to a documented difference."""
    a, b = go_text.split("\n"), rs_text.split("\n")
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    patches, problems = [], []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        g, r = a[i1:i2], b[j1:j2]
        ids = set()
        for l in g:
            hit = [d for d in diffs if d["group"] == group and d.get("go") and re.fullmatch(d["go"], l)]
            if not hit:
                problems.append("undocumented Go-side line in %s: %r" % (group, l[:160]))
            ids.update(d["id"] for d in hit)
        for l in r:
            hit = [d for d in diffs if d["group"] == group and d.get("rust") and re.fullmatch(d["rust"], l)]
            if not hit:
                problems.append("undocumented Rust-side line in %s: %r" % (group, l[:160]))
            ids.update(d["id"] for d in hit)
        patches.append({"id": "%s-%d" % (group, len(patches) + 1), "group": group, "differences": sorted(ids),
                        "at": i1, "go": g, "rust": r})
    return patches, problems


# ---------------------------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------------------------
def run_spec_layers(binary, tmp, spec, R, impl, only=None):
    def want(prefix):
        return only is None or prefix in only

    if want("W"):
        for case in spec["wire_cases"]:
            ok, detail = run_wire_case(binary, tmp, case, tuple(case.get("args", ())), impl)
            R.check(case["id"], ok, detail)
        ok, detail = run_error_table(binary, tmp, spec["empty_hub_errors"])
        n = sum(len(v) for v in spec["empty_hub_errors"].values())
        R.check("W-ERR-TABLE (%d request shapes over %d methods)" % (n, len(spec["empty_hub_errors"])), ok, detail)
    if want("S"):
        for case in spec["process_cases"]:
            if case.get("only_impl", impl) != impl:
                continue
            ok, detail = run_process_case(binary, tmp, case)
            R.check(case["id"], ok, detail)
        for case in spec["lifecycle_cases"]:
            ok, detail = run_lifecycle(binary, tmp, case)
            R.check(case["id"], ok, detail)
        ok, detail = run_house_log(binary, tmp, spec["soft_contracts"]["house_call_log"])
        R.check("S-LOG-HOUSE-CALL-PREFIX", ok, detail)
        ok, detail = run_bind_conflict(binary, tmp)
        R.check("S-CONFIGURE-BIND-CONFLICT", ok, detail)


def run_transcripts(binary, tmp, impl, R, groups=None):
    texts = {}
    for name, fn in GROUPS:
        if groups and name not in groups:
            continue
        texts[name] = run_group(binary, tmp, name, fn)
        diff = compare(texts[name], expected_for(impl, name))
        R.check("T-%s" % name, diff is None, "\n" + (diff or ""))
    return texts


def run_capabilities(spec, texts, R):
    """C-*: the Capabilities `configure` answered inside the flow transcripts equal the spec."""
    where = {  # driver -> (group, a line that precedes the result line)
        "gate_sip_server": ("gate_sip_server", "## configure (the reply is the Capabilities struct in field order)"),
        "dahua_sip": ("dahua", "## configure: the Capabilities struct, then driver_availability"),
        "unifi_protect": ("unifi", "## configure with ring_events: the Capabilities struct, then driver_availability"),
        "unifi_protect_talk": ("talk", "## configure: the Capabilities struct, then driver_availability"),
    }
    for drv, (group, header) in where.items():
        if group not in texts:
            print("SKIP  C-CAPS-%s (the group %s was not run)" % (drv, group), flush=True)
            continue
        lines = texts[group].split("\n")
        got = None
        for i, l in enumerate(lines):
            if l.startswith(header):
                # The reply is the first following line that carries a "result": since the transcripts keep the
                # real order (Phase R R1-44), a driver_availability event may precede the reply (Go does that).
                for cand in lines[i + 1:i + 6]:
                    try:
                        obj = json.loads(cand.strip())
                    except ValueError:
                        continue
                    if isinstance(obj, dict) and "result" in obj:
                        got = obj["result"]
                        break
                break
        want = spec["capabilities"][drv]["result"]
        R.check("C-CAPS-%s" % drv, got == want, "got %s want %s" % (got, want))


def self_test(binary, impl, spec):
    """Mutation check: every deliberately wrong expectation must be caught."""
    R = Results()
    caught = []
    with tempfile.TemporaryDirectory(prefix="contract-self-") as tmp:
        # 1. a wire case with a wrong code
        case = copy.deepcopy(spec["wire_cases"][0])
        case["steps"][0]["expect"][0] = {"error": {"code": "mutated_code"}}
        ok, _ = run_wire_case(binary, tmp, case, (), impl)
        caught.append(("wire case with a wrong error code", not ok))
        wc = {c["id"]: c for c in spec["wire_cases"]}

        def wire_fails(base, edit):
            c = copy.deepcopy(wc[base])
            edit(c)
            return not run_wire_case(binary, tmp, c, tuple(c.get("args", ())), impl)[0]
        # 1b. every checker of the wire layer is made to fail once (R2-43)
        caught.append(("wire case whose raw fragment is absent (raw_contains)",
                       wire_fails("W-FRAME-ESCAPING", lambda c: c["steps"][0].__setitem__("raw_contains", ["ABSENT-FRAGMENT"]))))
        caught.append(("wire step that expects one line fewer (the quiet period sees the extra one)",
                       wire_fails("W-FRAME-PIPELINE", lambda c: c["steps"][0].__setitem__("expect", c["steps"][0]["expect"][:1]))))
        caught.append(("wire step with its ordered replies swapped (ordered)",
                       wire_fails("W-MEDIA-OPEN-VALIDATION", lambda c: c["steps"][0]["expect"].reverse())))
        caught.append(("wire step whose reply has an extra field (exact match)",
                       wire_fails("W-FRAME-UNKNOWN-METHOD", lambda c: c["steps"][0]["expect"][0].pop("id"))))
        caught.append(("wire step with the wrong override for this implementation (expect_%s)" % impl,
                       wire_fails("W-FRAME-UNKNOWN-METHOD", lambda c: c["steps"][0].__setitem__("expect_" + impl, [{"id": 2, "error": {"code": "x", "message": "nope"}}]))))
        caught.append(("padded send whose expected id is wrong (send_padded)",
                       wire_fails("W-FRAME-LARGE-OK", lambda c: c["steps"][0]["expect"][0].__setitem__("id", 6))))
        # 2. the error table with one wrong message
        tab = copy.deepcopy(spec["empty_hub_errors"])
        tab["deconfigure"][0]["message"] = "mutated"
        ok, _ = run_error_table(binary, tmp, {"deconfigure": tab["deconfigure"][:3]})
        caught.append(("error table with a wrong message", not ok))
        # 3. a process case with the wrong exit code
        pc = copy.deepcopy(spec["process_cases"][0])
        pc["exit"] = pc["exit"] + 7
        ok, _ = run_process_case(binary, tmp, pc)
        caught.append(("process case with a wrong exit code", not ok))
        # 4. a lifecycle case that claims the socket survives
        lc = copy.deepcopy([c for c in spec["lifecycle_cases"] if c["kind"] == "sigterm"][0])
        lc["socket_removed"] = not lc["socket_removed"]
        lc["id"] += "-mut"
        ok, _ = run_lifecycle(binary, tmp, lc)
        caught.append(("lifecycle case with the wrong socket expectation", not ok))
        # 3b. the process-case checkers: output_contains, output_exact, flag_count (R2-43)
        for what, edit in (("an absent output fragment (output_contains)", lambda c: c.__setitem__("output_contains", ["ABSENT-FRAGMENT"])),
                           ("a wrong exact output (output_exact)", lambda c: c.__setitem__("output_exact", "nothing\n")),
                           ("a wrong flag count (flag_count)", lambda c: c.__setitem__("flag_count", 10))):
            base = next(c for c in spec["process_cases"] if c["id"] == ("S-FLAG-HELP" if "flag" in what or "contains" in what else "S-START-NO-SOCKET"))
            pc2 = copy.deepcopy(base)
            edit(pc2)
            ok, _ = run_process_case(binary, tmp, pc2)
            caught.append(("process case with " + what, not ok))
        # 3c. the lifecycle checkers: exit code, an absent output fragment, the bind-failure regex, a stray output
        lcs = {c["id"]: c for c in spec["lifecycle_cases"]}
        for what, base, edit in (
                ("a wrong exit code", "S-SIGINT", lambda c: c.__setitem__("exit", c["exit"] + 5)),
                ("an absent output fragment", "S-SIGINT", lambda c: c.__setitem__("output_contains", ["ABSENT-FRAGMENT"])),
                ("a wrong bind-failure regex", "S-BIND-FAILURE", lambda c: c.__setitem__("output_regex", "ABSENT-REGEX")),
                ("output_empty on a run that prints", "S-LINE-LIMIT", lambda c: c.__setitem__("output_empty", True))):
            lc2 = copy.deepcopy(lcs[base])
            edit(lc2)
            lc2["id"] += "-mut"
            ok, _ = run_lifecycle(binary, tmp, lc2)
            caught.append(("lifecycle case with " + what, not ok))
        # 3d. the house-call log check with a wrong prefix, and the capability check with a wrong capability
        ok, _ = run_house_log(binary, tmp, {"prefix": "portico-transport: no such prefix "})
        caught.append(("house-call log check with a wrong prefix", not ok))
        bad_spec = copy.deepcopy(spec)
        bad_spec["capabilities"]["dahua_sip"]["result"]["InboundAudio"] = 99
        cap_texts = {"dahua": run_group(binary, tmp, "dahua", S.dahua_flow)}
        RC = Results()
        with contextlib.redirect_stdout(io.StringIO()):
            run_capabilities(bad_spec, cap_texts, RC)
        caught.append(("capabilities check with a wrong capability", any(not ok for n, ok, _ in RC.rows if "dahua_sip" in n)))
        # 3e. the audit: a deliberately bad patch is rejected (R2-33, R2-34, R2-41)
        with open(PATCHES, encoding="utf-8") as pf:
            good = json.load(pf)

        def audit_rejects(edit):
            patches = copy.deepcopy(good)
            edit(patches)
            RA = Results()
            with contextlib.redirect_stdout(io.StringIO()):
                audit_patches(spec, patches, RA)
            return bool(RA.failed())
        caught.append(("an audit over a patch that is the standalone line with a panic and a password",
                       audit_rejects(lambda ps: ps[0]["rust"].__setitem__(0, "  process output: 'portico-transport: panic at src/x.rs:1: password=smokepass123\\n'"))))
        caught.append(("an audit over a webrtc patch with host candidates False",
                       audit_rejects(lambda ps: next(p for p in ps if p["id"] == "webrtc-5")["rust"].__setitem__(
                           0, next(p for p in ps if p["id"] == "webrtc-5")["rust"][0].replace("has host candidates: True", "has host candidates: False")))))
        caught.append(("an audit over a webrtc patch with the PCMU rtpmap dropped",
                       audit_rejects(lambda ps: next(p for p in ps if p["id"] == "webrtc-6")["rust"].__setitem__(0, "  rtpmap: ['a=rtpmap:9 G722/8000']"))))
        caught.append(("an audit over a patch whose `at` index is wrong",
                       audit_rejects(lambda ps: next(p for p in ps if p["id"] == "webrtc-5").__setitem__("at", 3))))
        # 3f. the parity-map audit: a row whose text changed late in the line, a test that does not exist, a coded
        # row with neither tests nor a reason are rejected (R2-41, R2-38)
        with open(MAP_PATH, encoding="utf-8") as mf:
            real_map = json.load(mf)
        real_files = load_parity_files()

        def map_rejects(edit_map=None, edit_files=None):
            m, fl = copy.deepcopy(real_map), copy.deepcopy(real_files)
            (edit_map or (lambda x: None))(m)
            (edit_files or (lambda x: None))(fl)
            RM = Results()
            with contextlib.redirect_stdout(io.StringIO()):
                audit_parity_map(spec, RM, m, fl)
            return bool(RM.failed())
        row315 = next(r for r in real_map["rows"] if r.get("n") == 315)
        caught.append(("a parity row whose claim was reversed after its 40th character",
                       map_rejects(edit_files=lambda fl: fl[row315["file"]].__setitem__(
                           row315["line"] - 1, fl[row315["file"]][row315["line"] - 1].replace("MaxInt64/MinInt64", "NOTHING: a range error is 0")))))
        caught.append(("a parity row that names a test that does not exist",
                       map_rejects(edit_map=lambda m: next(r for r in m["rows"] if r.get("n") == 315).__setitem__("tests", ["no_such_test_anywhere"]))))
        caught.append(("a UNIT row with neither tests nor a reason",
                       map_rejects(edit_map=lambda m: [r.pop("tests", None) for r in m["rows"] if r.get("n") == 315])))
        caught.append(("a parity row mapped to a case id that does not exist",
                       map_rejects(edit_map=lambda m: next(r for r in m["rows"] if r.get("n") == 305).__setitem__("cases", ["S-NO-SUCH-CASE"]))))
        # 3g. the audit of the tests a row names (R3B-4, R3B-7): a name that is only a `fn` or a method, a real test of
        # another subject, a wildcard that matches nothing and a `case` row with no case are rejected
        def row_n(m, n):
            return next(r for r in m["rows"] if r.get("n") == n)
        caught.append(("a parity row naming `main` (a fn, not a test)",
                       map_rejects(edit_map=lambda m: row_n(m, 315).__setitem__("tests", ["main"]))))
        caught.append(("a parity row naming a jitter test `admit` on a standalone row (a test, of another subject)",
                       map_rejects(edit_map=lambda m: row_n(m, 400).__setitem__("tests", ["admit"]))))
        caught.append(("a parity row naming a real test of another subject (a dahua supervisor test on a standalone row)",
                       map_rejects(edit_map=lambda m: row_n(m, 400).__setitem__("tests", ["the_restart_wait_starts_again_after_a_long_run"]))))
        caught.append(("a parity row mapped to a wildcard case id that matches nothing",
                       map_rejects(edit_map=lambda m: row_n(m, 305).__setitem__("cases", ["W-NO-SUCH-*"]))))
        caught.append(("a `case` row with an empty case list",
                       map_rejects(edit_map=lambda m: row_n(m, 305).__setitem__("cases", []))))
        # 3h. the provenance audit (R3B-1): a commit that is not in the history is rejected, one that is, passes
        RP = Results()
        with contextlib.redirect_stdout(io.StringIO()):
            audit_provenance(RP, is_ancestor=lambda c: c == "aaaaaaa")
        RQ = Results()
        with contextlib.redirect_stdout(io.StringIO()):
            audit_provenance(RQ, is_ancestor=lambda c: True)
        caught.append(("an audit over results built from a commit that is not an ancestor of HEAD", bool(RP.failed()) and not RQ.failed()))
        # 3i. the test-table audit (R2-161): a wrong count and a wrong total are rejected
        real_counts = test_attr_counts()
        RT = Results()
        with contextlib.redirect_stdout(io.StringIO()):
            audit_test_table(RT, text="runs **7 tests**\n| `lib` | 3 |\n", counts={"lib": 4})
        caught.append(("a test table with a wrong count and a wrong total", len(RT.failed()) == 2))
        # 4b. the error comparison is whole-reply: an extra field or a `result` beside the error is caught (R1-139)
        row = {"code": "c", "message": "m"}
        good = {"id": 1, "error": {"code": "c", "message": "m"}}
        caught.append(("error reply with an extra field", error_reply_is(good, 1, row) and not error_reply_is({**good, "result": None}, 1, row)
                       and not error_reply_is({"id": 1, "error": {"code": "c", "message": "m", "x": 1}}, 1, row)))
        # 5. the transcript comparison: one changed byte in a long transcript, and a dropped line
        exp = expected_for(impl, "main")
        mutated = exp.replace('"code":"unknown_method"', '"code":"unknown_methoX"', 1)
        caught.append(("transcript with one changed byte", compare(mutated, exp) is not None))
        lines = exp.split("\n")
        del lines[40]
        caught.append(("transcript with a dropped line", compare("\n".join(lines), exp) is not None))
        # 6. a transcript group run against the wrong implementation's patch set
        other = "rust" if impl == "go" else "go"
        wrong = expected_for(other, "webrtc")
        right = expected_for(impl, "webrtc")
        caught.append(("Go and Rust webrtc transcripts differ (the patch set is not empty)", wrong != right))
        # 7. the actual group run against a mutated expectation
        text = run_group(binary, tmp, "sigint", S.sigint_idle)
        bad = text.replace("exit code: 0", "exit code: 3")
        caught.append(("a live group against a wrong exit code", compare(text, bad) is not None and compare(text, expected_for(impl, "sigint")) is None))
        # 8. the floating-reply steps (PARITY 360 to 362): the notifications are pinned, only the reply moves
        def render(lines, after):
            n = len(S.OUT)
            with contextlib.redirect_stdout(io.StringIO()):
                S.show("t", lines, len(lines), reply_after=after)
            text = "\n".join(S.OUT[n:])
            del S.OUT[n:]
            return text
        reply = '{"id":3,"result":{"ok":true}}'
        n1, n2 = '{"method":"a","params":{}}', '{"method":"b","params":{}}'
        want = render([n1, n2, reply], 0)
        caught.append(("a floating step with its notifications swapped", render([n2, n1, reply], 0) != want))
        caught.append(("a floating step with a notification missing", render([n1, reply], 0) != want))
        caught.append(("a floating step with a second reply", render([n1, n2, reply, reply], 0) != want))
        caught.append(("a floating step whose reply came before a pinned notification", "!!" in render([reply, n1, n2], 1)))
        caught.append(("a floating step whose notification is not the pinned one", render([n1, reply, '{"method":"c","params":{}}'], 0) != want))
        R.check("SELF the reply of a floating step may sit anywhere after its pinned notifications",
                render([reply, n1, n2], 0) == want == render([n1, reply, n2], 0) == render([n1, n2, reply], 0)
                and render([n1, reply, n2], 1) == render([n1, n2, reply], 1) and "!!" not in render([n1, n2, reply], 2))
    for what, ok in caught:
        R.check("MUTATION %s is rejected" % what, ok)
    return R


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("binary", nargs="?")
    ap.add_argument("--impl", choices=["rust"], default="rust",
                    help="the only implementation left (the Go binary can no longer be built); the Go transcripts are the frozen reference")
    ap.add_argument("--groups", help="comma list of transcript groups (default all)")
    ap.add_argument("--layers", default="W,S,T,C", help="comma list of layers to run")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--record", action="store_true", help="rewrite the Rust patches against the frozen Go reference transcripts")
    ap.add_argument("--audit", action="store_true", help="check the patch file against the documented differences (no binary)")
    ap.add_argument("--rehash-map", action="store_true", help="refresh the row digests of the parity map after a reviewed edit of a row (no binary)")
    a = ap.parse_args()
    spec = load_spec()
    R = Results()
    if a.audit:
        return audit(spec, R)
    if a.rehash_map:
        return rehash_map(spec)
    if not a.binary:
        ap.error("a binary is required")
    binary = os.path.abspath(a.binary)
    layers = set(a.layers.split(","))
    if a.self_test:
        R = self_test(binary, a.impl, spec)
        print("\n%d checks, %d failed" % (len(R.rows), len(R.failed())))
        return 1 if R.failed() else 0
    with tempfile.TemporaryDirectory(prefix="contract-") as tmp:
        if a.record:
            return record(binary, tmp, a.impl, spec)
        run_spec_layers(binary, tmp, spec, R, a.impl, only=layers)
        texts = {}
        if "T" in layers or "C" in layers:
            texts = run_transcripts(binary, tmp, a.impl, R, a.groups.split(",") if a.groups else None)
        if "C" in layers:
            run_capabilities(spec, texts, R)
    print("\n%d checks, %d failed" % (len(R.rows), len(R.failed())))
    return 1 if R.failed() else 0


def record(binary, tmp, impl, spec):
    os.makedirs(TRANSCRIPTS, exist_ok=True)
    texts = {name: run_group(binary, tmp, name, fn) for name, fn in GROUPS}
    patches, problems = [], []
    for name, t in texts.items():
        with open(os.path.join(TRANSCRIPTS, name + ".txt"), encoding="utf-8") as f:
            go = f.read()
        p, pr = diff_patches(go, t, name, spec["implementation_differences"])
        patches += p
        problems += pr
    for pr in problems:
        print("PROBLEM", pr)
    if problems:
        print("not written: the Rust transcript differs from Go's in ways no documented difference covers")
        return 1
    with open(PATCHES, "w", encoding="utf-8") as f:
        json.dump(patches, f, indent=1, ensure_ascii=False)
        f.write("\n")
    print("recorded %d Rust patches" % len(patches))
    return 0


def audit_patches(spec, patches, R, go_dir=None):
    """Every patch maps to documented differences, every line of it matches a whole-line pattern of the
    difference, every patch still applies to its Go transcript (a stale `at` is an error), and every
    documented difference is used by a patch or is a wire case."""
    used = set()
    by_group = {}
    for p in patches:
        by_group.setdefault(p["group"], []).append(p)
        diffs = [d for d in spec["implementation_differences"] if d["id"] in p["differences"]]
        ok = bool(diffs) and all(d["parity"] for d in diffs)
        for l in p["go"]:
            ok = ok and any(d.get("go") and re.fullmatch(d["go"], l) and d["group"] == p["group"] for d in diffs)
        for l in p["rust"]:
            ok = ok and any(d.get("rust") and re.fullmatch(d["rust"], l) and d["group"] == p["group"] for d in diffs)
        used.update(p["differences"])
        R.check("AUDIT patch %s -> %s" % (p["id"], ",".join(p["differences"])), ok)
    for group, ps in sorted(by_group.items()):
        try:
            with open(os.path.join(go_dir or TRANSCRIPTS, group + ".txt"), encoding="utf-8") as f:
                apply_patches(f.read().split("\n"), ps)
            R.check("AUDIT the patches of %s apply to the Go transcript" % group, True)
        except (ValueError, OSError) as e:
            R.check("AUDIT the patches of %s apply to the Go transcript" % group, False, str(e))
    known, names = known_case_ids(spec), rust_test_names()
    for d in spec["implementation_differences"]:
        R.check("AUDIT difference %s is used by a patch or is a wire case" % d["id"],
                d["id"] in used or d.get("not_in_transcripts"), d["id"])
        if d.get("not_in_transcripts"):
            refs = [d[k] for k in ("wire_case", "process_case", "deferred_case") if d.get(k)]
            bad = [r for r in refs if r not in known] + [t for t in d.get("tests", []) if t not in names]
            R.check("AUDIT difference %s names a case or a test, and it exists" % d["id"],
                    (refs or d.get("tests")) and not bad, bad or "no wire_case, process_case, deferred_case or tests")


def audit_provenance(R, path=None, is_ancestor=None):
    """The commits the evidence names must be in the history of HEAD (R3B-1): a results file that names a
    commit of a branch that was rebased away is evidence about code that is not in the tree."""
    path = path or os.path.join(ROOT, "transport-rs", "conformance", "deferred_wire_results.json")
    if is_ancestor is None:
        def is_ancestor(c):
            r = subprocess.run(["git", "-C", ROOT, "merge-base", "--is-ancestor", c, "HEAD"], capture_output=True)
            return r.returncode == 0
    with open(path, encoding="utf-8") as f:
        prov = json.load(f)["provenance"]
    bad = [(k, prov[k].get("commit")) for k in ("go", "rust")
           if not re.fullmatch(r"[0-9a-f]{7,40}", prov[k].get("commit") or "") or not is_ancestor(prov[k]["commit"])]
    R.check("PROVENANCE the commits of deferred_wire_results.json are ancestors of HEAD", not bad, bad)


_TEST_ATTR_COUNT_RE = re.compile(r"^\s*#\[\s*(?:tokio::test|test)\b", re.M)


def test_attr_counts():
    """binary name -> number of `#[test]` / `#[tokio::test..]` attributes: tests/<name>.rs by file, `lib` for
    src/ (src/main.rs is `main`). An ignored test counts (`cargo test` lists it in `running N tests`)."""
    base = os.path.join(ROOT, "transport-rs")
    counts = {}
    for n in os.listdir(os.path.join(base, "tests")):
        if n.endswith(".rs"):
            with open(os.path.join(base, "tests", n), encoding="utf-8", errors="replace") as f:
                counts[n[:-3]] = len(_TEST_ATTR_COUNT_RE.findall(f.read()))
    for dp, _, fs in os.walk(os.path.join(base, "src")):
        for n in fs:
            if n.endswith(".rs"):
                key = "main" if os.path.join(dp, n) == os.path.join(base, "src", "main.rs") else "lib"
                with open(os.path.join(dp, n), encoding="utf-8", errors="replace") as f:
                    counts[key] = counts.get(key, 0) + len(_TEST_ATTR_COUNT_RE.findall(f.read()))
    return counts


def audit_test_table(R, text=None, counts=None):
    """docs/tests_and_vectors.md lists every test binary with its count, and the total it states is their sum
    (R2-161, R3B-5: the table went stale after two rebases)."""
    if text is None:
        with open(os.path.join(ROOT, "transport-rs", "docs", "tests_and_vectors.md"), encoding="utf-8") as f:
            text = f.read()
    counts = counts if counts is not None else test_attr_counts()
    table = {m.group(1): int(m.group(2)) for m in re.finditer(r"^\| `(\w+)` \| (\d+) \|", text, re.M)}
    diff = sorted(k for k in set(table) | {k for k, v in counts.items() if v} if table.get(k) != counts.get(k))
    R.check("DOCS the test table of tests_and_vectors.md equals the test attributes per binary (%d binaries)" % len(table),
            not diff, ["%s: table %s, tree %s" % (k, table.get(k), counts.get(k)) for k in diff])
    m = re.search(r"runs \*\*(\d+) tests\*\*", text)
    R.check("DOCS the total of tests_and_vectors.md is the sum of its table", bool(m) and int(m.group(1)) == sum(table.values()),
            "states %s, table sums to %d" % (m.group(1) if m else None, sum(table.values())))


def audit(spec, R):
    with open(PATCHES, encoding="utf-8") as f:
        patches = json.load(f)
    audit_patches(spec, patches, R)
    audit_parity_map(spec, R)
    audit_doc_paths(R)
    audit_provenance(R)
    audit_test_table(R)
    print("\n%d checks, %d failed" % (len(R.rows), len(R.failed())))
    return 1 if R.failed() else 0


def audit_doc_paths(R):
    """Every `src/...rs` or `tests/...rs` path the documents name exists (a split or a rename that leaves a
    document citing the old file is a failure; R2-160). A `:line` suffix is ignored."""
    base = os.path.join(ROOT, "transport-rs")
    docs = [os.path.join(base, n) for n in ("README.md", "DESIGN.md", "PARITY.md", "INTEROP.md")]
    for dp, _, fs in os.walk(os.path.join(base, "docs")):
        docs += [os.path.join(dp, n) for n in fs if n.endswith(".md")]
    missing = []
    for d in docs:
        if not os.path.exists(d):
            continue
        with open(d, encoding="utf-8") as f:
            text = f.read()
        for m in re.finditer(r"(?<![\w./-])((?:src|tests)/[\w/.-]*?\.rs)\b", text):
            if not os.path.exists(os.path.join(base, m.group(1))):
                missing.append("%s: %s" % (os.path.relpath(d, base), m.group(1)))
    R.check("DOCS every src/ or tests/ .rs path a document names exists (%d documents)" % len(docs), not missing, sorted(set(missing))[:20])


MAP_PATH = os.path.join(ROOT, "contract", "transport_ipc_v1_parity_map.json")
TESTED_CODES = ("UNIT", "PANIC", "API", "TASK")  # a row with one of these codes names the tests that pin it
TESTS_FROM_ROW = 300  # the round-1 and round-2 rows; the older rows have `tests` only where someone added them


def row_digest(text):
    """The whole row, whitespace-normalised, hashed: a change anywhere in it makes the map stale."""
    return hashlib.sha1(re.sub(r"\s+", " ", text).strip().encode("utf-8")).hexdigest()[:12]


def load_parity_files():
    pdir = os.path.join(ROOT, "transport-rs", "docs", "parity")
    files = {}
    for name in sorted(os.listdir(pdir)):
        if name.endswith(".md"):
            with open(os.path.join(pdir, name), encoding="utf-8") as f:
                files[name[:-3]] = f.read().split("\n")
    return files


_FN_RE = re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?fn\s+(\w+)")
_TEST_ATTR_RE = re.compile(r"^\s*#\[\s*(?:tokio::test|test|rstest|test_case)\b")


def _norm(text):
    return text.lower().replace("_", "").replace("-", "")


def rust_test_names():
    """name -> [(path, normalised text)] of every REAL test (R3B-4): a Rust `fn` that follows a
    `#[test]` / `#[tokio::test..]` attribute (other attributes and comments may sit between), or a Python
    `def test_*` of scripts/. A helper, a method or a `fn main` of the same name is
    not a test: the first version of this audit took any `fn` or `def` name, and four rows named
    `configure` and `admit`."""
    tests = {}
    base = os.path.join(ROOT, "transport-rs")
    for sub in ("src", "tests", "scripts"):
        for dp, _, fs in os.walk(os.path.join(base, sub)):
            for fn in fs:
                if not fn.endswith((".rs", ".py")):
                    continue
                with open(os.path.join(dp, fn), encoding="utf-8", errors="replace") as f:
                    text = f.read()
                where = (os.path.relpath(os.path.join(dp, fn), base), _norm(text))
                names = set()
                if fn.endswith(".py"):
                    names.update(re.findall(r"\bdef\s+(test_\w+)", text))
                else:
                    pending = False
                    for line in text.split("\n"):
                        if _TEST_ATTR_RE.match(line):
                            pending = True
                            continue
                        m = _FN_RE.match(line)
                        if m:
                            if pending:
                                names.add(m.group(1))
                            pending = False
                        elif pending and line.strip() and not line.lstrip().startswith(("#[", "//", "///")):
                            pending = False
                for n in names:
                    tests.setdefault(n, []).append(where)
    return tests


def test_names_subject(tests, name, subject, row_text=""):
    """True when `name` is a real test in a file whose path or text mentions the row's subject (the parity
    file name, `unifi_protect`: as a whole or by one of its parts of 3 or more letters), or whose path or
    module path (`gostd::url`) the row's own text names (a test of a shared module)."""
    parts = {_norm(subject)} | {p for p in re.split(r"[_-]", subject.lower()) if len(p) >= 3}
    for path, text in tests.get(name, []):
        mod = re.sub(r"(?:/mod)?\.rs$", "", path)
        mod = "::".join(mod.split("/")[1:]) if path.startswith("src/") else None
        if path in row_text or (mod and mod in row_text):
            return True
        if any(p in path.lower().replace("_", "") or p in text for p in parts):
            return True
    return False


# The `D-*` case ids of the retired wire_deferred.py (DEV-831). The script drove both binaries, and its Go
# half can no longer run (the Go tree is gone); the ids stay because the PARITY map and the contract name them
# and the recorded results are in conformance/deferred_wire_results.json (history, provenance-audited).
DEFERRED_CASE_IDS = frozenset({
    "D-AUTH-WINDOW-CLOSE",
    "D-BYE-FOREIGN",
    "D-BYE-FOREIGN-DIALLED",
    "D-CANCEL-FOREIGN",
    "D-DIAL-BAD-SDP",
    "D-DIAL-RESENT-200",
    "D-HTTP-FRAMING",
    "D-HTTP-REQUEST-HEAD",
    "D-ICMP-DEAF",
    "D-IPC-WRITE-TIMEOUT",
    "D-MEDIA-PORT-OVERFLOW",
    "D-NONCE-REPLAY",
    "D-OFFER-LIMITS",
    "D-REGISTER-EXPIRES0",
    "D-SHUTDOWN-BYE",
    "D-TIMER-C-CANCEL-*",
    "D-TIMER-C-CANCEL-BYE",
    "D-TIMER-C-CANCEL-CANCEL",
    "D-TIMER-C-CANCEL-NONE",
})


def deferred_case_ids():
    return set(DEFERRED_CASE_IDS)


def known_case_ids(spec):
    known = {c["id"] for k in ("wire_cases", "process_cases", "lifecycle_cases") for c in spec[k]}
    known |= {"W-ERR-TABLE", "S-LOG-HOUSE-CALL-PREFIX", "S-CONFIGURE-BIND-CONFLICT"}
    known |= {"T-" + n for n, _ in GROUPS} | {"C-CAPS-" + d for d in spec["capabilities"]}
    return known | deferred_case_ids()


def rehash_map(spec):
    """Rewrite the digest of every mapped row from the subject files (after a reviewed edit of a row)."""
    with open(MAP_PATH, encoding="utf-8") as f:
        pm = json.load(f)
    files = load_parity_files()
    n = 0
    for r in pm["rows"]:
        lines = files.get(r.get("file"))
        if lines is None or not 1 <= r["line"] <= len(lines):
            continue
        d = row_digest(lines[r["line"] - 1])
        if r.get("digest") != d:
            r["digest"] = d
            n += 1
    with open(MAP_PATH, "w", encoding="utf-8") as f:
        f.write(json.dumps(pm, indent=1, ensure_ascii=False))
    print("rehashed %d of %d rows" % (n, len(pm["rows"])))
    return 0


def audit_parity_map(spec, R, pm=None, files=None):
    """The PARITY map still describes the subject files of docs/parity/, every numbered row is in it,
    every case id exists, every row's whole text still hashes to what the map recorded, every row coded
    UNIT / PANIC / API / TASK names tests that exist. (transport-rs/PARITY.md is only the index.)"""
    if pm is None:
        with open(MAP_PATH, encoding="utf-8") as f:
            pm = json.load(f)
    files = files if files is not None else load_parity_files()
    known = known_case_ids(spec)
    numbered = {(fn, i) for fn, lines in files.items() for i, l in enumerate(lines, 1)
                if re.match(r"^\| *\d+ *\|", l) or re.match(r"^\d+\. ", l) or re.match(r"^\* \*\*\d+ ", l)}
    mapped = {(r.get("file"), r["line"]) for r in pm["rows"]}
    R.check("MAP every numbered row of docs/parity (%d) is mapped" % len(numbered), numbered <= mapped,
            "unmapped %s" % sorted(numbered - mapped))

    def row_line(r):
        lines = files.get(r.get("file"))
        if lines is None or not 1 <= r["line"] <= len(lines):
            return None
        return lines[r["line"] - 1]

    stale = [(r.get("file"), r["line"]) for r in pm["rows"]
             if row_line(r) is None or row_digest(row_line(r)) != r.get("digest")]
    R.check("MAP the mapped rows still read as the subject files say (whole row, digest)", not stale,
            "stale rows %s (check the row, then `contract_runner.py --rehash-map`)" % stale)
    badcase = sorted({c for r in pm["rows"] for c in r.get("cases", []) if c not in known and not c.endswith("*")})
    R.check("MAP every case id exists in the runner", not badcase, badcase)
    noreason = [r["line"] for r in pm["rows"] if r["kind"] != "case" and r.get("reason_code") not in pm["reason_codes"]]
    R.check("MAP every row without a full case has a defined reason", not noreason, noreason)
    names = rust_test_names()
    need, missing, notest = 0, [], []
    for r in pm["rows"]:
        coded = r["kind"] != "case" and r.get("reason_code") in TESTED_CODES
        tests = r.get("tests", [])
        if coded and (r.get("n") or 0) >= TESTS_FROM_ROW:
            need += 1
            if not tests and not r.get("no_test"):
                notest.append(r["n"])
        missing += ["%s:%s" % (r.get("n"), t) for t in tests
                    if not test_names_subject(names, t, r.get("file", ""), row_line(r) or "")]
    R.check("MAP every UNIT / PANIC / API / TASK row of %d and up (%d) names its tests or says why there is none"
            % (TESTS_FROM_ROW, need), not notest, notest)
    R.check("MAP every test a row names is a real test (`#[test]`, `#[tokio::test]`, `def test_*`) in a file that mentions the row's subject",
            not missing, missing)
    nocase = [r["line"] for r in pm["rows"] if r["kind"] == "case" and not r.get("cases")]
    R.check("MAP every row of kind `case` names at least one case", not nocase, nocase)
    nomatch = sorted({c for r in pm["rows"] for c in r.get("cases", [])
                      if c.endswith("*") and not any(k.startswith(c[:-1]) for k in known)})
    R.check("MAP every wildcard case id matches at least one case", not nomatch, nomatch)
    cov = {}
    for r in pm["rows"]:
        cov[r["kind"]] = cov.get(r["kind"], 0) + 1
    print("  map: %s of %d rows (%d numbered)" % (cov, len(pm["rows"]), len(numbered)))


if __name__ == "__main__":
    sys.exit(main())
