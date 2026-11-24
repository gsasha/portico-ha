"""Tests for scripts/e4_preflight.py (DEV-518 W6). Pure: the script opens no socket and the tests bind nothing."""
import importlib.util
import io
import json
import re
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location("e4_preflight", ROOT / "scripts" / "e4_preflight.py")
pf = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pf)

GOOD = {"sip_host": "10.6.0.5", "sip_port": 5060, "rtp_port": 45000, "allow_non_loopback": True,
        "allow_from": "10.8.0.39", "password": "abcdefgh1", "media_range": "20000-20099",
        "observer_url": "http://10.6.0.7:8091"}


def levels(cfg):
    return [lv.strip() for lv, _ in pf.check(cfg)]


def msgs(cfg, level):
    return [m for lv, m in pf.check(cfg) if lv.strip() == level]


class Rules(unittest.TestCase):
    def test_locked_set_matches_the_driver(self):
        src = (ROOT / "custom_components/portico/door/drivers/gate_sip_server.py").read_text()
        ports = re.search(r"GATE_SIP_PORTS = frozenset\(\{([^}]*)\}\)", src).group(1)
        self.assertEqual(pf.GATE_SIP_PORTS, {int(p) for p in ports.split(",")})
        self.assertEqual(pf.LOW_PORT_LIMIT, int(re.search(r"LOW_PORT_LIMIT = (\d+)", src).group(1)))
        self.assertEqual(pf.MIN_PASSWORD, int(re.search(r"MIN_PASSWORD = (\d+)", src).group(1)))
        self.assertEqual(pf.MAX_PASSWORD, int(re.search(r"MAX_PASSWORD = (\d+)", src).group(1)))

    def test_locked_ports(self):
        for p in (5060, 40000, 8091, 8092, 8095, 80, 1023):
            self.assertTrue(pf.is_locked_port(p), p)
        for p in (1024, 15060, 45000):
            self.assertFalse(pf.is_locked_port(p), p)


class Check(unittest.TestCase):
    def test_the_planned_config_has_no_fail(self):
        self.assertNotIn("FAIL", levels(GOOD))

    def test_locked_port_without_unlock_fails(self):
        cfg = dict(GOOD, allow_non_loopback=False)
        self.assertTrue(any("standard_port_locked" in m for m in msgs(cfg, "FAIL")))

    def test_locked_port_with_unlock_warns_only(self):
        self.assertTrue(any("allowed only because" in m for m in msgs(GOOD, "WARN")))

    def test_lan_bind_needs_both_flags(self):
        cfg = dict(GOOD, allow_from="")
        self.assertTrue(any("BOTH" in m for m in msgs(cfg, "FAIL")))

    def test_same_host_as_observer_is_refused(self):
        cfg = dict(GOOD, sip_host="10.6.0.7")
        self.assertTrue(any("port_used_by_gate_sip" in m for m in msgs(cfg, "FAIL")))
        cfg = dict(cfg, sip_port=15060)  # rtp 45000 too: no gate-sip port left, not refused
        self.assertFalse(any("port_used_by_gate_sip" in m for m in msgs(cfg, "FAIL")))

    def test_localhost_observer_with_loopback_bind(self):
        cfg = {"sip_host": "127.0.0.1", "sip_port": 5060, "observer_url": "http://localhost:8091", "allow_from": "10.8.0.39"}
        self.assertTrue(any("port_used_by_gate_sip" in m for m in msgs(cfg, "FAIL")))

    def test_docker_internal_ranges_fail(self):
        for host in ("172.30.32.5", "172.30.42.9"):
            self.assertTrue(any("Docker/Supervisor" in m for m in msgs(dict(GOOD, sip_host=host), "FAIL")), host)

    def test_estate_10_net_other_than_ha_warns(self):
        self.assertTrue(any("NEVER a docker" in m for m in msgs(dict(GOOD, sip_host="10.6.0.77"), "WARN")))
        self.assertFalse(any("NEVER a docker" in m for m in msgs(GOOD, "WARN")))

    def test_allow_from(self):
        self.assertTrue(any("exactly the station" in m for m in msgs(GOOD, "OK")))
        self.assertTrue(any("does not contain" in m for m in msgs(dict(GOOD, allow_from="10.8.0.40"), "FAIL")))
        self.assertTrue(any("wider" in m for m in msgs(dict(GOOD, allow_from="10.8.0.0/24"), "WARN")))
        self.assertTrue(any("/0" in m for m in msgs(dict(GOOD, allow_from="0.0.0.0/0"), "FAIL")))

    def test_password_length_only(self):
        self.assertTrue(any("password length" in m for m in msgs(dict(GOOD, password="short"), "FAIL")))
        self.assertTrue(any("password length 9" in m for m in msgs(GOOD, "OK")))
        self.assertNotIn("abcdefgh1", " ".join(m for _, m in pf.check(GOOD)))

    def test_media_range(self):
        self.assertTrue(any("media_range" in m for m in msgs(dict(GOOD, media_range="500-600"), "FAIL")))
        self.assertTrue(any("media_range" in m for m in msgs(dict(GOOD, media_range="30000-20000"), "FAIL")))
        self.assertTrue(any("under" in m for m in msgs(dict(GOOD, media_range="10000-30000"), "FAIL")))
        self.assertTrue(any("no media_range" in m for m in msgs({k: v for k, v in GOOD.items() if k != "media_range"}, "WARN")))

    def test_equal_and_clashing_ports(self):
        self.assertTrue(any("equals" in m for m in msgs(dict(GOOD, rtp_port=5060), "FAIL")))
        other = {"driver": "gate_sip_server", "sip_host": "10.6.0.5", "sip_port": 15060, "rtp_port": 45000}
        self.assertTrue(any("port_in_use_by_entry" in m for m in msgs(dict(GOOD, other_entries=[other]), "FAIL")))

    def test_loopback_default_warns_not_fails_on_lan_rules(self):
        cfg = {"sip_host": "127.0.0.1", "allow_from": ""}
        self.assertNotIn("FAIL", levels(cfg))


class Cli(unittest.TestCase):
    def run_main(self, argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = pf.main(argv)
        return rc, buf.getvalue()

    def test_good_file_exit_0_and_checklist(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d, "e.json")
            p.write_text(json.dumps(GOOD))
            rc, out = self.run_main(["--config", str(p)])
        self.assertEqual(rc, 0)
        for needle in ("MANUAL CHECKS", "tcpdump", "rollback -> 10.6.0.7", "Local SIP Server", "UniFi WRITE",
                       "SIPServer 10.8.0.39", "20000-20099"):
            self.assertIn(needle, out)
        self.assertNotIn("abcdefgh1", out)

    def test_bad_flags_exit_1(self):
        rc, _ = self.run_main(["--host", "10.6.0.5", "--sip-port", "5060", "--allow-from", "10.8.0.39"])
        self.assertEqual(rc, 1)

    def test_unusable_file_exit_2(self):
        rc, _ = self.run_main(["--config", "/nonexistent/x.json"])
        self.assertEqual(rc, 2)

    def test_no_socket_module_imported(self):
        self.assertNotIn("import socket", Path(pf.__file__).read_text())


if __name__ == "__main__":
    unittest.main()
