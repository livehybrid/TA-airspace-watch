"""
Path bootstrap for the TA-airspace-watch add-on. Imported at the top of
every script in package/bin/ to make the vendored libraries in package/lib/
importable from inside Splunk's Python runtime, and to keep the path clean
of other apps' bin directories.
"""

import os
import re
import sys
from os.path import dirname

ta_name = "TA-airspace-watch"
pattern = re.compile(r"[\\/]etc[\\/]apps[\\/][^\\/]+[\\/]bin[\\/]?$")
new_paths = [p for p in sys.path if not pattern.search(p) or ta_name in p]
new_paths.insert(0, os.path.join(dirname(dirname(__file__)), "lib"))
new_paths.insert(0, os.path.sep.join([os.path.dirname(__file__), ta_name]))
sys.path = new_paths
