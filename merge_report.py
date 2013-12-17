#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# Copyright © 2008 Canonical Ltd.
# Copyright © 2013 Collabora Ltd.
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

import json
import os
import re
import time
from collections import (OrderedDict)
from textwrap import fill

import config
from deb.controlfile import ControlFile
from deb.version import (Version)
from model import (Distro, PackageVersion)
from momlib import files
from util import tree

class MergeResult(str):
    def __new__(cls, s):
        s = s.upper()

        if s in cls.__dict__:
            return cls.__dict__[s]

        raise ValueError('Not a MergeResult: %r' % s)

    def __repr__(self):
        return 'MergeResult(%r)' % str(self)

# We have to bypass MergeResult.__new__ here, to avoid chicken/egg:
# we're ensuring that there are constants in MergeResult.__dict__ so
# that MergeResult.__new__ will work :-)
MergeResult.UNKNOWN = str.__new__(MergeResult, 'UNKNOWN')
MergeResult.NO_BASE = str.__new__(MergeResult, 'NO_BASE')
MergeResult.SYNC_THEIRS = str.__new__(MergeResult, 'SYNC_THEIRS')
MergeResult.KEEP_OURS = str.__new__(MergeResult, 'KEEP_OURS')
MergeResult.FAILED = str.__new__(MergeResult, 'FAILED')
MergeResult.MERGED = str.__new__(MergeResult, 'MERGED')
MergeResult.CONFLICTS = str.__new__(MergeResult, 'CONFLICTS')

def read_report(output_dir):
    """Read the report to determine the versions that went into it."""

    report = {
        "source_package": None,
        "base_version": None,
        "base_files": [],
        "left_distro": None,
        "left_version": None,
        "left_files": [],
        "right_distro": None,
        "right_version": None,
        "right_files": [],
        "merged_is_right": False,
        "merged_dir": None,
        "merged_files": [],
        "build_metadata_changed": True,
        "committed": False
    }

    filename = "%s/REPORT" % output_dir

    if os.path.isfile(filename + '.json'):
        with open(filename + '.json') as r:
            report.update(json.load(r))
    elif os.path.isfile(filename):
        _read_report_text(output_dir, filename, report)
    else:
        raise ValueError, "No report exists"

    if (report['source_package'] is None or
            report["left_version"] is None or report["right_version"] is None or
            report["left_distro"] is None or report["right_distro"] is None):
        raise AttributeError("Insufficient detail in report")

    # this logic is a bit weird but whatever
    if report["merged_is_right"]:
        report["merged_dir"] = ""
        report["merged_files"] = report["right_files"]
    else:
        report["merged_dir"] = output_dir

    # promote versions to Version objects
    for k in ("left_version", "right_version", "base_version",
            "merged_version"):
        if report.get(k) is not None:
            report[k] = Version(report[k])

    # backwards compat
    report["package"] = report["source_package"]
    return report

def _read_report_text(output_dir, filename, report):
    """Read an old-style semi-human-readable REPORT."""

    with open(filename) as r:
        report["source_package"] = next(r).strip()
        in_list = None
        for line in r:
            if line.startswith("    "):
                if in_list == "base":
                    report["base_files"].append(line.strip())
                elif in_list == "left":
                    report["left_files"].append(line.strip())
                elif in_list == "right":
                    report["right_files"].append(line.strip())
                elif in_list == "merged":
                    report["merged_files"].append(line.strip())
            else:
                in_list = None

            if line.startswith("base:"):
                report["base_version"] = Version(line[5:].strip())
                in_list = "base"
            elif line.startswith("our distro "):
                m = re.match("our distro \(([^)]+)\): (.+)", line)
                if m:
                    report["left_distro"] = m.group(1)
                    report["left_version"] = Version(m.group(2).strip())
                    in_list = "left"
            elif line.startswith("source distro "):
                m = re.match("source distro \(([^)]+)\): (.+)", line)
                if m:
                    report["right_distro"] = m.group(1)
                    report["right_version"] = Version(m.group(2).strip())
                    in_list = "right"
            elif line.startswith("generated:"):
                in_list = "merged"
            elif line.startswith("Merged without changes: YES"):
                report["merged_is_right"] = True
            elif line.startswith("Build-time metadata changed: NO"):
                report["build_metadata_changed"] = False
            elif line.startswith("Merge committed: YES"):
                report["committed"] = True

    return report

def write_report(left, left_patch,
                 base, tried_bases,
                 right, right_patch,
                 merged_version, conflicts, src_file, patch_file, output_dir,
                 merged_dir, merged_is_right, build_metadata_changed):
    """Write the merge report."""

    package = left.package.name
    assert package == right.package.name, (package, right.package.name)

    assert isinstance(left, PackageVersion)
    left_source = left.getSources()
    left_distro = left.package.distro.name

    if base is None:
        base_source = None
    else:
        assert isinstance(base, PackageVersion)
        base_source = base.getSources()

    assert isinstance(right, PackageVersion)
    right_source = right.getSources()
    right_distro = right.package.distro.name

    filename = "%s/REPORT" % output_dir
    tree.ensure(filename)
    with open(filename, "w") as report:
        # Package and time
        print >>report, "%s" % package
        print >>report, "%s" % time.ctime()
        print >>report

        # General rambling
        print >>report, fill("Below now follows the report of the automated "
                             "merge of the %s changes to the %s source "
                             "package against the new %s version."
                             % (left_distro.title(), package,
                                right_distro.title()))
        print >>report
        print >>report, fill("This file is designed to be both human readable "
                             "and machine-parseable.  Any line beginning with "
                             "four spaces is a file that should be downloaded "
                             "for the complete merge set.")
        print >>report
        print >>report

        print >>report, fill("Here are the particulars of the three versions "
                             "of %s that were chosen for the merge.  The base "
                             "is the newest version that is a common ancestor "
                             "of both the %s and %s packages.  It may be of "
                             "a different upstream version, but that's not "
                             "usually a problem."
                             % (package, left_distro.title(),
                                right_distro.title()))
        print >>report
        print >>report, fill("The files are the source package itself, and "
                             "the patch from the common base to that version.")
        print >>report

        # Base version and files
        if tried_bases:
            # We print this even if base_source is not None: we want to
            # record the better base versions we tried and failed to find
            print >>report, "missing base version(s):"
            for v in tried_bases:
                print >>report, " %s" % v

        if base_source is not None:
            print >>report, "base: %s" % base_source["Version"]
            for md5sum, size, name in files(base_source):
                print >>report, "    %s" % name
        print >>report

        # Left version and files
        print >>report, "our distro (%s): %s" % (left_distro, left_source["Version"])
        for md5sum, size, name in files(left_source):
            print >>report, "    %s" % name
        print >>report
        if left_patch is not None:
            print >>report, "base -> %s" % left_distro
            print >>report, "    %s" % left_patch
            print >>report

        # Right version and files
        print >>report, "source distro (%s): %s" % (right_distro, right_source["Version"])
        for md5sum, size, name in files(right_source):
            print >>report, "    %s" % name
        print >>report
        if right_patch is not None:
            print >>report, "base -> %s" % right_distro
            print >>report, "    %s" % right_patch
            print >>report

        # Generated section
        print >>report
        print >>report, "Generated Result"
        print >>report, "================"
        print >>report
        if base_source is None:
            print >>report, fill("Failed to merge because the base version "
                                 "required for a 3-way diff is missing from %s pool. "
                                 "You will need to either merge manually; or add the "
                                 "missing base version sources to '%s/%s/*/%s/' and run "
                                 "update_sources.py."
                                 % (right_distro, config.get('ROOT'), right_distro, package))
            print >>report
        elif merged_is_right:
            print >>report, fill("The %s version supercedes the %s version "
                                 "and can be added to %s with no changes." %
                                 (right_distro.title(), left_distro.title(),
                                  left_distro.title()))
            print >>report
            print >>report, "Merged without changes: YES"
            print >>report
            if build_metadata_changed:
                print >>report, "Build-time metadata changed: NO"
                print >>report
        else:
            if src_file.endswith(".dsc"):
                print >>report, fill("No problems were encountered during the "
                                    "merge, so a source package has been "
                                    "produced along with a patch containing "
                                    "the differences from the %s version to the "
                                    "new version." % right_distro.title())
                print >>report
                print >>report, fill("You should compare the generated patch "
                                    "against the patch for the %s version "
                                    "given above and ensure that there are no "
                                    "unexpected changes.  You should also "
                                    "sanity check the source package."
                                    % left_distro.title())
                print >>report

                print >>report, "generated: %s" % merged_version

                # Files from the dsc
                dsc = ControlFile("%s/%s" % (output_dir, src_file),
                                multi_para=False, signed=True).para
                print >>report, "    %s" % src_file
                for md5sum, size, name in files(dsc):
                    print >>report, "    %s" % name
                print >>report
                if patch_file is not None:
                    print >>report, "%s -> generated" % right_distro
                    print >>report, "    %s" % patch_file
                    print >>report
                if build_metadata_changed:
                    print >>report, "Build-time metadata changed: NO"
                    print >>report
            else:
                print >>report, fill("Due to conflict or error, it was not "
                                    "possible to automatically create a source "
                                    "package.  Instead the result of the merge "
                                    "has been placed into the following tar file "
                                    "which you will need to turn into a source "
                                    "package once the problems have been "
                                    "resolved.")
                print >>report
                print >>report, "    %s" % src_file
                print >>report

            if len(conflicts):
                print >>report
                print >>report, "Conflicts"
                print >>report, "========="
                print >>report
                print >>report, fill("In one or more cases, there were different "
                                    "changes made in both %s and %s to the same "
                                    "file; these are known as conflicts."
                                    % (left_distro.title(), right_distro.title()))
                print >>report
                print >>report, fill("It is not possible for these to be "
                                    "automatically resolved, so this source "
                                    "needs human attention.")
                print >>report
                print >>report, fill("Those files marked with 'C ' contain diff3 "
                                    "conflict markers, which can be resolved "
                                    "using the text editor of your choice.  "
                                    "Those marked with 'C*' could not be merged "
                                    "that way, so you will find .%s and .%s "
                                    "files instead and should chose one of them "
                                    "or a combination of both, moving it to the "
                                    "real filename and deleting the other."
                                    % (left_distro.upper(), right_distro.upper()))
                print >>report

                conflicts.sort()
                for name in conflicts:
                    if os.path.isfile("%s/%s" % (merged_dir, name)):
                        print >>report, "  C  %s" % name
                    else:
                        print >>report, "  C* %s" % name
                print >>report

            if merged_version.revision is not None \
                and Version(left_source["Version"]).upstream != merged_version.upstream:
                sa_arg = " -sa"
            else:
                sa_arg = ""

            print >>report
            print >>report, fill("Once you have a source package you are happy "
                                "to upload, you should make sure you include "
                                "the orig.tar.gz if appropriate and information "
                                "about all the versions included in the merge.")
            print >>report
            print >>report, fill("Use the following command to generate a "
                                "correct .changes file:")
            print >>report
            print >>report, "  $ dpkg-genchanges -S -v%s%s" \
                % (left_source["Version"], sa_arg)

    # Use an OrderedDict to make the report more human-readable, and
    # provide pseudo-comments to clarify
    report = OrderedDict()
    report["source_package"] = package
    report["merge_date"] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    # reserve slots here for the result: we'll override it later
    report["#result"] = "???"
    report["result"] = MergeResult.UNKNOWN

    report["#left"] = "'our' version"
    report["left_distro"] = left_distro
    report["left_component"] = left.package.component
    report["left_version"] = str(left.version)
    report["left_files"] = [f[2] for f in files(left_source)]
    if left_patch is not None:
        report["#left_patch"] = "diff(base version ... left version)"
        report["left_patch"] = left_patch

    if tried_bases:
        report["#bases_not_found"] = "these common ancestors were unavailable"
        report["bases_not_found"] = tried_bases
    report["#base"] = "common ancestor of 'left' and 'right'"
    if base is not None:
        report["base_version"] = str(base.version)
        report["base_distro"] = base.package.distro.name
        report["base_files"] = [f[2] for f in files(base_source)]

    report["#right"] = "'their' version"
    report["right_distro"] = right_distro
    report["right_component"] = right.package.component
    report["right_version"] = str(right.version)
    report["right_files"] = [f[2] for f in files(right_source)]
    if right_patch is not None:
        report["#right_patch"] = "diff(base version ... right version)"
        report["right_patch"] = right_patch

    if merged_version is not None:
        report["merged_version"] = str(merged_version)

    report["merged_dir"] = output_dir

    if base is None:
        # this replaces the earlier result, and goes in the same position
        report["#result"] = ("Failed to merge because the base version " +
                "required for a 3-way merge is missing from the pool.")
        report["result"] = MergeResult.NO_BASE

    elif merged_is_right:
        assert not conflicts
        report["#result"] = ("Right version supersedes the left version " +
                "and can be added to the left (target) distro with no " +
                "changes.")
        report["result"] = MergeResult.SYNC_THEIRS
    elif src_file is None:
        report["#result"] = "Unexpected failure, no output"
        report["result"] = MergeResult.FAILED
        report["merged_files"] = report["right_files"]
        report["merged_dir"] = ""
    elif src_file.endswith(".dsc"):
        assert not conflicts
        dsc = ControlFile("%s/%s" % (output_dir, src_file),
                        multi_para=False, signed=True).para
        report["#result"] = "Merge appears to have been successful"
        report["result"] = MergeResult.MERGED
        report["merged_files"] = [f[2] for f in files(dsc)]
        if patch_file is not None:
            report["#merged_patch"] = "diff(left ... merged) for review"
            report["merged_patch"] = patch_file
        report["build_metadata_changed"] = bool(build_metadata_changed)
    else:
        report["merge_failure_tarball"] = src_file

        if conflicts:
            report["#result"] = "3-way merge encountered conflicts"
            report["result"] = MergeResult.CONFLICTS
        else:
            report["#result"] = "merge failed somehow, a tarball was produced"
            report["result"] = MergeResult.FAILED

    if conflicts:
        report["conflicts"] = sorted(conflicts)

    if report["result"] in (MergeResult.CONFLICTS, MergeResult.FAILED,
            MergeResult.MERGED):
        report["#genchanges"] = ("Pass these arguments to dpkg-genchanges, " +
                "dpkg-buildpackage or debuild when you have completed " +
                "the merge")
        if (merged_version is None or
                (merged_version.revision is not None and
                    left.version.upstream != merged_version.upstream)):
            maybe_sa = ' -sa'
        else:
            maybe_sa = ''
        report["genchanges"] = "-S -v%s%s" % (left.version, maybe_sa)

    report["committed"] = False

    filename = "%s/REPORT.json" % output_dir
    tree.ensure(filename)
    with open(filename + '.tmp', "w") as fh:
        json.dump(report, fh, indent=2, sort_keys=False)
    os.rename(filename + '.tmp', filename)
