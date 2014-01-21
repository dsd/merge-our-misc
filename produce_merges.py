#!/usr/bin/env python
# -*- coding: utf-8 -*-
# produce-merges.py - produce merged packages
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
import re
import time
import logging
import tempfile

from stat import *

from momlib import *
from deb.controlfile import ControlFile
from deb.version import Version
from generate_patches import generate_patch
from util import tree, shell, run
from merge_report import (MergeResult, MergeReport, read_report, write_report)
from momversion import VERSION
import config
import model.error

# Regular expression for top of debian/changelog
CL_RE = re.compile(r'^(\w[-+0-9a-z.]*) \(([^\(\) \t]+)\)((\s+[-0-9a-z]+)+)\;',
                   re.IGNORECASE)

logger = logging.getLogger('produce_merges')

class NoBase(Exception):
    pass

def options(parser):
    parser.add_option("-f", "--force", action="store_true",
                      help="Force creation of merges")

    parser.add_option("-D", "--source-distro", type="string", metavar="DISTRO",
                      default=None,
                      help="Source distribution")
    parser.add_option("-S", "--source-suite", type="string", metavar="SUITE",
                      default=None,
                      help="Source suite (aka distrorelease)")

    parser.add_option("-t", "--target", type="string", metavar="TARGET",
                      default=None,
                      help="Distribution target to use")

    parser.add_option("-V", "--version", type="string", metavar="VER",
                      help="Version to obtain from destination")

    parser.add_option("-X", "--exclude", type="string", metavar="FILENAME",
                      action="append",
                      help="Exclude packages listed in this file")
    parser.add_option("-I", "--include", type="string", metavar="FILENAME",
                      action="append",
                      help="Only process packages listed in this file")

def main(options, args):
    logger.info('Producing merges...')

    excludes = []
    if options.exclude is not None:
        for filename in options.exclude:
            logger.info('excluding packages from %s', filename)
            excludes.extend(read_package_list(filename))

    includes = []
    if options.include is not None:
        for filename in options.include:
            logger.info('including packages from %s', filename)
            includes.extend(read_package_list(filename))

    # For each package in the destination distribution, locate the latest in
    # the source distribution; calculate the base from the destination and
    # produce a merge combining both sets of changes
    for target in config.targets(args):
        logger.info('considering target %s', target)
        our_dist = target.dist
        our_component = target.component
        d = target.distro
        for pkg in d.packages(target.dist, target.component):
          if options.package is not None and pkg.name not in options.package:
            logger.info('skipping package %s: not the selected package',
                    pkg.name)
            continue
          if len(includes) and pkg.name not in includes:
            logger.info('skipping package %s: not in include list', pkg.name)
            continue
          if len(excludes) and pkg.name in excludes:
            logger.info('skipping package %s: in exclude list', pkg.name)
            continue
          if pkg.name in target.blacklist:
            logger.info("%s is blacklisted, skipping", pkg.name)
            continue
          logger.info('considering package %s', pkg.name)
          if options.version:
            our_version = Version(options.version)
            logger.debug('our version: %s (from command line)', our_version)
          else:
            our_version = pkg.newestVersion()
            logger.debug('our version: %s', our_version)
          upstream = None

          for srclist in target.getSourceLists(pkg.name):
            for src in srclist:
              logger.debug('considering source %s', src)
              try:
                for possible in src.distro.findPackage(pkg.name,
                    searchDist=src.dist):
                  logger.debug('- contains version %s', possible)
                  if upstream is None or possible > upstream:
                    logger.debug('  - that version is the best yet seen')
                    upstream = possible
              except model.error.PackageNotFound:
                pass

          output_dir = result_dir(target.name, pkg.name)

          if upstream is None:
            logger.info("%s not available upstream, skipping", our_version)
            cleanup(output_dir)
            report = MergeReport(left=our_version)
            report.result = MergeResult.KEEP_OURS
            report.merged_version = our_version.version
            report.write_report(output_dir)
            continue

          try:
            report = read_report(output_dir)
            if (Version(report['right_version']) == upstream.version and
                    Version(report['left_version']) == our_version.version and
                    # we'll retry the merge if there was an unexpected
                    # failure, a missing base or an unknown result last time
                    report['result'] in (MergeResult.KEEP_OURS,
                        MergeResult.SYNC_THEIRS, MergeResult.MERGED,
                        MergeResult.CONFLICTS)):
              logger.info("merge for %s [ours=%s, theirs=%s] already produced, skipping run", pkg, our_version.version, upstream.version)
              continue
          except (AttributeError, ValueError, KeyError):
            pass

          if our_version >= upstream:
            logger.info("our version %s >= their version %s, skipping",
                    our_version, upstream)
            cleanup(output_dir)
            report = MergeReport(left=our_version, right=upstream)
            report.result = MergeResult.KEEP_OURS
            report.merged_version = our_version.version
            report.write_report(output_dir)
            continue

          logger.info("local: %s, upstream: %s", our_version, upstream)

          try:
            produce_merge(target, our_version, upstream, output_dir)
          except ValueError as e:
            logger.exception("Could not produce merge, perhaps %s changed components upstream?", pkg)
            report = MergeReport(left=our_version, right=upstream)
            report.result = MergeResult.FAILED
            report.message = 'Could not produce merge: %s' % e
            report.write_report(output_dir)
            continue

def is_build_metadata_changed(left_source, right_source):
    """Return true if the two sources have different build-time metadata."""
    for field in ["Binary", "Architecture", "Build-Depends", "Build-Depends-Indep", "Build-Conflicts", "Build-Conflicts-Indep"]:
        if field in left_source and field not in right_source:
            return True
        if field not in left_source and field in right_source:
            return True
        if field in left_source and field in right_source and left_source[field] != right_source[field]:
            return True

    return False


def do_merge(left_dir, left_name, left_distro, base_dir,
             right_dir, right_name, right_distro, merged_dir):
    """Do the heavy lifting of comparing and merging."""
    logger.debug("Producing merge in %s", tree.subdir(ROOT, merged_dir))
    conflicts = []
    po_files = []

    # Look for files in the base and merge them if they're in both new
    # files (removed files get removed)
    for filename in tree.walk(base_dir):
        if tree.under(".pc", filename):
            # Not interested in merging quilt metadata
            continue

        base_stat = os.lstat("%s/%s" % (base_dir, filename))

        try:
            left_stat = os.lstat("%s/%s" % (left_dir, filename))
        except OSError:
            left_stat = None

        try:
            right_stat = os.lstat("%s/%s" % (right_dir, filename))
        except OSError:
            right_stat = None

        if left_stat is None and right_stat is None:
            # Removed on both sides
            pass

        elif left_stat is None:
            logger.debug("removed from %s: %s", left_distro, filename)
            if not same_file(base_stat, base_dir, right_stat, right_dir,
                             filename):
                # Changed on RHS
                conflict_file(left_dir, left_distro, right_dir, right_distro,
                              merged_dir, filename)
                conflicts.append(filename)

        elif right_stat is None:
            # Removed on RHS only
            logger.debug("removed from %s: %s", right_distro, filename)
            if not same_file(base_stat, base_dir, left_stat, left_dir,
                             filename):
                # Changed on LHS
                conflict_file(left_dir, left_distro, right_dir, right_distro,
                              merged_dir, filename)
                conflicts.append(filename)

        elif S_ISREG(left_stat.st_mode) and S_ISREG(right_stat.st_mode):
            # Common case: left and right are both files
            if handle_file(left_stat, left_dir, left_name, left_distro,
                           right_dir, right_stat, right_name, right_distro,
                           base_stat, base_dir, merged_dir, filename,
                           po_files):
                conflicts.append(filename)

        elif same_file(left_stat, left_dir, right_stat, right_dir, filename):
            # left and right are the same, doesn't matter which we keep
            tree.copyfile("%s/%s" % (right_dir, filename),
                          "%s/%s" % (merged_dir, filename))

        elif same_file(base_stat, base_dir, left_stat, left_dir, filename):
            # right has changed in some way, keep that one
            logger.debug("preserving non-file change in %s: %s",
                          right_distro, filename)
            tree.copyfile("%s/%s" % (right_dir, filename),
                          "%s/%s" % (merged_dir, filename))

        elif same_file(base_stat, base_dir, right_stat, right_dir, filename):
            # left has changed in some way, keep that one
            logger.debug("preserving non-file change in %s: %s",
                          left_distro, filename)
            tree.copyfile("%s/%s" % (left_dir, filename),
                          "%s/%s" % (merged_dir, filename))
        else:
            # all three differ, mark a conflict
            conflict_file(left_dir, left_distro, right_dir, right_distro,
                          merged_dir, filename)
            conflicts.append(filename)

    # Look for files in the left hand side that aren't in the base,
    # conflict if new on both sides or copy into the tree
    for filename in tree.walk(left_dir):
        if tree.under(".pc", filename):
            # Not interested in merging quilt metadata
            continue

        if tree.exists("%s/%s" % (base_dir, filename)):
            continue

        if not tree.exists("%s/%s" % (right_dir, filename)):
            logger.debug("new in %s: %s", left_distro, filename)
            tree.copyfile("%s/%s" % (left_dir, filename),
                          "%s/%s" % (merged_dir, filename))
            continue

        left_stat = os.lstat("%s/%s" % (left_dir, filename))
        right_stat = os.lstat("%s/%s" % (right_dir, filename))

        if S_ISREG(left_stat.st_mode) and S_ISREG(right_stat.st_mode):
            # Common case: left and right are both files
            if handle_file(left_stat, left_dir, left_name, left_distro,
                           right_dir, right_stat, right_name, right_distro,
                           None, None, merged_dir, filename,
                           po_files):
                conflicts.append(filename)

        elif same_file(left_stat, left_dir, right_stat, right_dir, filename):
            # left and right are the same, doesn't matter which we keep
            tree.copyfile("%s/%s" % (right_dir, filename),
                          "%s/%s" % (merged_dir, filename))

        else:
            # they differ, mark a conflict
            conflict_file(left_dir, left_distro, right_dir, right_distro,
                          merged_dir, filename)
            conflicts.append(filename)

    # Copy new files on the right hand side only into the tree
    for filename in tree.walk(right_dir):
        if tree.under(".pc", filename):
            # Not interested in merging quilt metadata
            continue

        if tree.exists("%s/%s" % (base_dir, filename)):
            continue

        if tree.exists("%s/%s" % (left_dir, filename)):
            continue

        logger.debug("new in %s: %s", right_distro, filename)
        tree.copyfile("%s/%s" % (right_dir, filename),
                      "%s/%s" % (merged_dir, filename))

    # Handle po files separately as they need special merging
    for filename in po_files:
        if merge_po(left_dir, right_dir, merged_dir, filename):
            conflict_file(left_dir, left_distro, right_dir, right_distro,
                          merged_dir, filename)
            conflicts.append(filename)
            continue

        merge_attr(base_dir, left_dir, right_dir, merged_dir, filename)

    return conflicts

def handle_file(left_stat, left_dir, left_name, left_distro,
                right_dir, right_stat, right_name, right_distro,
                base_stat, base_dir, merged_dir, filename, po_files):
    """Handle the common case of a file in both left and right."""
    if filename == "debian/changelog":
        # two-way merge of changelogs
        try:
          merge_changelog(left_dir, right_dir, merged_dir, filename)
        except:
          return True
    elif filename.endswith(".po") and not \
            same_file(left_stat, left_dir, right_stat, right_dir, filename):
        # two-way merge of po contents (do later)
        po_files.append(filename)
        return False
    elif filename.endswith(".pot") and not \
            same_file(left_stat, left_dir, right_stat, right_dir, filename):
        # two-way merge of pot contents
        if merge_pot(left_dir, right_dir, merged_dir, filename):
            conflict_file(left_dir, left_distro, right_dir, right_distro,
                          merged_dir, filename)
            return True
    elif base_stat is not None and S_ISREG(base_stat.st_mode):
        # was file in base: diff3 possible
        if merge_file(left_dir, left_name, left_distro, base_dir,
                      right_dir, right_name, right_distro, merged_dir,
                      filename):
            return True
    elif same_file(left_stat, left_dir, right_stat, right_dir, filename):
        # same file in left and right
        logger.debug("%s and %s both turned into same file: %s",
                      left_distro, right_distro, filename)
        tree.copyfile("%s/%s" % (left_dir, filename),
                      "%s/%s" % (merged_dir, filename))
    else:
        # general file conflict
        conflict_file(left_dir, left_distro, right_dir, right_distro,
                      merged_dir, filename)
        return True

    # Apply permissions
    merge_attr(base_dir, left_dir, right_dir, merged_dir, filename)
    return False

def same_file(left_stat, left_dir, right_stat, right_dir, filename):
    """Are two filesystem objects the same?"""
    if S_IFMT(left_stat.st_mode) != S_IFMT(right_stat.st_mode):
        # Different fundamental types
        return False
    elif S_ISREG(left_stat.st_mode):
        # Files with the same size and MD5sum are the same
        if left_stat.st_size != right_stat.st_size:
            return False
        elif md5sum("%s/%s" % (left_dir, filename)) \
                 != md5sum("%s/%s" % (right_dir, filename)):
            return False
        else:
            return True
    elif S_ISDIR(left_stat.st_mode) or S_ISFIFO(left_stat.st_mode) \
             or S_ISSOCK(left_stat.st_mode):
        # Directories, fifos and sockets are always the same
        return True
    elif S_ISCHR(left_stat.st_mode) or S_ISBLK(left_stat.st_mode):
        # Char/block devices are the same if they have the same rdev
        if left_stat.st_rdev != right_stat.st_rdev:
            return False
        else:
            return True
    elif S_ISLNK(left_stat.st_mode):
        # Symbolic links are the same if they have the same target
        if os.readlink("%s/%s" % (left_dir, filename)) \
               != os.readlink("%s/%s" % (right_dir, filename)):
            return False
        else:
            return True
    else:
        return True


def merge_changelog(left_dir, right_dir, merged_dir, filename):
    """Merge a changelog file."""
    logger.debug("Knitting %s", filename)

    left_cl = read_changelog("%s/%s" % (left_dir, filename))
    right_cl = read_changelog("%s/%s" % (right_dir, filename))
    tree.ensure(filename)

    with open("%s/%s" % (merged_dir, filename), "w") as output:
        for right_ver, right_text in right_cl:
            while len(left_cl) and left_cl[0][0] > right_ver:
                (left_ver, left_text) = left_cl.pop(0)
                print >>output, left_text

            while len(left_cl) and left_cl[0][0] == right_ver:
                (left_ver, left_text) = left_cl.pop(0)

            print >>output, right_text

        for left_ver, left_text in left_cl:
            print >>output, left_text

    return False

def read_changelog(filename):
    """Return a parsed changelog file."""
    entries = []

    with open(filename) as cl:
        (ver, text) = (None, "")
        for line in cl:
            match = CL_RE.search(line)
            if match:
                try:
                    ver = Version(match.group(2))
                except ValueError:
                    ver = None

                text += line
            elif line.startswith(" -- "):
                if ver is None:
                    ver = Version("0")

                text += line
                entries.append((ver, text))
                (ver, text) = (None, "")
            elif len(line.strip()) or ver is not None:
                text += line

    if len(text):
        entries.append((ver, text))

    return entries


def merge_po(left_dir, right_dir, merged_dir, filename):
    """Update a .po file using msgcat or msgmerge."""
    merged_po = "%s/%s" % (merged_dir, filename)
    closest_pot = find_closest_pot(merged_po)
    if closest_pot is None:
        return merge_pot(left_dir, right_dir, merged_dir, filename)

    left_po = "%s/%s" % (left_dir, filename)
    right_po = "%s/%s" % (right_dir, filename)

    logger.debug("Merging PO file %s", filename)
    try:
        tree.ensure(merged_po)
        shell.run(("msgmerge", "--force-po", "-o", merged_po,
                   "-C", left_po, right_po, closest_pot))
    except (ValueError, OSError):
        logger.error("PO file merge failed: %s", filename)
        return True

    return False

def merge_pot(left_dir, right_dir, merged_dir, filename):
    """Update a .po file using msgcat."""
    merged_pot = "%s/%s" % (merged_dir, filename)

    left_pot = "%s/%s" % (left_dir, filename)
    right_pot = "%s/%s" % (right_dir, filename)

    logger.debug("Merging POT file %s", filename)
    try:
        tree.ensure(merged_pot)
        shell.run(("msgcat", "--force-po", "--use-first", "-o", merged_pot,
                   right_pot, left_pot))
    except (ValueError, OSError):
        logger.error("POT file merge failed: %s", filename)
        return True

    return False

def find_closest_pot(po_file):
    """Find the closest .pot file to the po file given."""
    dirname = os.path.dirname(po_file)
    for entry in os.listdir(dirname):
        if entry.endswith(".pot"):
            return os.path.join(dirname, entry)
    else:
        return None


def merge_file(left_dir, left_name, left_distro, base_dir,
               right_dir, right_name, right_distro, merged_dir, filename):
    """Merge a file using diff3."""
    dest = "%s/%s" % (merged_dir, filename)
    tree.ensure(dest)

    with open(dest, "w") as output:
        status = shell.run(("diff3", "-E", "-m",
                            "-L", left_name, "%s/%s" % (left_dir, filename),
                            "-L", "BASE", "%s/%s" % (base_dir, filename),
                            "-L", right_name, "%s/%s" % (right_dir, filename)),
                           stdout=output, okstatus=(0,1,2))

    if status != 0:
        if not tree.exists(dest) or os.stat(dest).st_size == 0:
            # Probably binary
            if same_file(os.stat("%s/%s" % (left_dir, filename)), left_dir,
                         os.stat("%s/%s" % (right_dir, filename)), right_dir,
                         filename):
                logger.debug("binary files are the same: %s", filename)
                tree.copyfile("%s/%s" % (left_dir, filename),
                              "%s/%s" % (merged_dir, filename))
            elif same_file(os.stat("%s/%s" % (base_dir, filename)), base_dir,
                           os.stat("%s/%s" % (left_dir, filename)), left_dir,
                           filename):
                logger.debug("preserving binary change in %s: %s",
                              right_distro, filename)
                tree.copyfile("%s/%s" % (right_dir, filename),
                              "%s/%s" % (merged_dir, filename))
            elif same_file(os.stat("%s/%s" % (base_dir, filename)), base_dir,
                           os.stat("%s/%s" % (right_dir, filename)), right_dir,
                           filename):
                logger.debug("preserving binary change in %s: %s",
                              left_distro, filename)
                tree.copyfile("%s/%s" % (left_dir, filename),
                              "%s/%s" % (merged_dir, filename))
            else:
                logger.debug("binary file conflict: %s", filename)
                conflict_file(left_dir, left_distro, right_dir, right_distro,
                              merged_dir, filename)
                return True
        else:
            logger.debug("Conflict in %s", filename)
            return True
    else:
        return False


def merge_attr(base_dir, left_dir, right_dir, merged_dir, filename):
    """Set initial and merge changed attributes."""
    if base_dir is not None \
           and os.path.isfile("%s/%s" % (base_dir, filename)) \
           and not os.path.islink("%s/%s" % (base_dir, filename)):
        set_attr(base_dir, merged_dir, filename)
        apply_attr(base_dir, left_dir, merged_dir, filename)
        apply_attr(base_dir, right_dir, merged_dir, filename)
    else:
        set_attr(right_dir, merged_dir, filename)
        apply_attr(right_dir, left_dir, merged_dir, filename)

def set_attr(src_dir, dest_dir, filename):
    """Set the initial attributes."""
    mode = os.stat("%s/%s" % (src_dir, filename)).st_mode & 0777
    os.chmod("%s/%s" % (dest_dir, filename), mode)

def apply_attr(base_dir, src_dir, dest_dir, filename):
    """Apply attribute changes from one side to a file."""
    src_stat = os.stat("%s/%s" % (src_dir, filename))
    base_stat = os.stat("%s/%s" % (base_dir, filename))

    for shift in range(0, 9):
        bit = 1 << shift

        # Permission bit added
        if not base_stat.st_mode & bit and src_stat.st_mode & bit:
            change_attr(dest_dir, filename, bit, shift, True)

        # Permission bit removed
        if base_stat.st_mode & bit and not src_stat.st_mode & bit:
            change_attr(dest_dir, filename, bit, shift, False)

def change_attr(dest_dir, filename, bit, shift, add):
    """Apply a single attribute change."""
    logger.debug("Setting %s %s", filename,
                  [ "u+r", "u+w", "u+x", "g+r", "g+w", "g+x",
                    "o+r", "o+w", "o+x" ][shift])

    dest = "%s/%s" % (dest_dir, filename)
    attr = os.stat(dest).st_mode & 0777
    if add:
        attr |= bit
    else:
        attr &= ~bit

    os.chmod(dest, attr)


def conflict_file(left_dir, left_distro, right_dir, right_distro,
                  dest_dir, filename):
    """Copy both files as conflicts of each other."""
    left_src = "%s/%s" % (left_dir, filename)
    right_src = "%s/%s" % (right_dir, filename)
    dest = "%s/%s" % (dest_dir, filename)

    logger.debug("Conflicted: %s", filename)
    tree.remove(dest)

    # We need to take care here .. if one of the items involved in a
    # conflict is a directory then it might have children and we don't want
    # to throw an error later.
    #
    # We get round this by making the directory a symlink to the conflicted
    # one.
    #
    # Fortunately this is so rare it may never happen!

    if tree.exists(left_src):
        tree.copyfile(left_src, "%s.%s" % (dest, left_distro.upper()))
    if os.path.isdir(left_src):
        os.symlink("%s.%s" % (os.path.basename(dest), left_distro.upper()),
                   dest)

    if tree.exists(right_src):
        tree.copyfile(right_src, "%s.%s" % (dest, right_distro.upper()))
    if os.path.isdir(right_src):
        os.symlink("%s.%s" % (os.path.basename(dest), right_distro.upper()),
                   dest)

def add_changelog(package, merged_version, left_distro, left_dist,
                  right_distro, right_dist, merged_dir):
    """Add a changelog entry to the package."""
    changelog_file = "%s/debian/changelog" % merged_dir

    with open(changelog_file) as changelog:
        with open(changelog_file + ".new", "w") as new_changelog:
            print >>new_changelog, ("%s (%s) UNRELEASED; urgency=low"
                                    % (package, merged_version))
            print >>new_changelog
            print >>new_changelog, "  * Merge from %s %s.  Remaining changes:" \
                  % (right_distro.title(), right_dist)
            print >>new_changelog, "    - SUMMARISE HERE"
            print >>new_changelog
            print >>new_changelog, (" -- %s <%s>  " % (MOM_NAME, MOM_EMAIL) +
                                    time.strftime("%a, %d %b %Y %H:%M:%S %z"))
            print >>new_changelog
            for line in changelog:
                print >>new_changelog, line.rstrip("\r\n")

    os.rename(changelog_file + ".new", changelog_file)

def copy_in(output_dir, pkgver):
    """Make a copy of the source files."""

    source = pkgver.getSources()
    pkg = pkgver.package

    for md5sum, size, name in files(source):
        src = "%s/%s/%s" % (ROOT, pkg.poolDirectory().path, name)
        dest = "%s/%s" % (output_dir, name)
        if os.path.isfile(dest):
            os.unlink(dest)
        try:
          logger.debug("%s -> %s", src, dest)
          os.link(src, dest)
        except OSError, e:
          logger.exception("File not found: %s", src)

    patch = patch_file(pkg.distro, source)
    if os.path.isfile(patch):
        output = "%s/%s" % (output_dir, os.path.basename(patch))
        if not os.path.exists(output):
            os.link(patch, output)
        return os.path.basename(patch)
    else:
        return None


def create_tarball(package, version, output_dir, merged_dir):
    """Create a tarball of a merge with conflicts."""
    filename = "%s/%s_%s.src.tar.gz" % (output_dir, package,
                                        version.without_epoch)
    contained = "%s-%s" % (package, version.without_epoch)

    tree.ensure("%s/tmp/" % ROOT)
    parent = tempfile.mkdtemp(dir="%s/tmp/" % ROOT)
    try:
        tree.copytree(merged_dir, "%s/%s" % (parent, contained))

        debian_rules = "%s/%s/debian/rules" % (parent, contained)
        if os.path.isfile(debian_rules):
            os.chmod(debian_rules, os.stat(debian_rules).st_mode | 0111)

        shell.run(("tar", "czf", filename, contained), chdir=parent)

        logger.info("Created %s", tree.subdir(ROOT, filename))
        return os.path.basename(filename)
    finally:
        tree.remove(parent)

def create_source(package, version, since, output_dir, merged_dir):
    """Create a source package without conflicts."""
    contained = "%s-%s" % (package, version.upstream)
    filename = "%s_%s.dsc" % (package, version.without_epoch)

    tree.ensure("%s/tmp/" % ROOT)
    parent = tempfile.mkdtemp(dir="%s/tmp/" % ROOT)
    try:
        tree.copytree(merged_dir, "%s/%s" % (parent, contained))

        orig_filename = "%s_%s.orig.tar.gz" % (package, version.upstream)
        if os.path.isfile("%s/%s" % (output_dir, orig_filename)):
            os.link("%s/%s" % (output_dir, orig_filename),
                    "%s/%s" % (parent, orig_filename))

        cmd = ("dpkg-source",)
        if version.revision is not None and since.upstream != version.upstream:
            cmd += ("-sa",)
        cmd += ("-b", contained)

        try:
            shell.run(cmd, chdir=parent)
        except (ValueError, OSError):
            logger.error("dpkg-source failed")
            return create_tarball(package, version, output_dir, merged_dir)

        if os.path.isfile("%s/%s" % (parent, filename)):
            logger.info("Created dpkg-source %s", filename)
            for name in os.listdir(parent):
                src = "%s/%s" % (parent, name)
                dest = "%s/%s" % (output_dir, name)
                if os.path.isfile(src) and not os.path.isfile(dest):
                    os.link(src, dest)

            return os.path.basename(filename)
        else:
            logger.warning("Dropped dsc %s", tree.subdir(ROOT, filename))
            return create_tarball(package, version, output_dir, merged_dir)
    finally:
        tree.remove(parent)

def create_patch(package, version, output_dir, merged_dir,
                 right_source, right_dir):
    """Create the merged patch."""
    filename = "%s/%s_%s.patch" % (output_dir, package, version)

    parent = tempfile.mkdtemp()
    try:
        tree.copytree(merged_dir, "%s/%s" % (parent, version))
        tree.copytree(right_dir, "%s/%s" % (parent, right_source["Version"]))

        with open(filename, "w") as diff:
            shell.run(("diff", "-pruN",
                       right_source["Version"], "%s" % version),
                      chdir=parent, stdout=diff, okstatus=(0, 1, 2))
            logger.info("Created %s", tree.subdir(ROOT, filename))

        return os.path.basename(filename)
    finally:
        tree.remove(parent)

def read_package_list(filename):
    """Read a list of packages from the given file."""
    packages = []

    with open(filename) as list_file:
        for line in list_file:
            if line.startswith("#"):
                continue

            package = line.strip()
            if len(package):
                packages.append(package)

    return packages

def get_common_ancestor(target, downstream, downstream_versions, upstream,
        upstream_versions, tried_bases):
  logger.debug('looking for common ancestor of %s and %s',
          downstream.version, upstream.version)
  for downstream_version, downstream_text in downstream_versions:
    if downstream_version is None:
      # sometimes read_changelog gets confused
      continue
    for upstream_version, upstream_text in upstream_versions:
      if downstream_version == upstream_version:
        logger.debug('%s looks like a possibility', downstream_version)

        try:
          package_version = target.distro.findPackage(
                  downstream.package.name, searchDist=target.dist,
                  version=downstream_version)[0]
        except model.error.PackageNotFound:
          source_lists = target.getSourceLists(downstream.package.name)
          sources = []
          for sl in source_lists:
            for source in sl:
              sources.append(source)

          for source in sources:
            try:
              package_version = source.distro.findPackage(
                      downstream.package.name,
                      searchDist=source.dist,
                      version=downstream_version)[0]
            except model.error.PackageNotFound:
              continue
            except Exception:
              tried_bases.add(downstream_version)
              logger.debug('unable to find %s in %s:\n',
                      downstream_version, source, exc_info=1)
              # go to next source
              continue
            else:
              # no error finding it in this source
              logger.debug('found %s in source distro', downstream_version)
              break
          else:
            # run out of sources
            tried_bases.add(downstream_version)
            logger.debug('unable to find %s in any source distro',
                downstream_version)
            # go to next version
            continue
        else:
          # no error finding it in the target
          logger.debug('found %s in target distro', downstream_version)

        try:
          target.fetchMissingVersion(package_version.package,
                  package_version.version)
          base_dir = unpack_source(package_version)
        except Exception:
          logger.exception('unable to unpack %s:\n', package_version)
        else:
          logger.debug('base version for %s and %s is %s',
                  downstream, upstream, package_version)
          return (package_version, base_dir)

        tried_bases.add(downstream_version)

  raise NoBase('unable to find a usable base version for %s and %s' %
          (downstream, upstream))

def produce_merge(target, left, upstream, output_dir):

  left_dir = unpack_source(left)
  upstream_dir = unpack_source(upstream)

  report = MergeReport(left=left, right=upstream)
  report.mom_version = str(VERSION)
  report.merge_date = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())

  # Try to find the newest common ancestor
  tried_bases = set()
  try:
    downstream_versions = read_changelog(left_dir + '/debian/changelog')
    upstream_versions = read_changelog(upstream_dir + '/debian/changelog')
    base, base_dir = get_common_ancestor(target,
            left, downstream_versions, upstream, upstream_versions, tried_bases)
  except Exception as e:
    cleanup(output_dir)
    report.bases_not_found = sorted(tried_bases, reverse=True)

    if isinstance(e, NoBase):
      report.result = MergeResult.NO_BASE
      logger.info('%s', e)
    else:
      report.result = MergeResult.FAILED
      logger.exception('error finding base version:\n')

    report.write_report(output_dir)
    return

  report.set_base(base)
  report.bases_not_found = sorted(tried_bases, reverse=True)

  logger.info('base version: %s', base.version)

  generate_patch(base, left.package.distro, left, slipped=False, force=False,
          unpacked=True)
  generate_patch(base, upstream.package.distro, upstream, slipped=False,
          force=False, unpacked=True)

  merged_version = Version(str(upstream.version)+config.get('LOCAL_SUFFIX'))

  if base >= upstream:
    logger.info("Nothing to be done: %s >= %s", base, upstream)
    cleanup(output_dir)
    report.result = MergeResult.KEEP_OURS
    report.write_report(output_dir)
    return

  merged_dir = work_dir(left.package.name, merged_version)

  if base.version == left.version:
    logger.info("Syncing %s to %s", left, upstream)
    cleanup(output_dir)
    if not os.path.isdir(output_dir):
        os.makedirs(output_dir)
    right_patch = copy_in(output_dir, upstream)
    write_report(left, None,
        base, report.bases_not_found,
        upstream, right_patch,
        merged_version, None, None, right_patch,
        output_dir, None, True, False)
    return

  logger.info("Merging %s..%s onto %s", upstream, base, left)

  try:
    conflicts = do_merge(left_dir, left.package.name, left.package.distro.name, base_dir,
                         upstream_dir, upstream.package.name, upstream.package.distro.name,
                         merged_dir)
  except OSError as e:
    cleanup(merged_dir)
    logger.exception("Could not merge %s, probably bad files?", left)
    report.result = MergeResult.FAILED
    report.message = 'Could not merge: %s' % e
    report.write_report(output_dir)
    return

  try:
    add_changelog(left.package.name, merged_version, left.package.distro.name, left.package.dist,
                  upstream.package.distro.name, upstream.package.dist, merged_dir)
  except IOError as e:
    logger.exception("Could not update changelog for %s!", left)
    report.result = MergeResult.FAILED
    report.message = 'Could not update changelog: %s' % e
    report.write_report(output_dir)
    return
  cleanup(output_dir)
  os.makedirs(output_dir)
  copy_in(output_dir, base)
  left_patch = copy_in(output_dir, left)
  right_patch = copy_in(output_dir, upstream)

  patch_file = None
  build_metadata_changed = False

  if len(conflicts):
    src_file = create_tarball(left.package.name, merged_version, output_dir, merged_dir)
  else:
    src_file = create_source(left.package.name, merged_version, left.version, output_dir, merged_dir)
    if src_file.endswith(".dsc"):
      build_metadata_changed = is_build_metadata_changed(left.getSources(), ControlFile("%s/%s" % (output_dir, src_file), signed=True).para)
      patch_file = create_patch(left.package.name, merged_version,
                                output_dir, merged_dir,
                                upstream.getSources(), upstream_dir)
  write_report(left, left_patch,
               base, report.bases_not_found,
               upstream, right_patch,
               merged_version, conflicts, src_file, patch_file,
               output_dir, merged_dir, False, build_metadata_changed)
  logger.info("Wrote output to %s", src_file)
  cleanup(merged_dir)
  cleanup_source(upstream.getSources())
  cleanup_source(base.getSources())
  cleanup_source(left.getSources())

if __name__ == "__main__":
    run(main, options, usage="%prog",
        description="produce merged packages")
