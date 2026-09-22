#! /usr/bin/env python3
# -*- coding: utf-8 -*-
#
# check_cluster - Pacemaker / Corosync health check for Nagios / Icinga.
#
# Reads the structured output of `crm_mon --output-as=xml` (falling back to the
# legacy `--as-xml` on old Pacemaker) and reports on:
#
#   * corosync quorum + DC election + pacemakerd state
#   * configured qdevice connectivity (including a lost external voter)
#   * node health   : offline / unclean       -> CRITICAL
#                     standby / maintenance / pending / shutdown -> WARNING
#   * resource health: failed / blocked       -> CRITICAL
#                      unmanaged / orphaned    -> WARNING
#   * promotable clones (multi_state="true"): each must have exactly the
#     expected number of Promoted (master) instances -> CRITICAL otherwise.
#     This is the redis / mysql / elasticsearch promotion-health signal that
#     the per-node service checks cannot see.
#
# Exit codes: 0 OK, 1 WARNING, 2 CRITICAL, 3 UNKNOWN.
#
# Runs crm_mon unprivileged and uses one narrowly authorized sudo command for
# qdevice status only; see README.md.
#
# Origin: forked from mgrzybek/monitoring-plugins-crm (GPLv3). Rewritten to use
# the stable XML interface across Pacemaker 2.0/2.1/3.x (Debian 11/12/13), to
# drop the unmaintained pynagios dependency, and to add the quorum and
# promoted-count checks. Licensed GPLv3+.

import argparse
import re
import subprocess
import sys
import xml.etree.ElementTree as ET

OK, WARNING, CRITICAL, UNKNOWN = 0, 1, 2, 3
STATE_NAME = {OK: "OK", WARNING: "WARNING", CRITICAL: "CRITICAL", UNKNOWN: "UNKNOWN"}

CRM_MON = "/usr/sbin/crm_mon"
COROSYNC_CONF = "/etc/corosync/corosync.conf"
QDEVICE_TOOL = "/usr/sbin/corosync-qdevice-tool"
SUDO = "/usr/bin/sudo"
# Pacemaker >= 2.1 reports "Promoted"/"Unpromoted"; 2.0 reported "Master"/"Slave".
PROMOTED_ROLES = ("Promoted", "Master")


def run_crm_mon():
    """Return (xml_bytes, error_string).

    Calls crm_mon directly. The monitoring user must be allowed to read the
    cluster state - add it to the ``haclient`` group (preferred) or wrap the
    plugin in sudo at the check-command level. The plugin does not escalate
    privileges itself. Prefers the current --output-as=xml flag and falls back
    to the legacy --as-xml, so it works across Pacemaker 2.0/2.1/3.x."""
    errors = []
    for flag in ("--output-as=xml", "--as-xml"):
        try:
            proc = subprocess.run([CRM_MON, flag],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except FileNotFoundError:
            return None, "%s not found" % CRM_MON
        out = proc.stdout or b""
        # crm_mon may return non-zero in some cluster states while still
        # emitting a valid document, so trust the payload, not the exit code.
        if out.strip().startswith(b"<"):
            return out, None
        errors.append("%s %s: rc=%d %s" % (
            CRM_MON, flag, proc.returncode,
            (proc.stderr or b"").decode("utf-8", "replace").strip()))
    return None, "; ".join(errors)


def qdevice_configured():
    """Return (configured, error_string) from the local Corosync config."""
    try:
        with open(COROSYNC_CONF, "r", encoding="utf-8") as config_file:
            config = config_file.read()
    except FileNotFoundError:
        return False, None
    except OSError as error:
        return None, "%s: %s" % (COROSYNC_CONF, error)

    # `device {` is the Corosync quorum-device declaration. Node and logging
    # blocks use different names, so anchoring it to a configuration line
    # avoids matching comments or option values.
    return bool(re.search(r"(?m)^\s*device\s*\{", config)), None


def run_qdevice_tool():
    """Return (status_text, error_string) for the configured qdevice."""
    try:
        proc = subprocess.run(
            [SUDO, "-n", QDEVICE_TOOL, "-s"], stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True)
    except FileNotFoundError:
        return None, "%s not found" % SUDO
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "no output").strip()
        return None, "%s rc=%d %s" % (QDEVICE_TOOL, proc.returncode, detail)
    return proc.stdout, None


def iter_resources(element):
    """Yield every <resource> element (recursing into clones/groups/bundles)."""
    for child in element:
        if child.tag == "resource":
            yield child
        elif child.tag in ("clone", "group", "bundle", "replica"):
            for sub in iter_resources(child):
                yield sub


def check(argv=None):
    ap = argparse.ArgumentParser(
        description="Pacemaker/Corosync cluster health check (crm_mon XML).")
    yn = ("yes", "no")
    ap.add_argument("--nodes", choices=yn, default="yes",
                    help="check node health (default yes)")
    ap.add_argument("--resources", choices=yn, default="yes",
                    help="check resource health (default yes)")
    ap.add_argument("--quorum", choices=yn, default="yes",
                    help="check corosync quorum / DC (default yes)")
    ap.add_argument("--promotables", choices=yn, default="yes",
                    help="check promoted-instance count of promotable clones "
                         "(default yes)")
    ap.add_argument("--promoted-expected", type=int, default=1, metavar="N",
                    help="expected Promoted instances per promotable clone "
                         "(default 1)")
    ap.add_argument("--perfdata", choices=yn, default="yes",
                    help="append perfdata (default yes)")
    args = ap.parse_args(argv)

    out, err = run_crm_mon()
    if out is None:
        return _emit(UNKNOWN, [err], [], {}, args)
    try:
        root = ET.fromstring(out)
    except ET.ParseError as e:
        return _emit(UNKNOWN, ["cannot parse crm_mon XML: %s" % e], [], {}, args)

    crits, warns = [], []
    perf = {}

    summary = root.find("summary")
    cluster_maint = False
    qdevice_is_configured = False
    qdevice_connected = False
    if summary is not None:
        stack = summary.find("stack")
        if stack is not None and stack.get("pacemakerd-state", "running") != "running":
            crits.append("pacemakerd state=%s" % stack.get("pacemakerd-state"))

        copts = summary.find("cluster_options")
        if copts is not None and copts.get("maintenance-mode") == "true":
            cluster_maint = True
            warns.append("cluster in maintenance-mode")

        if args.quorum == "yes":
            dc = summary.find("current_dc")
            if dc is None or dc.get("present") != "true":
                crits.append("no DC elected")
            elif dc.get("with_quorum") != "true":
                crits.append("cluster does NOT have quorum")

            qdevice_is_configured, qdevice_config_error = qdevice_configured()
            if qdevice_config_error:
                crits.append("cannot inspect qdevice configuration: %s"
                             % qdevice_config_error)
            elif qdevice_is_configured:
                qdevice_status, qdevice_error = run_qdevice_tool()
                if qdevice_error:
                    crits.append("cannot read qdevice status: %s" % qdevice_error)
                else:
                    state_match = re.search(
                        r"(?m)^State:\s*(\S+)\s*$", qdevice_status or "")
                    qdevice_state = state_match.group(1) if state_match else "unknown"
                    qdevice_connected = qdevice_state == "Connected"
                    if not qdevice_connected:
                        crits.append("qdevice state=%s" % qdevice_state)
                perf["qdevice_connected"] = int(qdevice_connected)

    # ---- nodes ----
    n_online = n_offline = n_standby = n_maint = 0
    if args.nodes == "yes":
        nodes_el = root.find("nodes")
        for node in (nodes_el.findall("node") if nodes_el is not None else []):
            name = node.get("name")
            online = node.get("online") == "true"
            if online:
                n_online += 1
            else:
                n_offline += 1
            if node.get("unclean") == "true":
                crits.append("node %s UNCLEAN" % name)
            elif not online:
                if node.get("shutdown") == "true":
                    warns.append("node %s offline (shutdown)" % name)
                else:
                    crits.append("node %s OFFLINE" % name)
            if node.get("standby") == "true":
                n_standby += 1
                warns.append("node %s standby" % name)
            if node.get("maintenance") == "true":
                n_maint += 1
                warns.append("node %s maintenance" % name)
            if node.get("pending") == "true":
                warns.append("node %s pending" % name)
        perf["nodes_online"] = n_online
        perf["nodes_offline"] = n_offline

    # ---- resources ----
    r_ok = r_failed = r_blocked = 0
    if args.resources == "yes":
        resources_el = root.find("resources")
        for r in (iter_resources(resources_el) if resources_el is not None else []):
            rid = r.get("id")
            if r.get("failed") == "true" and r.get("failure_ignored") != "true":
                r_failed += 1
                crits.append("resource %s FAILED" % rid)
            elif r.get("blocked") == "true":
                r_blocked += 1
                crits.append("resource %s BLOCKED" % rid)
            elif r.get("orphaned") == "true":
                warns.append("resource %s orphaned" % rid)
            elif r.get("managed") == "false" and not cluster_maint:
                warns.append("resource %s unmanaged" % rid)
            else:
                r_ok += 1
        perf["resources_ok"] = r_ok
        perf["resources_failed"] = r_failed
        perf["resources_blocked"] = r_blocked

    # ---- promotable clones ----
    promotable_ok = 0
    if args.promotables == "yes":
        resources_el = root.find("resources")
        for clone in (resources_el.iter("clone") if resources_el is not None else []):
            if clone.get("multi_state") != "true":
                continue
            cid = clone.get("id")
            promoted = sum(
                1 for r in clone.findall("resource")
                if r.get("role") in PROMOTED_ROLES and r.get("active") == "true")
            if promoted != args.promoted_expected:
                crits.append("promotable %s has %d promoted (expected %d)"
                             % (cid, promoted, args.promoted_expected))
            else:
                promotable_ok += 1
        perf["promotables_ok"] = promotable_ok

    if args.nodes == "no" and args.resources == "no" \
            and args.quorum == "no" and args.promotables == "no":
        return _emit(UNKNOWN, ["nothing to check (all checks disabled)"], [], {}, args)

    counts = []
    if args.nodes == "yes":
        counts.append("nodes %d up/%d down" % (n_online, n_offline))
    if args.resources == "yes":
        counts.append("resources %d ok/%d failed/%d blocked"
                      % (r_ok, r_failed, r_blocked))
    if args.promotables == "yes":
        counts.append("promotables %d ok" % promotable_ok)
    if args.quorum == "yes" and qdevice_is_configured:
        counts.append("qdevice %s" % (
            "connected" if qdevice_connected else "disconnected"))

    state = CRITICAL if crits else (WARNING if warns else OK)
    return _emit(state, crits, warns, perf, args, counts)


def _emit(state, crits, warns, perf, args, counts=None):
    problems = crits + warns
    detail = "; ".join(problems) if problems else "; ".join(counts or []) or "all healthy"
    if counts and problems:
        detail += " || " + "; ".join(counts)
    line = "CLUSTER %s - %s" % (STATE_NAME[state], detail)
    if args.perfdata == "yes" and perf:
        line += " | " + " ".join("%s=%d" % (k, v) for k, v in sorted(perf.items()))
    print(line)
    return state


if __name__ == "__main__":
    sys.exit(check())
