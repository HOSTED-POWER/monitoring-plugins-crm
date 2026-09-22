# monitoring-plugins-crm

Python Nagios/Icinga-compatible **Pacemaker / Corosync** cluster health check.

Forked from [mgrzybek/monitoring-plugins-crm](https://github.com/mgrzybek/monitoring-plugins-crm)
and rewritten to:

* use the stable `crm_mon --output-as=xml` interface (falls back to the legacy
  `--as-xml`), so it is robust across Pacemaker 2.0 / 2.1 / 3.x
  (Debian 11 / 12 / 13) where the plain-text `crm_mon` output changes between
  releases;
* drop the unmaintained `pynagios` dependency — **standard library only**;
* add checks the original (and the common text-parsing plugins) lack:
  **corosync quorum**, a configured **qdevice connection**, and the
  **promoted-instance count of promotable clones**.

## What it checks

| Area | CRITICAL | WARNING |
|------|----------|---------|
| Cluster | pacemakerd not running, no DC, **no quorum** | whole-cluster maintenance-mode |
| Qdevice | configured qdevice missing, unreadable or disconnected | — |
| Nodes | offline, unclean | standby, maintenance, pending, clean shutdown |
| Resources | failed, blocked | orphaned, unmanaged |
| Promotable clones (`multi_state="true"`) | promoted count ≠ expected (default 1) | — |

The promotable-clone check is the key one for active/passive resources such as
redis, mysql or elasticsearch managed by Pacemaker: it alerts when a clone has
no master (0 promoted) or a split (>1), which a per-node service check cannot see.

## Running as the monitoring user

The plugin calls `crm_mon` directly as the unprivileged monitoring user. Add
that user (e.g. `nagios`) to the `haclient` group, which Pacemaker uses for CIB
read access:

```
usermod -aG haclient nagios
# restart the monitoring agent so it picks up the new group
systemctl restart icinga2
```

When a qdevice is configured, its local control socket remains root-only. The
qdevice tool supports both status and control operations, so the plugin uses
non-interactive sudo for the exact read-only status command. Grant only this
command and argument:

```
nagios ALL=(root) NOPASSWD: /usr/sbin/corosync-qdevice-tool -s
```

Sudo's argument matching prevents the plugin user from running the tool's
shutdown action or other command variants. Clusters without a `device {`
declaration in `corosync.conf` do not invoke sudo or the qdevice tool.

## Usage

```
# full check (default: nodes + resources + quorum/qdevice + promotables + perfdata)
/usr/lib/nagios/plugins/check_cluster

# scope it
/usr/lib/nagios/plugins/check_cluster --nodes=yes --resources=yes --quorum=yes --promotables=yes

# a clone that should have 2 masters
/usr/lib/nagios/plugins/check_cluster --promoted-expected=2
```

Options: `--nodes`, `--resources`, `--quorum`, `--promotables` (each `yes`/`no`,
default `yes`), `--promoted-expected N` (default `1`), `--perfdata` (`yes`/`no`,
default `yes`), `--help`.

Perfdata: `nodes_online`, `nodes_offline`, `resources_ok`, `resources_failed`,
`resources_blocked`, `promotables_ok`, and `qdevice_connected` when a qdevice
is configured.

Example output:

```
CLUSTER OK - nodes 2 up/0 down; resources 9 ok/0 failed/0 blocked; promotables 2 ok; qdevice connected | nodes_offline=0 nodes_online=2 promotables_ok=2 qdevice_connected=1 resources_blocked=0 resources_failed=0 resources_ok=9
CLUSTER CRITICAL - promotable turbostack-redis-cache-clone has 0 promoted (expected 1) || nodes 2 up/0 down; ... 
```

Exit codes: `0` OK, `1` WARNING, `2` CRITICAL, `3` UNKNOWN.

## Deployment

The plugin is a single self-contained file. Either `pip install .` (installs
`bin/check_cluster` + the package), or simply drop
`monitoring_plugins_crm/crm_check.py` in place as the plugin:

```
install -m 0755 monitoring_plugins_crm/crm_check.py /usr/lib/nagios/plugins/check_cluster
```
