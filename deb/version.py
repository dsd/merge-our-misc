#!/usr/bin/env python
# -*- coding: utf-8 -*-
# deb/version.py - parse and compare Debian version strings.
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

import re

import config


# Regular expressions make validating things easy
valid_epoch = re.compile(r'^[0-9]+$')
valid_upstream = re.compile(r'^[A-Za-z0-9+:.~-]*$')
valid_revision = re.compile(r'^[A-Za-z0-9+.~]+$')

# Character comparison table for upstream and revision components
cmp_table = "~ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz+-.:"


class Version(object):
    """Debian version number.

    This class is designed to be reasonably transparent and allow you
    to write code like:

    |   s.version >= '1.100-1'

    The comparison will be done according to Debian rules, so '1.2' will
    compare lower.

    Properties:
      epoch       Epoch
      upstream    Upstream version
      revision    Debian/local revision
    """

    def __init__(self, ver):
        """Parse a string or number into the three components."""
        self.epoch = None
        self.upstream = None
        self.revision = None

        ver = str(ver)
        if not len(ver):
            raise ValueError

        # Epoch is component before first colon
        idx = ver.find(":")
        if idx != -1:
            self.epoch = ver[:idx]
            if not len(self.epoch):
                raise ValueError
            if not valid_epoch.search(self.epoch):
                raise ValueError
            self.epoch = int(self.epoch)
            ver = ver[idx+1:]

        # Revision is component after last hyphen
        idx = ver.rfind("-")
        if idx != -1:
            self.revision = ver[idx+1:]
            if not len(self.revision):
                raise ValueError
            if not valid_revision.search(self.revision):
                raise ValueError
            ver = ver[:idx]

        # Remaining component is upstream
        self.upstream = ver
        if not len(self.upstream):
            raise ValueError
        if not valid_upstream.search(self.upstream):
            raise ValueError, "%s is not a valid upstream version"%self.upstream

    def getWithoutEpoch(self):
        """Return the version without the epoch."""
        str = self.upstream
        if self.revision is not None:
            str += "-%s" % (self.revision,)
        return str

    without_epoch = property(getWithoutEpoch)

    def __str__(self):
        """Return the class as a string for printing."""
        str = ""
        if self.epoch is not None:
            str += "%d:" % (self.epoch,)
        str += self.upstream
        if self.revision is not None:
            str += "-%s" % (self.revision,)
        return str

    def __repr__(self):
        """Return a debugging representation of the object."""
        return "<%s epoch: %d, upstream: %r, revision: %r>" \
               % (self.__class__.__name__, self.epoch,
                  self.upstream, self.revision)

    def __cmp__(self, other):
        """Compare two Version classes."""
        other = Version(other)

        result = cmp(self.epoch, other.epoch)
        if result != 0: return result

        result = deb_cmp(self.upstream, other.upstream)
        if result != 0: return result

        result = deb_cmp(self.revision or "", other.revision or "")
        if result != 0: return result

        return 0

    def __hash__(self):
        return hash((self.epoch, self.upstream, self.revision))

    def base(self, slip=False):
        def strip_suffix(text, suffix):
            try:
                idx = text.rindex(suffix)
            except ValueError:
                return text

            for char in text[idx+len(suffix):]:
                if not (char.isdigit() or char == '.'):
                    return text

            return text[:idx]
        v = strip_suffix(str(self), "build")
        if config.get('LOCAL_SUFFIX') is not None:
          v = strip_suffix(v, config.get('LOCAL_SUFFIX'))
        v = strip_suffix(v, "co")
        v = strip_suffix(v, "ubuntu")
        if v.endswith("-"):
            v += "0"
        if slip and v.endswith("-0"):
            v = v[:-2] + "-1"
        return Version(v)


def strcut(str, idx, accept):
    """Cut characters from str that are entirely in accept."""
    ret = ""
    while idx < len(str) and str[idx] in accept:
        ret += str[idx]
        idx += 1

    return (ret, idx)

def deb_order(str, idx):
    """Return the comparison order of two characters."""
    if idx >= len(str):
        return 0
    elif str[idx] == "~":
        return -1
    else:
        return cmp_table.index(str[idx])

def deb_cmp_str(x, y):
    """Compare two strings in a deb version."""
    idx = 0
    while (idx < len(x)) or (idx < len(y)):
        result = deb_order(x, idx) - deb_order(y, idx)
        if result < 0:
            return -1
        elif result > 0:
            return 1

        idx += 1

    return 0

def deb_cmp(x, y):
    """Implement the string comparison outlined by Debian policy."""
    x_idx = y_idx = 0
    while x_idx < len(x) or y_idx < len(y):
        # Compare strings
        (x_str, x_idx) = strcut(x, x_idx, cmp_table)
        (y_str, y_idx) = strcut(y, y_idx, cmp_table)
        result = deb_cmp_str(x_str, y_str)
        if result != 0: return result

        # Compare numbers
        (x_str, x_idx) = strcut(x, x_idx, "0123456789")
        (y_str, y_idx) = strcut(y, y_idx, "0123456789")
        result = cmp(int(x_str or "0"), int(y_str or "0"))
        if result != 0: return result

    return 0
