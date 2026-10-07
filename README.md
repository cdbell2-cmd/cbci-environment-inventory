# Environment Inventory (standalone)

Live, point-in-time inventory of a CloudBees CI environment — one orchestrator that
connects to the **Operations Center (CJOC)**, discovers every connected controller, and
collects the three objectives from the CIBC Success Path Proposal into a single
timestamped folder.

This folder is **self-contained**: it bundles everything needed to run the collection.
`generate_environment_inventory.py` imports two helper modules that sit **beside it in
this same folder** — it does not reach back into the `plugin-utilities` toolkit:

- `generate_plugin_health_report.py` — per-instance plugin *health* CSV + usage download.
- `get_affected_jobs.py` — job-invocation counting used for the org-wide ranked rollup.

## Files in this folder

| File | Purpose |
|------|---------|
| `generate_environment_inventory.py` | **Primary script** — run this. |
| `generate_plugin_health_report.py`  | Reused dependency (plugin health report). |
| `get_affected_jobs.py`              | Reused dependency (affected-jobs counting). |
| `requirements.txt`                  | Python dependencies. |
| `output/`                           | Timestamped inventory runs are written here. |

## What it collects

| Objective | Data points | Source |
|-----------|-------------|--------|
| 1. Resource allocation | name, URL, version, CPU, memory, HA/replicas, owning team, static (non-k8s) agent specs | Groovy on the OC (provisioning) + Groovy on each controller (version, agents) |
| 2. Job ecosystem | job type (incl. **Declarative vs Scripted** pipelines), SCM repo, trigger(s), builds/week, per-controller type counts | Groovy on each controller |
| 3. Plugin usage & health | name, version, enabled/disabled per instance; org-wide ranked rollup; version drift | Groovy (list) + reused `generate_plugin_health_report.py` (health CSV) |

## How it works

All live data is gathered by POSTing Groovy to each instance's `/scriptText` API
endpoint. This requires an **admin** token (Overall/Administer). If the script console is
disabled or the token lacks rights on a given controller, that controller is recorded in
`inventory-errors.csv` and the run continues.

## Requirements

1. `JENKINS_USER_ID` and `JENKINS_API_TOKEN` exported in your environment — one admin
   account assumed to authenticate to the OC and all controllers via SSO.
2. Python >= 3.x with the dependencies in `requirements.txt`:

   ```bash
   pip install -r requirements.txt
   ```

## Usage

The engagement is a two-step flow: **(1)** collect the inventory, then **(2)** build the
client-friendly Excel report pack from it.

```bash
# Step 1 — collect the inventory (writes CSV/JSON under output/<stamp>_environment-inventory/):
python generate_environment_inventory.py --oc-url https://cjoc.example.com

# Step 2 — build the Excel workbooks from the newest run (writes to <run>/reports/):
python build_excel_reports.py
```

More collection options:

```bash
# From this directory:
python generate_environment_inventory.py --oc-url https://cjoc.example.com

# Only certain controllers (substring match on name):
python generate_environment_inventory.py --oc-url https://cjoc.example.com --controllers team-a,team-b

# Skip sections / tune the build-frequency window:
python generate_environment_inventory.py --oc-url https://cjoc.example.com \
    --skip-resources --build-frequency-days 14

python generate_environment_inventory.py -h   # full help
```

The script can be launched from any working directory — it `chdir`s into its own folder,
so `output/` always lands here regardless of where you invoke it.

## Client-friendly Excel reports

After a run, turn the CSV/JSON output into clean, labeled Excel workbooks for the client:

```bash
pip install openpyxl   # (already in requirements.txt)
python build_excel_reports.py                 # newest run under ./output
python build_excel_reports.py --run-dir output/<stamp>_environment-inventory
```

Writes to `<run>/reports/`, organized around the three objectives:

- `00_Environment_Inventory_Summary.xlsx` — a **Read Me** guide sheet plus environment-wide
  rollups: Controllers (Obj 1), Job Ecosystem (Obj 2), Plugins Ranked / Flagged / Version Drift
  (Obj 3), and any collection Errors.
- `operations-center.xlsx` — the CJOC: version, connected-controller manifest, plugins + health.
- `controller__<name>.xlsx` — one per controller: Overview, Resources & Agents (Obj 1),
  Job Types + Jobs (Obj 2, with Freestyle rows highlighted as Pipeline-coaching targets),
  Plugins + Plugin Health (Obj 3).

Each sheet has a bold title, a frozen filterable header row, and auto-sized columns.

### Key flags

- `--controllers a,b` — only process controllers whose name contains one of these substrings.
- `--skip-plugins` / `--skip-jobs` / `--skip-resources` — skip a collection phase.
- `--build-frequency-days N` — window for average builds/week (default 30).
- `--team-pattern REGEX` — regex (one capture group) applied to the controller's OC path to
  derive the owning team. By default the leading OC folder segment is used, else `unknown-team`.
- `--job-usage-threshold`, `--ignore-list`, `--no-calculate-obsolete`,
  `--target-controller-type` — passed through to the reused plugin health report.

## Output layout

```
output/<YYYYMMDD-HHMMSS>_environment-inventory/
  operations-center/
    oc-info.json                    # name, url, version
    oc-plugins.csv                  # name, version, enabled/disabled
    oc-plugin-health-report.csv     # reused health report (if OC exposes pluginUsage)
    controllers-discovered.csv      # discovery manifest: name, url, class, state, team, cpu, mem, HA
  controllers/<team>__<controller>/
    resources.json                  # Objective 1 (name,url,version,cpu,memory,disk,HA,replicas,team)
    static-agents.csv               # Objective 1 (non-Kubernetes/static agents only)
    jobs.csv                        # Objective 2 (type, SCM repo, trigger, builds/week)
    job-type-summary.csv            # Objective 2
    scm-probe.json                  # diagnostic: only written if a container had no extractable SCM
    plugins.csv                     # Objective 3 (enabled/disabled)
    plugin-usage.json               # raw pluginUsage download
    plugin-health-report.csv        # Objective 3 (reused health report)
  org-wide/
    plugin-usage-ranked.csv         # ranked by job invocations; '# Controllers' + 'On OC' columns
    flagged-plugins.csv             # deprecated/unsupported/CVE/low-health plugins, ranked by usage
    plugin-version-drift.csv        # plugins installed at differing versions across controllers
    controller-resource-summary.csv # Objective 1 rollup
    job-ecosystem-summary.csv       # Objective 2 rollup
  inventory-errors.csv              # per-instance failures (instance, phase, url, error)
  run-metadata.json                 # args, timestamps, counts, duration
```

## Notes & limitations

- `/scriptText` requires Overall/Administer. Instances where it is blocked are surfaced in
  `inventory-errors.csv` rather than aborting the run.
- CPU/memory/HA come from the OC's Managed Controller provisioning objects. Field names vary
  by CloudBees version; anything genuinely unavailable is written as `N/A`.
- **HA status:** read from the controller's replication config. A managed controller with no
  replication configured is reported as `ha: false, replicas: 1` with `ha_inferred: true`, and a
  `_ha_probe` field in `resources.json` lists the accessors (and values) that were inspected, so
  the result is always verifiable. A real HA controller (replicas > 1) reports `ha: true` directly.
- **SCM:** extracted from Git remotes, provider sources (`owner/repo` + server URL), and a generic
  URL-getter sweep. If a Multibranch/Org-Folder has no configured source, `N/A` is correct and
  `scm-probe.json` records the (empty or unrecognized) source objects for confirmation.
- **Pipeline style:** `WorkflowJob`s are reported as `Declarative` or `Scripted` where it can be
  determined — inline CPS scripts are parsed for a top-level `pipeline { }` block; Jenkinsfile-from-SCM
  and multibranch branches are classified from the declarative execution marker on recent builds.
  Jobs that have never built and are defined from SCM may stay the generic `Pipeline` until first run.
- **Validation:** `sample-data/` contains a seeder that populates a controller with representative
  Git-backed jobs, triggers, pipeline styles, team folders, and a static agent, so the collector's
  output can be verified before running against the real environment. See `sample-data/README.md`.
- Build frequency is a windowed estimate over the last `--build-frequency-days` days.
- The OC (or a controller) may not expose `/pluginUsage/download` — e.g. the CloudBees Plugin
  Usage Analyzer plugin is not installed. That step is guarded and recorded in
  `inventory-errors.csv`, not fatal. The org-wide **`# Controllers`** install count and
  **`Versions Seen`** still include such instances, because they are accumulated from the
  always-available Groovy plugin list; only **`# Job Invocations`** (usage-derived) is 0 for an
  instance whose usage download failed.
- Trigger names are reported by their trigger type (e.g. `SCMTrigger`, `TimerTrigger`), and
  `MatrixConfiguration` axis children are excluded from the job list/counts (the parent `Matrix`
  job is reported).
- Read-only: this tool only reads from the live environment and writes locally under `output/`.
