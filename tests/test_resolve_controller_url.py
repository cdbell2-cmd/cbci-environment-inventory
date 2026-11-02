#!/usr/bin/env python
"""Offline unit tests for resolve_controller_url().

These need no live CloudBees environment — they pin the folder-path flattening
behaviour so the regression (folder-nested controllers resolving to a nested
'/releases/v2609a' path instead of the real '/releases-v2609a' endpoint) stays fixed.

Run:  python -m pytest tests/ -q      (or)      python tests/test_resolve_controller_url.py
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from generate_environment_inventory import resolve_controller_url  # noqa: E402


class ResolveControllerUrlTests(unittest.TestCase):
    OC = 'https://ci.rye.ps.beescloud.com/cjoc'

    def test_folder_nested_controller_flattens_with_hyphen(self):
        # The exact reported case: a managed controller inside the 'releases' folder.
        # UI path is /cjoc/job/releases/job/v2609a/ ; the endpoint is host-root flattened.
        url = resolve_controller_url(None, self.OC, 'releases/v2609a', 'v2609a')
        self.assertEqual(url, 'https://ci.rye.ps.beescloud.com/releases-v2609a')

    def test_deeply_nested_controller_flattens_every_segment(self):
        url = resolve_controller_url(None, self.OC, 'team-a/releases/v2609a', 'v2609a')
        self.assertEqual(url, 'https://ci.rye.ps.beescloud.com/team-a-releases-v2609a')

    def test_top_level_controller_unchanged(self):
        # No folders -> no '/' in the full name -> flattening is a no-op (regression guard).
        url = resolve_controller_url(None, 'http://ci.localtest.me/cjoc',
                                     'controller-0', 'controller-0')
        self.assertEqual(url, 'http://ci.localtest.me/controller-0')

    def test_absolute_endpoint_url_is_trusted(self):
        # Subdomain-per-controller / explicit getEndpointUrl() wins and is returned as-is.
        url = resolve_controller_url('https://v2609a.ci.example.com/', self.OC,
                                     'releases/v2609a', 'v2609a')
        self.assertEqual(url, 'https://v2609a.ci.example.com')

    def test_oc_context_path_is_stripped_controllers_live_at_host_root(self):
        # OC is served under /cjoc; controllers are NOT under that context path.
        url = resolve_controller_url(None, self.OC, 'v2609a', 'v2609a')
        self.assertEqual(url, 'https://ci.rye.ps.beescloud.com/v2609a')

    def test_relative_raw_url_is_ignored_in_favour_of_full_name(self):
        # getEndpointUrl() sometimes returns the OC-relative item path; it is not absolute,
        # so we fall back to the flattened full name rather than trusting the relative path.
        url = resolve_controller_url('job/releases/job/v2609a/', self.OC,
                                     'releases/v2609a', 'v2609a')
        self.assertEqual(url, 'https://ci.rye.ps.beescloud.com/releases-v2609a')

    def test_falls_back_to_name_when_full_name_missing(self):
        url = resolve_controller_url(None, self.OC, None, 'v2609a')
        self.assertEqual(url, 'https://ci.rye.ps.beescloud.com/v2609a')


if __name__ == '__main__':
    unittest.main(verbosity=2)
