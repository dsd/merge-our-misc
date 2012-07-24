#!/usr/bin/env python
# -*- coding: utf-8 -*-
# generate-diffs.py - generate changes and diff files for new packages
#
# Copyright © 2008 Canonical Ltd.
# Author: Scott James Remnant <scott@ubuntu.com>.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of version 3 of the GNU General Public License as
# published by the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import os
import logging

from momlib import *
from util import tree
from config import Distro


def options(parser):
    parser.add_option("-p", "--package", type="string", metavar="PACKAGE",
                      action="append",
                      help="Process only these packages")
    parser.add_option("-t", "--target", type="string", metavar="TARGET",
                      default=None,
                      help="Process only this distribution target")

def main(options, args):
    if options.target is not None:
        target_distro, target_dist, target_component = get_target_distro_dist_component(options.target)
        distros = [target_distro]
    elif len(args):
        distros = args
    else:
        distros = get_pool_distros()

    # For latest version of each package in the given distributions, iterate the pool in order
    # and generate a diff from the previous version and a changes file
    for distro in distros:
        d = Distro.get(distro)
        if options.target is None:
            dists = d.config("dists")
        else:
            dists = [target_dist]
        for dist in dists:
            if options.target is None:
                components = d.config('components')
            else:
                components = [target_component]
            for component in components:
                for source in d.newestSources(dist, component):
                    if options.package is not None \
                           and source["Package"] not in options.package:
                        continue
                    if not PACKAGELISTS.check_any_distro(distro, dist, source["Package"]):
                        continue

                    pkg = d.package(dist, component, source['Package'])
                    sources = pkg.getSources()
                    version_sort(sources)

                    last = None
                    try:
                        for this in sources:
                            try:
                                generate_diff(distro, last, this)
                            finally:
                                if last is not None:
                                    cleanup_source(last)

                            last = this
                    finally:
                        if last is not None:
                            cleanup_source(last)

def generate_diff(distro, last, this):
    """Generate the differences."""
    logging.debug("%s: %s %s", distro, this["Package"], this["Version"])

    changes_filename = changes_file(distro, this)
    if not os.path.isfile(changes_filename) \
            and not os.path.isfile(changes_filename + ".bz2"):
        unpack_source(this)
        try:
            save_changes_file(changes_filename, this, last)
            logging.info("Saved changes file: %s",
                          tree.subdir(ROOT, changes_filename))
        except (ValueError, OSError):
            logging.error("dpkg-genchanges for %s failed",
                          tree.subdir(ROOT, changes_filename))

    if last is None:
        return

    diff_filename = diff_file(distro, this)
    if not os.path.isfile(diff_filename) \
            and not os.path.isfile(diff_filename + ".bz2"):
        unpack_source(this)
        unpack_source(last)
        save_patch_file(diff_filename, last, this)
        save_basis(diff_filename, last["Version"])
        logging.info("Saved diff file: %s", tree.subdir(ROOT, diff_filename))


if __name__ == "__main__":
    run(main, options, usage="%prog [DISTRO...]",
        description="generate changes and diff files for new packages")
