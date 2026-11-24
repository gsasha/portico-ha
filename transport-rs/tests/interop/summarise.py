#!/usr/bin/env python3
"""Prints one compact block per run directory (the result.json of harness.py): the verdict and the
numbers that INTEROP.md quotes. Usage: summarise.py RUN_DIR [RUN_DIR ...]"""
import json
import os
import sys


def main():
    for d in sys.argv[1:]:
        r = json.load(open(os.path.join(d, "result.json")))
        v = r["verdict"]
        print("== %s: %d passed, %d failed" % (r["run"], len(v["passed"]), len(v["failed"])))
        for f in v["failed"]:
            print("   FAILED:", f)
        for s in r["sessions"]:
            print("   browser %s: answer %s | in %s out %s | hears(dominant Hz) %s | max stall %s ms | pair %s" % (
                s["sid"], [c for _, c in s.get("answer_codecs", [])], s.get("codecs_in"), s.get("codecs_out"),
                s.get("dom_hz"), s.get("max_stall_ms"), (s.get("pair") or {}).get("local")))
        for st in r["stations"]:
            h = st["station_hears"]
            print("   station %s: heard %d samples, loud windows %d, dominant %s; rx %d tx %d packets; BYE seen: %s" % (
                st["sid"], h["samples"], h["loud_windows"], h["dominant_hz_hist"], st["rx_packets"], st["tx_packets"], st["bye_seen"]))
        if "station_bye_after_s" in r:
            print("   browser killed -> BYE at the station after %s s; slot freed: %s; ice after end: %s" % (
                r["station_bye_after_s"], r.get("slot_freed_second_ring_notified"), r.get("ice_after_end")))
        print("   RSS idle %s kB, configured %s kB, in call %s, end %s kB; wall %s s" % (
            r.get("rss_idle_kb"), r.get("rss_configured_kb"), r.get("rss_in_call_kb"), r.get("rss_end_kb"), r.get("wall_s")))
        for t, l in [(t, l) for t, l in r.get("transport_lines", []) if "peer connection" in l or "grace" in l or "never" in l][:12]:
            print("   transport %6.2f s: %s" % (t, l))


if __name__ == "__main__":
    main()
