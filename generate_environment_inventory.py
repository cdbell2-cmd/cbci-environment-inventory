#!/usr/bin/env python

# WINDOWS USERS #
# You will probably have to change that top line to #!python

# pip install requests tqdm pyyaml   (same deps as the rest of the toolkit)

"""
Live CloudBees CI environment inventory orchestrator.

Connects to a live CloudBees Operations Center (CJOC), discovers every connected
Managed/Client Controller, and gathers a point-in-time inventory for three objectives
(from the CIBC Success Path Proposal):

  1. Controller resource allocation (name, url, version, cpu, memory, HA, team, static agents)
  2. Job ecosystem (job type, SCM repo, trigger, build frequency, per-controller type counts)
  3. Plugin usage & health (per controller + OC, with an org-wide ranked rollup)

Collection is done by executing Groovy via each instance's /scriptText API endpoint
(requires an admin token). The per-instance plugin *health* CSV reuses the existing
generate_plugin_health_report.py so we don't duplicate that logic.

Output is a single timestamped folder containing one sub-folder for the Operations
Center, one sub-folder per controller, and an org-wide rollup folder.

Auth: set JENKINS_USER_ID and JENKINS_API_TOKEN in your environment (one admin
account assumed to authenticate to the OC and all controllers via SSO).
"""

import argparse
import csv
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

import requests

# --------------------------------------------------------------------------------------
# Reuse the bundled plugin scripts. These modules live ALONGSIDE this script in the same
# folder (generate_plugin_health_report.py, get_affected_jobs.py). They run
# Path('output').mkdir() at import time, so we chdir into THIS folder first so any stray
# 'output/' they create lands inside this folder (not the caller's working directory).
# --------------------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent
os.chdir(SCRIPT_DIR)
sys.path.insert(0, str(SCRIPT_DIR))

try:
    import generate_plugin_health_report as phr
    import get_affected_jobs as gaj
except Exception as e:  # pragma: no cover - surfaced immediately to the user
    print(f"ERROR: unable to import the bundled plugin scripts from {SCRIPT_DIR}\n{e}")
    sys.exit(1)

# --------------------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------------------
JSON_START = "===JSON-START==="
JSON_END = "===JSON-END==="
HTTP_TIMEOUT = 60  # seconds per request
RETRYABLE_STATUS = {500, 502, 503, 504}

# ManagedMaster/ClientMaster both extend ConnectedMaster in the Operations Center model.
GROOVY_DISCOVER = r"""
import groovy.json.JsonOutput

def invoke0 = { obj, name ->
    try {
        def mth = obj.getClass().getMethods().find { it.getName() == name && it.getParameterTypes().length == 0 }
        if (mth != null) { return mth.invoke(obj) }
    } catch (ex) { }
    return null
}

// Read replica count + HA flag out of a Replication/HA config object.
def scanReplication = { obj, res ->
    if (obj == null) { return }
    ['getReplicas', 'getReplicaCount', 'getCount', 'getNumberOfReplicas',
     'getDesiredReplicas', 'getInstances', 'getScale', 'getSize'].each { g ->
        if (res.replicas == null) { def v = invoke0(obj, g); if (v instanceof Number) { res.replicas = v } }
    }
    ['isEnabled', 'isActivated', 'isActive', 'isHighAvailability', 'isHaEnabled'].each { g ->
        if (res.ha == null) { def v = invoke0(obj, g); if (v instanceof Boolean) { res.ha = v } }
    }
    // Probe child getters WITH their simple values, for precise tuning.
    obj.getClass().getMethods().each { m ->
        if (m.getParameterTypes().length == 0 && (m.getName().startsWith('get') || m.getName().startsWith('is'))) {
            def val = null
            try { def r = m.invoke(obj); if (r instanceof Number || r instanceof Boolean || r instanceof CharSequence) { val = r.toString() } } catch (ex) { }
            res.probe << (obj.getClass().getSimpleName() + '.' + m.getName() + (val != null ? ('=' + val) : ''))
        }
    }
}

// Pull resource fields from one object into res (only filling blanks).
def scanInto = { obj, res ->
    if (obj == null) { return }
    obj.getClass().getMethods().each { m ->
        if (m.getParameterTypes().length != 0) { return }
        def n = m.getName().toLowerCase()
        try {
            if (n == 'getmemory' && res.memoryMB == null) { res.memoryMB = m.invoke(obj) }
            else if ((n == 'getcpus' || n == 'getcpu') && res.cpus == null) { res.cpus = m.invoke(obj) }
            else if ((n == 'getdisk' || n == 'getdiskspace') && res.diskGB == null) { res.diskGB = m.invoke(obj) }
            else if ((n == 'getnumberofreplicas' || n == 'getreplicas' || n == 'getinstances' || n == 'getdesiredreplicas' || n == 'getscale') && res.replicas == null) { def v = m.invoke(obj); if (v instanceof Number) { res.replicas = v } }
            else if ((n == 'ishighavailability' || n == 'gethighavailability' || n == 'isha' || n == 'ishaenabled' || n == 'ishighlyavailable') && res.ha == null) { res.ha = m.invoke(obj) }
        } catch (ex) { }
    }
    // Explicitly capture HA/replication accessors (number, boolean, object, or null) so
    // the probe shows exactly what the product exposes on this version.
    ['getReplication', 'getReplicas', 'getReplicaConfiguration', 'getReplicationConfiguration',
     'getHighAvailability', 'getHa', 'getClustering'].each { g ->
        def mth = obj.getClass().getMethods().find { it.getName() == g && it.getParameterTypes().length == 0 }
        if (mth == null) { return }
        def v = null
        try { v = mth.invoke(obj) } catch (ex) { }
        res.probe << (obj.getClass().getSimpleName() + '.' + g + ' -> ' + (v == null ? 'null' : v.getClass().getSimpleName() + '=' + v.toString()))
        if (v instanceof Number && res.replicas == null) { res.replicas = v }
        else if (v instanceof Boolean && res.ha == null) { res.ha = v }
        else if (v != null && !(v instanceof CharSequence)) { scanReplication(v, res) }
    }
}

def collectCandidates = { cm ->
    def objs = []
    ['getProvisioning', 'getConfiguration', 'getClusterEndpoint', 'getSpec', 'getProperties', 'getGlobalConfiguration'].each { name ->
        try {
            def mth = cm.getClass().getMethods().find { it.getName() == name && it.getParameterTypes().length == 0 }
            if (mth != null) { def o = mth.invoke(cm); if (o != null) { objs << o } }
        } catch (ex) { }
    }
    return objs
}

def scanResource = { cm ->
    def res = [memoryMB: null, cpus: null, diskGB: null, replicas: null, ha: null,
               provisioningClass: null, probe: ([] as Set)]
    def cands = collectCandidates(cm)
    if (!cands.isEmpty()) { res.provisioningClass = cands[0].getClass().getName() }
    cands.each { scanInto(it, res) }
    scanInto(cm, res)   // the ManagedMaster itself may expose HA/replica
    // CloudBees HA (active-active) == more than one replica. Infer if not explicitly flagged.
    if (res.ha == null && res.replicas instanceof Number) { res.ha = (res.replicas > 1) }
    // Fallback: a managed controller with no replication config is single-replica / non-HA.
    res.haInferred = false
    if (res.ha == null && res.replicas == null) { res.ha = false; res.replicas = 1; res.haInferred = true }
    res.probe = (res.probe as List)
    return res
}

def out = [controllers: []]
def all = []
try { all = Jenkins.instance.getAllItems(com.cloudbees.opscenter.server.model.ConnectedMaster) }
catch (ex) {
    // Fall back to any item that looks like a connected master
    all = Jenkins.instance.getAllItems().findAll { it.getClass().getName().toLowerCase().contains('master') }
}

all.each { cm ->
    def rec = [:]
    try { rec.name = cm.getDisplayName() } catch (ex) { rec.name = null }
    try { rec.fullName = cm.getFullName() } catch (ex) { rec.fullName = rec.name }
    rec.className = cm.getClass().getName()
    def url = null
    ['getEndpointUrl', 'getEndpointURL', 'getUrl'].each { getter ->
        if (url == null) {
            try {
                def mth = cm.getClass().getMethods().find { it.getName() == getter && it.getParameterTypes().length == 0 }
                if (mth != null) { url = mth.invoke(cm) }
            } catch (ex) { }
        }
    }
    rec.url = url
    try { rec.state = cm.getState()?.toString() } catch (ex) { rec.state = null }
    try { rec.online = cm.isOnline() } catch (ex) { rec.online = null }
    try { rec.approved = cm.isApproved() } catch (ex) { rec.approved = null }
    rec.provisioning = scanResource(cm)
    out.controllers << rec
}

println "===JSON-START==="
println JsonOutput.toJson(out)
println "===JSON-END==="
"""

# Runs on a controller: version, jobs, static agents, plugins — one round trip.
GROOVY_CONTROLLER = r"""
import groovy.json.JsonOutput
import hudson.model.Job

def windowDays = __WINDOW_DAYS__
def cutoff = System.currentTimeMillis() - (windowDays * 86400000L)

def typeOf = { item ->
    def c = item.getClass().getName()
    if (c.contains('WorkflowMultiBranchProject')) { return 'Multibranch' }
    if (c.contains('OrganizationFolder')) { return 'OrgFolder' }
    if (c.contains('MatrixProject')) { return 'Matrix' }
    if (c.contains('WorkflowJob')) { return 'Pipeline' }
    if (c.contains('FreeStyleProject')) { return 'Freestyle' }
    if (c.contains('MavenModuleSet')) { return 'Maven' }
    if (c.contains('Folder')) { return 'Folder' }
    return c.tokenize('.').last()
}

def invoke0 = { obj, name ->
    try {
        def mth = obj.getClass().getMethods().find { it.getName() == name && it.getParameterTypes().length == 0 }
        if (mth != null) { return mth.invoke(obj) }
    } catch (ex) { }
    return null
}

def addUrlsFrom = { scm, sink ->
    if (scm == null) { return }
    // GitSCM-style: userRemoteConfigs -> url
    try {
        if (scm.metaClass.respondsTo(scm, 'getUserRemoteConfigs')) {
            scm.getUserRemoteConfigs().each { urc ->
                try { if (urc.getUrl()) { sink << urc.getUrl() } } catch (ex) { }
            }
        }
    } catch (ex) { }
    // Direct remote URL (GitSCMSource.getRemote(), generic getUrl/getRemoteBase)
    ['getRemote', 'getUrl', 'getRemoteBase'].each { getter ->
        def v = invoke0(scm, getter)
        if (v) { sink << v.toString() }
    }
    // Provider sources (GitHub/Bitbucket/GitLab): repoOwner + repository [+ serverUrl]
    def owner = invoke0(scm, 'getRepoOwner')
    def repo = invoke0(scm, 'getRepository')
    if (owner && repo) {
        def server = invoke0(scm, 'getServerUrl') ?: invoke0(scm, 'getApiUri')
        def base = server ? server.toString().replaceAll('/+$', '') : ''
        sink << (base ? "${base}/${owner}/${repo}" : "${owner}/${repo}")
    }
    // Generic sweep: any zero-arg getter whose name looks like a repo/remote URL.
    if (sink.isEmpty()) {
        scm.getClass().getMethods().each { m ->
            if (m.getParameterTypes().length != 0) { return }
            def n = m.getName().toLowerCase()
            if (n =~ /url|remote|repo|clone|origin|scmuri/ && !n.contains('absolute')) {
                try {
                    def v = m.invoke(scm)
                    if (v instanceof CharSequence && v.toString().trim()) { sink << v.toString() }
                } catch (ex) { }
            }
        }
    }
}

// Diagnostic: for container items with no SCM found, list the source object class + its
// zero-arg getters so the extraction can be tuned to the actual source type.
def probeSources = { item ->
    def info = []
    def srcs = []
    try { if (item.metaClass.respondsTo(item, 'getSCMSources')) { srcs.addAll(item.getSCMSources()) } } catch (ex) { }
    try { if (item.metaClass.respondsTo(item, 'getSources')) { item.getSources().each { s -> try { srcs << s.getSource() } catch (ex) { } } } } catch (ex) { }
    srcs.each { s ->
        if (s == null) { return }
        def getters = s.getClass().getMethods().findAll { it.getParameterTypes().length == 0 && (it.getName().startsWith('get') || it.getName().startsWith('is')) }.collect { it.getName() }
        info << [cls: s.getClass().getName(), getters: getters]
    }
    return info
}

def scmUrlsOf = { item ->
    def sink = [] as Set
    try { if (item.metaClass.respondsTo(item, 'getSCMs')) { item.getSCMs().each { addUrlsFrom(it, sink) } } } catch (ex) { }
    try {
        if (item.metaClass.respondsTo(item, 'getDefinition')) {
            def d = item.getDefinition()
            if (d != null && d.metaClass.respondsTo(d, 'getScm')) { addUrlsFrom(d.getScm(), sink) }
        }
    } catch (ex) { }
    try { if (item.metaClass.respondsTo(item, 'getSCMSources')) { item.getSCMSources().each { addUrlsFrom(it, sink) } } } catch (ex) { }
    try { if (item.metaClass.respondsTo(item, 'getSources')) { item.getSources().each { s -> try { addUrlsFrom(s.getSource(), sink) } catch (ex) { } } } } catch (ex) { }
    return (sink as List)
}

def triggerNames = { m ->
    def names = [] as Set
    try { m.keySet().each { td -> names << td.getClass().getName().tokenize('.').last() } } catch (ex) { }
    return (names as List)
}

def triggersOf = { item ->
    def out = [] as Set
    try { if (item.metaClass.respondsTo(item, 'getTriggers')) { out.addAll(triggerNames(item.getTriggers())) } } catch (ex) { }
    return (out as List)
}

def out = [version: Jenkins.VERSION, jobs: [], jobTypeCounts: [:], agents: [], plugins: [], scmProbe: []]

Jenkins.instance.getAllItems().each { item ->
    def t = typeOf(item)
    out.jobTypeCounts[t] = (out.jobTypeCounts[t] ?: 0) + 1
    // Skip plain folders from the per-job list (kept only in the type counts)
    if (t == 'Folder') { return }
    def scm = scmUrlsOf(item)
    // When a container has no SCM we could extract, capture a one-off diagnostic probe.
    if (scm.isEmpty() && (t == 'Multibranch' || t == 'OrgFolder') && out.scmProbe.size() < 3) {
        out.scmProbe << [item: item.getFullName(), sources: probeSources(item)]
    }
    def rec = [fullName: item.getFullName(), url: null, type: t,
               scm: scm, triggers: triggersOf(item),
               buildsInWindow: null, buildsPerWeek: null, isJob: (item instanceof Job)]
    try { rec.url = Jenkins.instance.getRootUrl() ? (Jenkins.instance.getRootUrl() + item.getUrl()) : item.getUrl() } catch (ex) { }
    if (item instanceof Job) {
        try {
            int n = 0
            item.getBuilds().each { b -> if (b.getTimeInMillis() >= cutoff) { n++ } }
            rec.buildsInWindow = n
            rec.buildsPerWeek = ((n as double) / ((windowDays as double) / 7.0d)).round(2)
        } catch (ex) { }
    }
    out.jobs << rec
}

Jenkins.instance.nodes.each { node ->
    def cls = node.getClass().getName()
    def isStatic = (node instanceof hudson.slaves.DumbSlave)
    if (cls.toLowerCase().contains('kubernetes')) { isStatic = false }
    if (cls.toLowerCase().contains('ec2') || cls.toLowerCase().contains('docker')) { isStatic = false }
    if (!isStatic) { return }
    def rec = [name: node.getNodeName(), executors: null, labels: null, remoteFS: null,
               launcher: null, offline: null, arch: null, memoryMB: null]
    try { rec.executors = node.getNumExecutors() } catch (ex) { }
    try { rec.labels = node.getLabelString() } catch (ex) { }
    try { rec.remoteFS = node.getRemoteFS() } catch (ex) { }
    try { rec.launcher = node.getLauncher()?.getClass()?.getName() } catch (ex) { }
    try {
        def comp = node.toComputer()
        if (comp != null) {
            try { rec.offline = comp.isOffline() } catch (ex) { }
            try {
                def md = comp.getMonitorData()
                md.each { k, v ->
                    if (v == null) { return }
                    def kn = k.toString().toLowerCase()
                    if (kn.contains('architecture')) { rec.arch = v.toString() }
                    if (kn.contains('swapspace') || kn.contains('memory')) {
                        try {
                            def mth = v.getClass().getMethods().find { it.getName() == 'getTotalPhysicalMemory' }
                            if (mth != null) { def bytes = mth.invoke(v); if (bytes instanceof Number) { rec.memoryMB = (bytes.longValue() / (1024L*1024L)) } }
                        } catch (ex) { }
                    }
                }
            } catch (ex) { }
        }
    } catch (ex) { }
    out.agents << rec
}

Jenkins.instance.pluginManager.plugins.each { p ->
    def rec = [shortName: p.getShortName(), version: null, enabled: null, active: null, hasUpdate: null, deprecated: null]
    try { rec.version = p.getVersion() } catch (ex) { }
    try { rec.enabled = p.isEnabled() } catch (ex) { }
    try { rec.active = p.isActive() } catch (ex) { }
    try { rec.hasUpdate = p.hasUpdate() } catch (ex) { }
    try { def d = p.getDeprecations(); rec.deprecated = (d != null && !d.isEmpty()) } catch (ex) { }
    out.plugins << rec
}

println "===JSON-START==="
println JsonOutput.toJson(out)
println "===JSON-END==="
"""

# Lightweight plugin-only script, used for the OC (jobs/agents aren't meaningful there).
GROOVY_PLUGINS = r"""
import groovy.json.JsonOutput
def out = [version: Jenkins.VERSION, plugins: []]
Jenkins.instance.pluginManager.plugins.each { p ->
    def rec = [shortName: p.getShortName(), version: null, enabled: null, active: null, hasUpdate: null, deprecated: null]
    try { rec.version = p.getVersion() } catch (ex) { }
    try { rec.enabled = p.isEnabled() } catch (ex) { }
    try { rec.active = p.isActive() } catch (ex) { }
    try { rec.hasUpdate = p.hasUpdate() } catch (ex) { }
    try { def d = p.getDeprecations(); rec.deprecated = (d != null && !d.isEmpty()) } catch (ex) { }
    out.plugins << rec
}
println "===JSON-START==="
println JsonOutput.toJson(out)
println "===JSON-END==="
"""


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------
class RawDefaultsFormatter(argparse.RawDescriptionHelpFormatter, argparse.ArgumentDefaultsHelpFormatter):
    pass


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=RawDefaultsFormatter)
    parser.add_argument('--oc-url', '-o', dest='oc_url', required=True,
                        help='Base URL of the CloudBees Operations Center, e.g. https://cjoc.example.com')
    parser.add_argument('--controllers', dest='controllers', default=None,
                        help='Comma-separated substring filter; only controllers whose name matches are processed')
    parser.add_argument('--skip-plugins', dest='skip_plugins', action='store_true', help='Skip plugin collection/health')
    parser.add_argument('--skip-jobs', dest='skip_jobs', action='store_true', help='Skip job-ecosystem collection')
    parser.add_argument('--skip-resources', dest='skip_resources', action='store_true', help='Skip resource/agent collection')
    parser.add_argument('--build-frequency-days', dest='build_days', type=int, default=30,
                        help='Window (days) used to compute average builds/week')
    parser.add_argument('--job-usage-threshold', '-j', dest='job_usage_threshold', type=int, default=5,
                        help='Passthrough to the reused plugin health report')
    parser.add_argument('--ignore-list', '-i', dest='ignore_list', default=None,
                        help='Passthrough to the reused plugin health report (file of plugins to ignore)')
    parser.add_argument('--no-calculate-obsolete', '-n', dest='no_calc_obsolete', action='store_true',
                        help='Passthrough to the reused plugin health report')
    parser.add_argument('--target-controller-type', '-t', dest='target_controller_type',
                        choices=['mm', 'cm'], default='mm', help='Controller type for the CloudBees update-center lookup')
    parser.add_argument('--output-dir', dest='output_dir', default='output',
                        help='Base directory for the timestamped inventory (relative to this script)')
    parser.add_argument('--team-pattern', dest='team_pattern', default=None,
                        help='Optional regex with one capture group applied to the controller fullName to derive the team')
    return parser.parse_args()


# --------------------------------------------------------------------------------------
# HTTP / Groovy helpers
# --------------------------------------------------------------------------------------
def run_groovy(session, base_url, script, auth):
    """POST a Groovy script to {base_url}/scriptText and return the parsed JSON payload.

    Raises on HTTP error, auth failure, or if the expected JSON markers are missing.
    """
    url = base_url.rstrip('/') + '/scriptText'
    last_exc = None
    for attempt in range(3):
        try:
            resp = session.post(url, data={'script': script}, auth=auth, timeout=HTTP_TIMEOUT)
        except requests.RequestException as e:
            last_exc = e
            time.sleep(1.5 * (attempt + 1))
            continue
        if resp.status_code in RETRYABLE_STATUS:
            last_exc = RuntimeError(f"HTTP {resp.status_code} from {url}")
            time.sleep(1.5 * (attempt + 1))
            continue
        if resp.status_code == 403:
            raise PermissionError(
                f"HTTP 403 from {url} - script console disabled or token lacks Overall/Administer")
        resp.raise_for_status()
        return _extract_json(resp.text, url)
    raise last_exc if last_exc else RuntimeError(f"Failed to POST {url}")


def _extract_json(text, url):
    if JSON_START not in text or JSON_END not in text:
        snippet = text.strip().replace('\n', ' ')[:300]
        raise ValueError(f"No JSON markers in /scriptText response from {url}. Response began: {snippet!r}")
    body = text.split(JSON_START, 1)[1].split(JSON_END, 1)[0].strip()
    return json.loads(body)


# --------------------------------------------------------------------------------------
# Small utilities
# --------------------------------------------------------------------------------------
def sanitize(name):
    safe = []
    for ch in str(name):
        safe.append(ch if (ch.isalnum() or ch in '-_.') else '-')
    slug = ''.join(safe).strip('-')
    return slug or 'unnamed'


def resolve_controller_url(raw_url, oc_url, full_name, name):
    """Return an absolute controller endpoint URL.

    CloudBees' getEndpointUrl() is sometimes empty, in which case discovery returns the
    OC-relative item path (e.g. 'job/controller-0/'). Managed Controllers are served at
    the OC host under their full item path WITHOUT the '/job/' segments and WITHOUT the
    OC's own context path — e.g. OC 'http://host/cjoc' -> controller 'http://host/controller-0/'.
    If the OC already gave us an absolute URL (subdomain-per-controller setups), trust it.
    """
    if raw_url:
        sp = urlsplit(raw_url)
        if sp.scheme and sp.netloc:
            return raw_url.rstrip('/')
    oc = urlsplit(oc_url)
    base = f"{oc.scheme}://{oc.netloc}"
    path = (full_name or name or '').strip('/')
    return f"{base}/{path}".rstrip('/')


def derive_team(full_name, name, pattern):
    if pattern:
        import re
        m = re.search(pattern, full_name or name or '')
        if m and m.groups():
            return sanitize(m.group(1))
    fn = full_name or ''
    if '/' in fn:
        return sanitize(fn.split('/', 1)[0])
    return 'unknown-team'


def write_json(path, data):
    with open(path, 'w') as f:
        json.dump(data, f, indent=2, default=str)


def write_csv(path, header, rows):
    with open(path, 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def na(value):
    return 'N/A' if value is None or value == '' else value


# --------------------------------------------------------------------------------------
# Per-instance collection
# --------------------------------------------------------------------------------------
def collect_plugins_csv(path, plugins):
    rows = []
    for p in sorted(plugins, key=lambda x: (x.get('shortName') or '')):
        status = 'enabled' if p.get('enabled') else 'disabled'
        rows.append([
            na(p.get('shortName')), na(p.get('version')), status,
            na(p.get('active')), na(p.get('hasUpdate')), na(p.get('deprecated')),
        ])
    write_csv(path, ['Name', 'Version', 'Enabled/Disabled', 'Active', 'Has Update', 'Deprecated'], rows)


def write_jobs_outputs(folder, inv):
    jobs = inv.get('jobs', [])
    rows = []
    for j in jobs:
        rows.append([
            na(j.get('fullName')), na(j.get('type')),
            '; '.join(j.get('scm') or []) or 'N/A',
            '; '.join(j.get('triggers') or []) or 'none',
            na(j.get('buildsPerWeek')), na(j.get('buildsInWindow')), na(j.get('url')),
        ])
    write_csv(folder / 'jobs.csv',
              ['Job', 'Type', 'SCM Repo', 'Trigger(s)', 'Builds/Week', 'Builds In Window', 'URL'], rows)

    counts = inv.get('jobTypeCounts', {}) or {}
    summary = [[t, counts[t]] for t in sorted(counts, key=lambda k: counts[k], reverse=True)]
    write_csv(folder / 'job-type-summary.csv', ['Type', 'Count'], summary)

    # Diagnostic: if any container job had no extractable SCM, record what its source
    # objects look like so the extraction can be tuned to the actual source type.
    probe = inv.get('scmProbe') or []
    if probe:
        write_json(folder / 'scm-probe.json', probe)


def write_agents_csv(folder, inv):
    rows = []
    for a in inv.get('agents', []):
        rows.append([
            na(a.get('name')), na(a.get('executors')), na(a.get('labels')),
            na(a.get('arch')), na(a.get('memoryMB')), na(a.get('remoteFS')),
            na(a.get('launcher')), na(a.get('offline')),
        ])
    write_csv(folder / 'static-agents.csv',
              ['Agent', 'Executors', 'Labels', 'Arch/OS', 'Memory (MB)', 'Remote FS', 'Launcher', 'Offline'], rows)


def run_plugin_health(controller_url, auth_user, auth_token, folder, args):
    """Reuse generate_plugin_health_report to download usage + write the health CSV in place.

    Returns the parsed plugin-usage dict (for the org rollup) or None on failure.
    """
    usage_path = str(folder / 'plugin-usage.json')
    health_path = str(folder / 'plugin-health-report.csv')

    phr.download_plugin_usage(controller_url, auth_user, auth_token, usage_path)

    ci_version = phr.get_ci_version(controller_url, auth_user, auth_token)
    phr.main({
        'ci_version': ci_version,
        'plugin_usage_file': usage_path,
        'job_usage_threshold': args.job_usage_threshold,
        'target_controller_type': args.target_controller_type,
        'ignore_list': args.ignore_list,
        'no_calc_obsolete': args.no_calc_obsolete,
        'report_outfile': health_path,
    })

    with open(usage_path, 'r', encoding='utf-8-sig') as f:
        return json.load(f)


# --------------------------------------------------------------------------------------
# Org-wide aggregation
# --------------------------------------------------------------------------------------
def accumulate_org_usage(org, controller_name, usage_json):
    """Fold one controller's plugin-usage JSON into the org-wide accumulator."""
    usages = (usage_json or {}).get('usages', {})
    for plugin, entries in usages.items():
        slot = org.setdefault(plugin, {
            'controllers': set(), 'job_invocations': 0, 'versions': set(),
        })
        slot['controllers'].add(controller_name)
        slot['job_invocations'] += len(gaj.get_affected_jobs(plugin, usages))
        for e in entries:
            v = (e.get('pluginInfo') or {}).get('currentVersion')
            if v:
                slot['versions'].add(v)


def write_org_reports(org_dir, org, health_index):
    ranked = []
    for plugin, slot in org.items():
        h = health_index.get(plugin, {})
        controllers = {c for c in slot['controllers'] if c != 'operations-center'}
        on_oc = 'operations-center' in slot['controllers']
        ranked.append([
            plugin,
            len(controllers),
            'Yes' if on_oc else 'No',
            slot['job_invocations'],
            h.get('tier', 'N/A'),
            h.get('health', 'N/A'),
            h.get('cves', 'N/A'),
            '; '.join(sorted(slot['versions'])) or 'N/A',
        ])
    ranked.sort(key=lambda r: (r[3], r[1]), reverse=True)
    write_csv(org_dir / 'plugin-usage-ranked.csv',
              ['Plugin', '# Controllers', 'On OC', '# Job Invocations', 'Tier', 'Health Score', 'Active CVEs', 'Versions Seen'],
              ranked)

    drift = [[p, '; '.join(sorted(s['versions']))]
             for p, s in sorted(org.items()) if len(s['versions']) > 1]
    write_csv(org_dir / 'plugin-version-drift.csv', ['Plugin', 'Versions Seen'], drift)

    # Objective 3: flag deprecated/unsupported/low-health plugins separately.
    flagged = []
    for plugin, nctrl, on_oc, jobs_, tier, health, cves, versions in ranked:
        reasons = []
        if str(tier).lower() == 'community':
            reasons.append('community/unsupported tier')
        try:
            if int(cves) > 0:
                reasons.append(f'{cves} active CVE(s)')
        except (TypeError, ValueError):
            pass
        try:
            if int(health) < 70:
                reasons.append(f'low health score ({health})')
        except (TypeError, ValueError):
            pass
        if reasons:
            flagged.append([plugin, nctrl, on_oc, jobs_, tier, health, cves, '; '.join(reasons)])
    # Highest-usage flagged plugins first — these are the priority replacements.
    flagged.sort(key=lambda r: r[3], reverse=True)
    write_csv(org_dir / 'flagged-plugins.csv',
              ['Plugin', '# Controllers', 'On OC', '# Job Invocations', 'Tier', 'Health Score', 'Active CVEs', 'Flag Reason'],
              flagged)


def load_health_index(folder):
    """Read a per-controller plugin-health-report.csv into {plugin: {tier,health,cves}}."""
    index = {}
    path = folder / 'plugin-health-report.csv'
    if not path.is_file():
        return index
    with open(path, 'r', newline='') as f:
        reader = csv.DictReader(f)
        for row in reader:
            name = row.get('Name')
            if name:
                index[name] = {
                    'tier': row.get('Plugin Tier', 'N/A'),
                    'health': row.get('Health Score', 'N/A'),
                    'cves': row.get('Active CVEs', 'N/A'),
                }
    return index


# --------------------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------------------
def main():
    args = parse_args()

    user = os.environ.get('JENKINS_USER_ID') or None
    token = os.environ.get('JENKINS_API_TOKEN') or None
    if not user or not token:
        print("JENKINS_USER_ID and JENKINS_API_TOKEN need to be set in your environment")
        sys.exit(1)
    auth = (user, token)

    oc_url = args.oc_url.rstrip('/')
    session = requests.Session()
    errors = []  # list of [instance, phase, url, error]
    started = datetime.now()

    def record_error(instance, phase, url, exc):
        msg = f"{type(exc).__name__}: {exc}"
        errors.append([instance, phase, url, msg])
        print(f"   !! [{instance}/{phase}] {msg}")

    # -- Output tree -------------------------------------------------------------------
    stamp = started.strftime('%Y%m%d-%H%M%S')
    root = Path(args.output_dir) / f"{stamp}_environment-inventory"
    oc_dir = root / 'operations-center'
    controllers_dir = root / 'controllers'
    org_dir = root / 'org-wide'
    for d in (oc_dir, controllers_dir, org_dir):
        d.mkdir(parents=True, exist_ok=True)
    print(f" --> Writing inventory to {root.resolve()}")

    org_usage = {}
    combined_health_index = {}
    controllers_processed = 0

    # -- OC discovery ------------------------------------------------------------------
    print(f" --> Discovering controllers from Operations Center: {oc_url}")
    discovered = []
    try:
        disc = run_groovy(session, oc_url, GROOVY_DISCOVER, auth)
        discovered = disc.get('controllers', [])
    except Exception as e:
        record_error('operations-center', 'discovery', oc_url, e)

    # Attach a derived team + folder slug + resolved absolute URL to each record.
    for c in discovered:
        c['team'] = derive_team(c.get('fullName'), c.get('name'), args.team_pattern)
        c['folderSlug'] = f"{c['team']}__{sanitize(c.get('name') or 'controller')}"
        c['rawUrl'] = c.get('url')
        c['url'] = resolve_controller_url(c.get('url'), oc_url, c.get('fullName'), c.get('name'))

    # Apply --controllers filter
    if args.controllers:
        wanted = [w.strip().lower() for w in args.controllers.split(',') if w.strip()]
        before = len(discovered)
        discovered = [c for c in discovered
                      if any(w in (c.get('name') or '').lower() for w in wanted)]
        print(f" --> Controller filter applied: {len(discovered)}/{before} match {wanted}")

    # Discovery manifest CSV
    manifest_rows = []
    for c in discovered:
        prov = c.get('provisioning') or {}
        manifest_rows.append([
            na(c.get('name')), na(c.get('url')), na(c.get('className')),
            na(c.get('state')), na(c.get('online')), c.get('team'),
            na(prov.get('cpus')), na(prov.get('memoryMB')),
            na(prov.get('ha')), na(prov.get('replicas')),
        ])
    write_csv(oc_dir / 'controllers-discovered.csv',
              ['Name', 'URL', 'Class', 'State', 'Online', 'Team',
               'CPUs', 'Memory (MB)', 'HA', 'Replicas'], manifest_rows)
    print(f" --> Discovered {len(discovered)} controller(s)")

    # -- OC pass (version + plugins + health) ------------------------------------------
    oc_info = {'url': oc_url, 'name': 'operations-center', 'version': None}
    if not args.skip_plugins:
        try:
            oc_plugins = run_groovy(session, oc_url, GROOVY_PLUGINS, auth)
            oc_info['version'] = oc_plugins.get('version')
            collect_plugins_csv(oc_dir / 'oc-plugins.csv', oc_plugins.get('plugins', []))
        except Exception as e:
            record_error('operations-center', 'plugins', oc_url, e)
        # The OC may not expose /pluginUsage/download; guard it.
        try:
            usage = run_plugin_health(oc_url, user, token, oc_dir, args)
            # rename default health/usage filenames to oc-prefixed for clarity
            _rename_if_exists(oc_dir / 'plugin-health-report.csv', oc_dir / 'oc-plugin-health-report.csv')
            _rename_if_exists(oc_dir / 'plugin-usage.json', oc_dir / 'oc-plugin-usage.json')
            accumulate_org_usage(org_usage, 'operations-center', usage)
        except Exception as e:
            record_error('operations-center', 'plugin-health', oc_url, e)
    write_json(oc_dir / 'oc-info.json', oc_info)

    # -- Per-controller pass -----------------------------------------------------------
    for c in discovered:
        name = c.get('name') or 'controller'
        c_url = (c.get('url') or '').rstrip('/')
        folder = controllers_dir / c['folderSlug']
        folder.mkdir(parents=True, exist_ok=True)
        print(f"\n === Controller: {name}  ({c_url or 'no url'}) ===")

        if c.get('online') is False:
            record_error(name, 'offline', c_url, RuntimeError('controller reported offline by OC; skipping live calls'))
            _write_resources(folder, c, None)
            continue
        if not c_url:
            record_error(name, 'no-url', '', RuntimeError('OC did not report an endpoint URL'))
            _write_resources(folder, c, None)
            continue

        inv = None
        need_live = not (args.skip_jobs and args.skip_resources)
        if need_live:
            try:
                inv = run_groovy(session, c_url, GROOVY_CONTROLLER.replace('__WINDOW_DAYS__', str(args.build_days)), auth)
            except Exception as e:
                record_error(name, 'inventory', c_url, e)

        # Objective 1: resources + static agents
        if not args.skip_resources:
            _write_resources(folder, c, inv)
            if inv is not None:
                try:
                    write_agents_csv(folder, inv)
                except Exception as e:
                    record_error(name, 'agents', c_url, e)

        # Objective 2: jobs
        if not args.skip_jobs and inv is not None:
            try:
                write_jobs_outputs(folder, inv)
            except Exception as e:
                record_error(name, 'jobs', c_url, e)

        # Objective 3: plugins (list from Groovy) + health report (reused script)
        if not args.skip_plugins:
            if inv is not None and inv.get('plugins') is not None:
                try:
                    collect_plugins_csv(folder / 'plugins.csv', inv.get('plugins', []))
                except Exception as e:
                    record_error(name, 'plugins', c_url, e)
            try:
                usage = run_plugin_health(c_url, user, token, folder, args)
                accumulate_org_usage(org_usage, name, usage)
                combined_health_index.update(load_health_index(folder))
            except Exception as e:
                record_error(name, 'plugin-health', c_url, e)

        controllers_processed += 1

    # -- Org-wide rollups --------------------------------------------------------------
    try:
        write_org_reports(org_dir, org_usage, combined_health_index)
    except Exception as e:
        record_error('org-wide', 'plugin-rollup', '', e)

    # Resource + job ecosystem rollups
    _write_org_resource_summary(org_dir, discovered)
    _write_org_job_summary(org_dir, controllers_dir, discovered)

    # -- Errors + metadata -------------------------------------------------------------
    write_csv(root / 'inventory-errors.csv', ['Instance', 'Phase', 'URL', 'Error'], errors)
    finished = datetime.now()
    write_json(root / 'run-metadata.json', {
        'started': started.isoformat(),
        'finished': finished.isoformat(),
        'duration_seconds': round((finished - started).total_seconds(), 1),
        'oc_url': oc_url,
        'controllers_discovered': len(discovered),
        'controllers_processed': controllers_processed,
        'errors': len(errors),
        'args': {k: v for k, v in vars(args).items()},
    })

    print(f"\n --> Done. Controllers processed: {controllers_processed}/{len(discovered)}, errors: {len(errors)}")
    print(f" --> Inventory: {root.resolve()}")
    if errors:
        print(f" --> Review {root / 'inventory-errors.csv'} for per-instance failures")


def _rename_if_exists(src, dst):
    if Path(src).is_file():
        Path(src).replace(dst)


def _write_resources(folder, controller_rec, inv):
    prov = controller_rec.get('provisioning') or {}
    version = inv.get('version') if inv else None
    data = {
        'name': controller_rec.get('name'),
        'url': controller_rec.get('url'),
        'team': controller_rec.get('team'),
        'className': controller_rec.get('className'),
        'version': na(version),
        'cpus': na(prov.get('cpus')),
        'memory_mb': na(prov.get('memoryMB')),
        'disk_gb': na(prov.get('diskGB')),
        'ha': na(prov.get('ha')),
        'ha_inferred': prov.get('haInferred', False),
        'replicas': na(prov.get('replicas')),
        'state': na(controller_rec.get('state')),
        'online': na(controller_rec.get('online')),
        'provisioning_source_class': na(prov.get('provisioningClass')),
    }
    # Diagnostic: list the HA/replication accessors (and their values) found on the
    # provisioning/controller objects, so the result can be verified / the Groovy tuned.
    if prov.get('probe'):
        data['_ha_probe'] = prov.get('probe')
    write_json(folder / 'resources.json', data)


def _write_org_resource_summary(org_dir, discovered):
    rows = []
    for c in discovered:
        prov = c.get('provisioning') or {}
        rows.append([
            na(c.get('name')), c.get('team'), na(c.get('url')),
            na(prov.get('cpus')), na(prov.get('memoryMB')),
            na(prov.get('ha')), na(prov.get('replicas')), na(c.get('state')),
        ])
    write_csv(org_dir / 'controller-resource-summary.csv',
              ['Controller', 'Team', 'URL', 'CPUs', 'Memory (MB)', 'HA', 'Replicas', 'State'], rows)


def _write_org_job_summary(org_dir, controllers_dir, discovered):
    """Aggregate per-controller job-type-summary.csv files into one org rollup."""
    totals = {}
    per_controller = []
    for c in discovered:
        folder = controllers_dir / c.get('folderSlug', '')
        summary_path = folder / 'job-type-summary.csv'
        if not summary_path.is_file():
            continue
        row = {'Controller': c.get('name'), 'Team': c.get('team')}
        with open(summary_path, 'r', newline='') as f:
            for r in csv.DictReader(f):
                t, n = r.get('Type'), int(r.get('Count') or 0)
                row[t] = n
                totals[t] = totals.get(t, 0) + n
        per_controller.append(row)

    all_types = sorted(totals, key=lambda k: totals[k], reverse=True)
    header = ['Controller', 'Team'] + all_types
    rows = [[pc.get('Controller'), pc.get('Team')] + [pc.get(t, 0) for t in all_types] for pc in per_controller]
    rows.append(['ALL', ''] + [totals[t] for t in all_types])
    write_csv(org_dir / 'job-ecosystem-summary.csv', header, rows)


if __name__ == '__main__':
    main()
