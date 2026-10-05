#!/usr/bin/env python

# WINDOWS USERS #
# You will probably have to change that top line to #!python

# pip install requests tqdm

import json, requests
import csv
import argparse
import os, shutil
from tqdm import tqdm
import sys
from pathlib import Path
from urllib.parse import urlsplit

try:
    # This directory is in the repo with a .gitkeep, however if you pack up the scripts
    # and send them to a customer they likely won't have an adjacent 'output' directory.
    Path('output').mkdir(exist_ok=True)
except Exception as e:
    raise Exception(f"Unable to create the 'output' directory!\n{e}")

URL_UPDATE_CENTER       = "https://jenkins-updates.cloudbees.com/update-center"
URL_PLUGIN_HEALTH       = "https://plugin-health.jenkins.io/api/scores"
URL_CLOUDBEES_DOCS      = "https://docs.cloudbees.com/plugins/ci"
URL_JENKINS_PLUGINS     = "https://plugins.jenkins.io/api/plugin"

_URLS = [URL_UPDATE_CENTER, URL_PLUGIN_HEALTH, URL_CLOUDBEES_DOCS, URL_JENKINS_PLUGINS]

PLUGINS_TO_IGNORE = [
    'cloudbees-pipeline-explorer',
    'cloudbees-support',
    'cloudbees-license',
    'cloudbees-license-tracker',
    'cloudbees-ha',
    'cloudbees-monitoring',
    'cloudbees-jenkins-advisor',
    'cloudbees-inactive-items',
    'cloudbees-unified-ui',
]

CONTROLLER_TYPES = {
    'cm': 'Client Controller',
    'mm': 'Managed Controller'
}

_url_str = '\n\t - '.join(_URLS)
_desc = f"""
Analyze the data provided by the CloudBees Plugin Usage Analyzer Plugin.
See: https://docs.cloudbees.com/docs/cloudbees-ci/latest/plugin-management/plugin-usage
The report from CI gives us a lot of information, however it's typically just the start
of the journey when you're looking to cleanup old or unused plugins. A plugin may have 
configurations, but no jobs associated with it (or low job usage). We'll determine that
with subsequent steps.

--> We will need to connect to the following URLS:
\t - {_url_str}

With this we put together a csv with the following information:
Name,Version,Last Release Date,Total Installs,Health Score,Plugin Tier,Active CVEs,# Job Usage
"""

class RawDefaultsFormatter(argparse.RawDescriptionHelpFormatter, argparse.ArgumentDefaultsHelpFormatter):
    """Preserves formatting in description/epilog AND shows defaults."""
    pass


def parse_args():
    parser = argparse.ArgumentParser(description=_desc, formatter_class=RawDefaultsFormatter)

    # We want the user to specify either --file or --controller-url, but not both. BUT, at least 1 is required
    mode_group = parser.add_argument_group(
        title="Plugin Usage Data",
        description="If we've already downloaded it, we can provide the file. Otherwise we need the URL to the controller to download the plugin usage"
    )

    exclusive_group = mode_group.add_mutually_exclusive_group(required=True)
    exclusive_group.add_argument('--file', '-f', help='Path to Plugin Usage analysis JSON file')
    exclusive_group.add_argument('--controller-url', '-c', dest='controller_url', 
        help='FQDN to the controller: https://example.com/controller-name OR https//controller-name.example.com')

    parser.add_argument('--download-only', '-d', dest='download_only', action='store_true',
        help='If specified, we will only download the plugin usage from the controller')

    parser.add_argument('--target-controller-type', '-t', dest='target_controller_type', choices=CONTROLLER_TYPES.keys(), default='mm', required=False, 
        help=f'The type of controller: {CONTROLLER_TYPES}')

    parser.add_argument('--ci-version', '-v', dest="ci_version", required=False,
        help='CBCI version to check against for CAP status/tier. If not specified we\'ll use the version from querying the controller-url. \
            If the file option was used, then we will need this to be specified.')
    
    parser.add_argument('--ignore-list', '-i', dest='ignore_list', required=False,
        help='If you have plugins that you wish to exclude from processing, throw them into a file (one per line) and refer to it here')
    
    parser.add_argument('--job-usage-threshold', '-j', dest='job_usage_threshold', default=5, 
        help='Max number of usages for a plugin to be identified as "low" usage.')
    
    parser.add_argument('--no-calculate-obsolete', '-n', dest='no_calc_obsolete', action='store_true', 
        help='Check each plugin against the CloudBees Plugin list to determine if it is still distributed or not.')

    return parser.parse_args()

def get_ci_version(url, user, token):
    try:
        print(" --> Getting CI version... ")
        response = requests.head(url, auth=(user, token), allow_redirects=True)
        version = response.headers.get('X-Jenkins')
    except Exception as e:
        raise Exception(f"Unable to get X-Jenkins header\n{e}")

    return version

def download_plugin_usage(url: str, user: str, token: str, localfile: str):
    jenkins_url = f"{url}/pluginUsage/download"
    print(f" --> Downloading plugin usage from {jenkins_url} to {localfile}")
    # No need for a prog bar here...
    with requests.get(jenkins_url, auth=(user, token), stream=True) as r:
        r.raise_for_status() # Check for 404/500 errors
        with open(localfile, 'wb') as f:
            for chunk in r.iter_content(chunk_size=8192): 
                f.write(chunk)
    # No return, we either did or didn't

def get_file_data(filename: str) -> dict:
    data = {}
    print(f" --> Loading datafile {filename}")
    with open(filename, 'r') as f:
        data = json.load(f)
    return data

def get_cbuc_data(controller_type: str, ci_version: str) -> dict:
    url = f"{URL_UPDATE_CENTER}/envelope-core-{controller_type}/update-center.json?version={ci_version}"

    print(f" --> Getting CB Update Center Data for version {ci_version}")

    # gotta chop a couple things off the edges to get the json...
    header_remove = 19
    footer_remove = 4
    try:
        resp = requests.get(url).text
        return json.loads(resp[header_remove:-footer_remove])
    except Exception as e:
        raise Exception(f"ERROR: unable to pull and parse data from {url}\n{e}")

def get_plugin_health_data() -> dict:
    print(f" --> Getting public health scores...")
    req = requests.get(URL_PLUGIN_HEALTH)
    try:
        return req.json()
    except Exception as e:
        raise Exception(f"ERROR: Unable to get json data from {URL_PLUGIN_HEALTH}\n{e}")

def is_plugin_obsolete(plugin_name: str) -> bool:
    url = f"{URL_CLOUDBEES_DOCS}/{plugin_name}"
    r = requests.get(url)
    return r.status_code != 200

def build_health_report_data(pname: str, pdata: dict, p_health_data: dict, uc_json: dict, num_jobs: int) -> list:
    version = pdata['pluginInfo']['currentVersion']
    url = f"{URL_JENKINS_PLUGINS}/{pname}"
    try:
        resp                = requests.get(url).json()
        build_date          = resp['buildDate']
        current_installs    = resp['stats']['currentInstalls']
        all_warnings        = resp['securityWarnings']
        active_cves         = 0
        
        if all_warnings != None:
            active_cves = sum(1 for w in all_warnings if w.get('active'))

        health_score = p_health_data['plugins'][pname]['value']
        tier = uc_json['envelope']['plugins'].get(pname, {}).get('tier', 'community')

        plugin_health_report = [pname, version, build_date, str(current_installs), str(health_score), tier, str(active_cves), str(num_jobs)]
    except:
        plugin_health_report = [pname, version, 'N/A', 'N/A', 'N/A', 'N/A', 'N/A', 'N/A']

    return plugin_health_report

def get_plugins_to_ignore(ignore_file='') -> list:
    plugins = []
    if ignore_file:
        if os.path.isfile(ignore_file):
            print(f" --> {ignore_file} found...")

            with open(ignore_file, 'r') as f:
                plugins = f.read().splitlines()
        else:
            print(f" --> WARNING: {ignore_file} specified but not found!")
    
    return list(set(plugins + PLUGINS_TO_IGNORE))

def print_term_results(obs_plugins, no_job_plugins, low_job_plugins, plugin_ignore_list, dependency_plugins, job_usage_threshold):
    if obs_plugins:
        print(f"\n --> Obsolete Installed Plugins: ({len(obs_plugins)})")

        for oip in sorted(obs_plugins):
            print(f"\t{oip}")

    if no_job_plugins:
        no_deps = list(set(no_job_plugins) - set(dependency_plugins))
        print(f"\n --> Plugins with No Job Usage: ({len(no_deps)})")
        
        for njup in sorted(no_deps):
            print(f"\t{njup}")

    if low_job_plugins:
        no_deps = list(set(low_job_plugins) - set(dependency_plugins))
        print(f"\n --> Plugins with fewer than {job_usage_threshold} usages: ({len(no_deps)})")

        for ljup in sorted(no_deps):
            print(f"\t{ljup}")

    if plugin_ignore_list:
        print(f"\n --> Plugins ignored ({len(plugin_ignore_list)})")

        for pi in sorted(plugin_ignore_list):
            print(f"\t{pi}")

# should this be imported, we'll require all this to be present
def main(event_data: dict):
    req_keys = ('ci_version', 'plugin_usage_file', 'job_usage_threshold', 'target_controller_type', 'ignore_list', 'no_calc_obsolete', 'report_outfile')

    for k in req_keys:
        try:
            _ = event_data[k]
        except KeyError as e:
            raise Exception(f"Expected to recieve the following keys in the pass data structure: {req_keys}\n{e}")

    plugin_usage_file       = event_data.get('plugin_usage_file')
    target_controller_type  = event_data.get('target_controller_type')
    ci_version              = event_data.get('ci_version')
    job_usage_threshold     = event_data.get('job_usage_threshold')
    ignore_list             = event_data.get('ignore_list')
    no_calc_obsolete        = event_data.get('no_calc_obsolete')
    report_outfile          = event_data.get('report_outfile')

    plugin_data             = get_file_data(plugin_usage_file)
    update_center_data      = get_cbuc_data(target_controller_type, ci_version)
    plugin_health_data      = get_plugin_health_data()

    plugin_health_report        = []
    obsolete_installed_plugins  = []
    no_job_usage_plugins        = []
    low_job_usage_plugins       = []
    dependency_plugins          = []

    all_plugins = plugin_data['usages']

    plugin_ignore_list = get_plugins_to_ignore(ignore_list)

    print(f" --> Number of currently installed plugins: {len(all_plugins)}")
    if len(plugin_ignore_list) > 0:
        print(f" --> Skipping {(len(plugin_ignore_list)-len(PLUGINS_TO_IGNORE))} user & {len(PLUGINS_TO_IGNORE)} script defined plugins...")

    pbar = tqdm(all_plugins.items(), desc='Processing Plugins')

    for plugin, plugin_data in pbar:
        if plugin in plugin_ignore_list:
            continue

        pbar.set_description(f"Plugin: {plugin[:30]:<30}")

        if no_calc_obsolete is False and is_plugin_obsolete(plugin):
            obsolete_installed_plugins.append(plugin)

        usageTypes = []
        for p in plugin_data:
            if p['location']['type'] == 'plugin-dependency':
                dependency_plugins.append(p['pluginInfo']['shortName'])
            usageTypes.append(p['location']['type'])

        exist_count = usageTypes.count('pipeline') + usageTypes.count('item')

        if exist_count == 0:
            no_job_usage_plugins.append(plugin)
        elif exist_count > 0 and exist_count < int(job_usage_threshold):
            low_job_usage_plugins.append(plugin)

        plugin_health_report.append(build_health_report_data(plugin, p, plugin_health_data, update_center_data, exist_count))

    if plugin_health_report:
        header = ['Name', 'Version', 'Last Release Date', 'Total Installs', 'Health Score', 'Plugin Tier', 'Active CVEs', '# Job Usage']
        print(f" --> Writing {report_outfile}")
        with open(report_outfile, 'w') as f:
            csv_writer = csv.writer(f)
            csv_writer.writerow(header)
            csv_writer.writerows(plugin_health_report)

    print_term_results(
        obsolete_installed_plugins,
        no_job_usage_plugins,
        low_job_usage_plugins,
        plugin_ignore_list,
        dependency_plugins,
        job_usage_threshold
    )

    
if __name__ == '__main__':
    args = parse_args()
    job_usage_threshold = args.job_usage_threshold
    ci_version = None

    if args.controller_url:
        parsed = urlsplit(args.controller_url)
        user = os.environ.get('JENKINS_USER_ID') or None
        token = os.environ.get('JENKINS_API_TOKEN') or None
        if not user or not token:
            raise Exception("JENKINS_USER_ID and JENKINS_API_TOKEN needs to be set in your environment")

        plugin_usage_file = f"./output/{parsed.hostname}-plugin-usage.json"
        outfile = f"output/{parsed.hostname}-plugin-health-report.csv"

        if os.path.isfile(plugin_usage_file):
            print(f" --> {plugin_usage_file} already exists. Backing it up first...")
            shutil.move(plugin_usage_file, f"{plugin_usage_file}.bak")
        
        download_plugin_usage(args.controller_url, user, token, plugin_usage_file)
        if args.download_only:
            print(f" --> Plugin Usage downloaded to: {plugin_usage_file}")
            sys.exit(0)
        
        if not args.ci_version:
            ci_version = get_ci_version(args.controller_url, user, token)
    else:
        if not args.ci_version:
            print('Error: --ci-version must be specified when not using the --controller-url')
            sys.exit(1)
        
        ci_version = args.ci_version
        plugin_usage_file = args.file

        file_base = os.path.basename(args.file).removesuffix('.json')
        outfile = f"output/{file_base}-plugin-health-report.csv"

    info_pack = {
        'ci_version': ci_version,
        'plugin_usage_file': plugin_usage_file,
        'job_usage_threshold': job_usage_threshold,
        'target_controller_type': args.target_controller_type,
        'ignore_list': args.ignore_list,
        'no_calc_obsolete': args.no_calc_obsolete, 
        'report_outfile': outfile
    }

    main(info_pack)

    