#!/usr/bin/env python

import csv
import json
import yaml
import os
import sys
import argparse
from pathlib import Path

try:
    Path('output').mkdir(exist_ok=True)
except Exception as e:
    print(f"Unable to create the 'output' directory: {e}")
    sys.exit(1)

NAME_WIDTH = 45
NUM_WIDTH = 10

_desc = f"""
Get affected jobs based on a list of plugins.

You've already run 'get_unused_plugins.py' for plugins with 1 or more job usages. We'll use that list as well as
the initial plugin usage analysis file from the CBCI UI to generate a yaml file containing the URL to every job
that uses each plugin
"""

class RawDefaultsFormatter(argparse.RawDescriptionHelpFormatter, argparse.ArgumentDefaultsHelpFormatter):
    """Preserves formatting in description/epilog AND shows defaults."""
    pass


def parse_args():
    parser = argparse.ArgumentParser(formatter_class=RawDefaultsFormatter,
        description=_desc)
    
    parser.add_argument('--plugin-list', '-p', dest='plugin_list_file', default='output/plugin-list.txt',
        help='Path to a flat file with a list of plugin names - one per line')
    
    parser.add_argument('--analysis-file', '-a', dest='analysis_file', required=True,
        help="Path to the plugin usage analysis file (likely downloaded from the CBCI UI)")
    
    parser.add_argument('--write-summary', '-w', dest='write_summary', action='store_true',
        help='Write the plugin name and the number of affected jobs to a csv for sorting/filtering \
            (this is separate from the affected jobs output)')
    
    return parser.parse_args()

def get_affected_jobs(plugin: str, usage_data: dict) -> list:
    jobs = []

    for p, p_data in usage_data.items():
        if (p == plugin):
            for data in p_data:
                if data['location']['type'] in ('pipeline', 'item'):
                    jobs.append(''.join([data['location']['controllerURL'], data['location']['url']]))

    return jobs

def write_summary(base_filename: str, out_data: dict) -> None:
    summary_file = f"output/{base_filename}-summary.csv"
    print(f"Writing summary to {summary_file}")

    header = ['Plugin Name', '# Affected Jobs']
    csv_struct = []
    for _p, jobs, in out_data.items():
        csv_struct.append([_p, len(jobs)])

    with open(summary_file, 'w', newline='') as f:
        csv_writer = csv.writer(f)
        csv_writer.writerow(header)
        csv_writer.writerows(csv_struct)

def write_output(base_filename: str, outdata: dict) -> None:
    outfile = f"output/{base_filename}-affected-jobs.yaml"

    if outdata:
        print(f"Writing {outfile}")
        with open(outfile, 'w') as f:
            yaml.dump(outdata, f)
    else:
        print("No affected jobs found...")

def main(plugin_file: str, analysis_file: str, do_write_summary: bool) -> None:
    plugin_list = []
    usage_data = []
    out_data = {}
    jobs = None
    file_base = os.path.basename(analysis_file).removesuffix('.json')

    with open(plugin_file, 'r') as f:
        lines = f.readlines()
        plugin_list = [line.strip() for line in lines]

    with open(analysis_file, 'r', encoding='utf-8-sig') as f:
        _usage = json.load(f)
        usage_data = _usage['usages']

    print(f"{'Plugin':<{NAME_WIDTH}}{'# Jobs':>{NUM_WIDTH}}")
    print("*" * (NAME_WIDTH + NUM_WIDTH))

    for p in plugin_list:
        jobs = get_affected_jobs(p, usage_data)

        if jobs:
            out_data.update({p: jobs})
            print(f"{p:.<{NAME_WIDTH}}{len(jobs):.>{NUM_WIDTH}}")
        
    print("*" * (NAME_WIDTH + NUM_WIDTH))

    if do_write_summary:
        write_summary(file_base, out_data)

    write_output(file_base, out_data)

if __name__ == '__main__':
    args = parse_args()
    plugin_file = args.plugin_list_file
    analysis_file = args.analysis_file
    arg_write_summary = args.write_summary

    if not os.path.isfile(plugin_file) or not os.path.isfile(analysis_file):
        print(f"Either {plugin_file} or {analysis_file} doesn't seem to exist")
        sys.exit(1)

    main(plugin_file, analysis_file, arg_write_summary)
    