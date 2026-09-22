#! /usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Self-contained tests for crm_check, run against real cluster XML fixtures
# (captured from Pacemaker 3.0 on Debian 13) plus in-memory fault mutations.
#
#   python3 tests/test_crm_check.py
#
# Exits 0 if all pass, 1 otherwise. No third-party test runner needed.

import contextlib
import io
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
from monitoring_plugins_crm import crm_check  # noqa: E402

OK, WARNING, CRITICAL, UNKNOWN = 0, 1, 2, 3
FIX = os.path.join(HERE, "fixtures")
PROMOTABLE = open(os.path.join(FIX, "healthy_promotable.xml"), "rb").read()
PLAIN = open(os.path.join(FIX, "healthy_plain_clone.xml"), "rb").read()
REAL_QDEVICE_RUNNER = crm_check.run_qdevice_tool

results = []


class CommandResult:
    returncode = 0
    stdout = "State:\t\t\tConnected\n"
    stderr = ""


def run(name, xml_bytes, argv, expect_state, expect_substr=None,
        qdevice_configured=False, qdevice_output=None, qdevice_error=None):
    crm_check.run_crm_mon = lambda: (xml_bytes, None)
    crm_check.qdevice_configured = lambda: (qdevice_configured, None)
    crm_check.run_qdevice_tool = lambda: (qdevice_output, qdevice_error)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        state = crm_check.check(argv)
    out = buf.getvalue().strip()
    ok = state == expect_state and (expect_substr is None or expect_substr in out)
    results.append(ok)
    print("[%s] %-34s -> %-8s (want %-8s) %s" % (
        "PASS" if ok else "FAIL", name,
        crm_check.STATE_NAME[state], crm_check.STATE_NAME[expect_state], out))


run("healthy promotable", PROMOTABLE, [], OK, "promotables 2 ok")
run("healthy plain clone", PLAIN, [], OK, "promotables 0 ok")
run("healthy qdevice", PROMOTABLE, [], OK, "qdevice connected",
    qdevice_configured=True,
    qdevice_output="State:\t\t\tConnected\n")
run("disconnected qdevice", PROMOTABLE, [], CRITICAL,
    "qdevice state=Disconnected", qdevice_configured=True,
    qdevice_output="State:\t\t\tDisconnected\n")
run("unavailable qdevice status", PROMOTABLE, [], CRITICAL,
    "cannot read qdevice status", qdevice_configured=True,
    qdevice_error="permission denied")

# Verify that qdevice status runs directly as the monitoring user.
qdevice_command = []
original_subprocess_run = crm_check.subprocess.run
crm_check.subprocess.run = lambda argv, **kwargs: (
    qdevice_command.extend(argv) or CommandResult())
status, error = REAL_QDEVICE_RUNNER()
crm_check.subprocess.run = original_subprocess_run
exact_qdevice_command = [
    crm_check.QDEVICE_TOOL, "-s"]
qdevice_command_ok = (
    qdevice_command == exact_qdevice_command
    and status == CommandResult.stdout
    and error is None)
results.append(qdevice_command_ok)
print("[%s] %-34s -> %s" % (
    "PASS" if qdevice_command_ok else "FAIL", "direct qdevice status command",
    " ".join(qdevice_command)))
run("no quorum",
    PROMOTABLE.replace(b'with_quorum="true"', b'with_quorum="false"'),
    [], CRITICAL, "quorum")
run("node offline",
    PROMOTABLE.replace(b'name="web2.weba.be" id="2" online="true"',
                       b'name="web2.weba.be" id="2" online="false"'),
    [], CRITICAL, "OFFLINE")
run("node standby",
    PROMOTABLE.replace(b'online="true" standby="false" standby_onfail="false" maintenance="false"',
                       b'online="true" standby="true" standby_onfail="false" maintenance="false"'),
    [], WARNING, "standby")
run("resource failed",
    PROMOTABLE.replace(b'id="turbostack-mysql-vip" resource_agent="ocf:heartbeat:IPaddr2" role="Started" active="true" orphaned="false" blocked="false" maintenance="false" managed="true" failed="false"',
                       b'id="turbostack-mysql-vip" resource_agent="ocf:heartbeat:IPaddr2" role="Started" active="true" orphaned="false" blocked="false" maintenance="false" managed="true" failed="true"'),
    [], CRITICAL, "FAILED")
run("promotable missing master",
    PROMOTABLE.replace(b'role="Promoted"', b'role="Unpromoted"', 1),
    [], CRITICAL, "promoted (expected 1)")
run("resource unmanaged",
    PROMOTABLE.replace(b'id="turbostack-mysql-vip" resource_agent="ocf:heartbeat:IPaddr2" role="Started" active="true" orphaned="false" blocked="false" maintenance="false" managed="true"',
                       b'id="turbostack-mysql-vip" resource_agent="ocf:heartbeat:IPaddr2" role="Started" active="true" orphaned="false" blocked="false" maintenance="false" managed="false"'),
    [], WARNING, "unmanaged")
run("cluster maintenance-mode",
    PROMOTABLE.replace(b'maintenance-mode="false"', b'maintenance-mode="true"'),
    [], WARNING, "maintenance-mode")
run("pacemakerd stopped",
    PROMOTABLE.replace(b'pacemakerd-state="running"', b'pacemakerd-state="shutting_down"'),
    [], CRITICAL, "pacemakerd")

# crm_mon unavailable -> UNKNOWN
crm_check.run_crm_mon = lambda: (None, "/usr/sbin/crm_mon not found")
crm_check.qdevice_configured = lambda: (False, None)
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    st = crm_check.check([])
results.append(st == UNKNOWN)
print("[%s] %-34s -> %-8s (want %-8s) %s" % (
    "PASS" if st == UNKNOWN else "FAIL", "crm_mon missing",
    crm_check.STATE_NAME[st], "UNKNOWN", buf.getvalue().strip()))

print("\n%d/%d passed" % (sum(results), len(results)))
sys.exit(0 if all(results) else 1)
