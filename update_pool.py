#!/usr/bin/env python
# -*- coding: utf-8 -*-
# update-pool.py - update a distribution's pool
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

from __future__ import with_statement

import os
import gzip
import urllib
import logging
import tempfile
from contextlib import closing

from momlib import *
from util import tree


def options(parser):
    parser.add_option("-p", "--package", type="string", metavar="PACKAGE",
                      action="append",
                      help="Process only these packages")

def main(options, args):
    if len(args):
        distros = args
    else:
        distros = get_pool_distros()

    # Update target distribution sources and calculate the list of packages we are
    # interested in (no need to download the entire Ubuntu archive...)
    updated_sources = set()
    for target in DISTRO_TARGETS:
        our_distro = DISTRO_TARGETS[target]["distro"]
        our_dist = DISTRO_TARGETS[target]["dist"]
        if our_distro not in distros:
            continue
        if "obs" in DISTROS[our_distro]:
            update_obs(our_distro, options.package)
            updated_sources.add((our_distro, None, None))
            sources = get_sources(our_distro, None, None)
        else:
            sources = []
            for our_component in DISTROS[our_distro]["components"]:
                update_sources(our_distro, our_dist, our_component)
                updated_sources.add((our_distro, our_dist, our_component))
                sources.extend(get_sources(our_distro, our_dist, our_component))
        for source in sources:
            if options.package is not None and source["Package"] not in options.package:
                continue
            if "obs" not in DISTROS[our_distro]:
                update_pool(distro, source)
            PACKAGELISTS.add_if_needed(target, None, source["Package"])
        PACKAGELISTS.save_if_modified(target)

    # Download the current sources for the remaining distributions and download
    # any new contents into our pool
    for distro in distros:
        if "obs" in DISTROS[distro]:
            if (distro, None, None) not in updated_sources:
                update_obs(distro)
                continue
        for dist in DISTROS[distro]["dists"]:
            for component in DISTROS[distro]["components"]:
                if (distro, dist, component) not in updated_sources:
                    update_sources(distro, dist, component)
                sources = get_sources(distro, dist, component)
                for source in sources:
                    if options.package is not None \
                           and source["Package"] not in options.package:
                        continue
                    if not PACKAGELISTS.check_any_distro(distro, dist, source["Package"]):
                        continue
                    update_pool(distro, source)

def update_obs(distro, packages=None):
    """Check out (or update) a distro from OBS, symlink it into the pool, and generate Sources files"""
    if packages is not None:
        for package in packages:
            obs_checkout_or_update(distro, package)
            obs_update_pool(distro, package)
    else:
        obs_checkout_or_update(distro)
        obs_update_pool(distro)

def sources_url(distro, dist, component):
    """Return a URL for a remote Sources.gz file."""
    try:
        return DISTROS[distro]["sources_urls"][(dist, component)]
    except KeyError:
        pass
        
    mirror = DISTROS[distro]["mirror"]
    url = mirror + "/dists"
    if dist is not None:
        url += "/" + dist
    if component is not None:
        url += "/" + component
    return url + "/source/Sources.gz"

def update_sources(distro, dist, component):
    """Update a Sources file."""
    url = sources_url(distro, dist, component)
    filename = sources_file(distro, dist, component)

    logging.debug("Downloading %s", url)

    try:
        if not os.path.isdir(os.path.dirname(filename)):
            os.makedirs(os.path.dirname(filename))
        urllib.URLopener().retrieve(url, filename)
    except IOError:
        logging.error("Downloading %s failed", url)
        raise

    logging.info("Saved %s", tree.subdir(ROOT, filename))
    return filename

def update_pool(distro, source):
    """Download a source package into our pool."""
    mirror = DISTROS[distro]["mirror"]
    sourcedir = source["Directory"]

    pooldir = pool_directory(distro, source["Package"])

    for md5sum, size, name in files(source):
        url = "%s/%s/%s" % (mirror, sourcedir, name)
        filename = "%s/%s/%s" % (ROOT, pooldir, name)

        if os.path.isfile(filename):
            if os.path.getsize(filename) == int(size):
                continue

        logging.debug("Downloading %s", url)
        ensure(filename)
        try:
            urllib.URLopener().retrieve(url, filename)
        except IOError:
            logging.error("Downloading %s failed", url)
            raise
        logging.info("Saved %s", tree.subdir(ROOT, filename))


if __name__ == "__main__":
    run(main, options, usage="%prog [DISTRO...]",
        description="update a distribution's pool")
