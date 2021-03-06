# "packages cannot be executed directly", see __main__.py for main()

import os
import logging
import shutil
import tempfile
import shutil
import atexit

import update_sources

os.environ['MOM_TEST'] = '1'

if 'MOM_TEST_DEBUG' in os.environ:
    logging.basicConfig(level=logging.DEBUG)

# Prevent the tests from contacting snapshot.debian.org, also provide a place
# where we can simulate a debsnap server for tests
debsnap_base = tempfile.mkdtemp(prefix='momtest.debsnap.')
update_sources.SNAPSHOT_BASE = 'file://' + debsnap_base
if 'MOM_TEST_NO_CLEANUP' not in os.environ:
  atexit.register(shutil.rmtree, debsnap_base)
