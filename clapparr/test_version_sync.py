#!/usr/bin/env python3
"""The version Dispatcharr shows must equal the version the catalog installs.

No API key, no network, no Dispatcharr. Run: python3 test_version_sync.py

⛔ WHY THIS EXISTS. Two sources of truth, nothing comparing them: Dispatcharr
reads Plugin.version out of plugin.py for what it DISPLAYS, while the catalog
and installer read "version" from plugin.json. They drifted silently across two
releases -- v1.5.0 and v1.6.0 both shipped plugin.py saying "1.4.1", inside the
published zips -- because the release step is a hand-built zip with no check.
The symptom is the quiet kind: everything works, and the plugin simply lies
about which build it is, so a bug report names a version nobody can reproduce
against.

Comparing them in a test is the fix. Bumping one without the other now fails
here rather than shipping.
"""
import json
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

fails = []


def check(name, got, want=True):
    ok = (got == want)
    print(("  ok   " if ok else "  FAIL ") + name
          + ("" if ok else "   got %r want %r" % (got, want)))
    if not ok:
        fails.append(name)


with open(os.path.join(_HERE, "plugin.json")) as fh:
    _manifest = json.load(fh)
_json_version = _manifest.get("version")

# ⚠️ Read as TEXT, not by importing. plugin.py imports Dispatcharr internals at
# module scope, so importing it here would make this test fail for a reason
# that has nothing to do with versions -- on a dev box, every time. The
# constant is a plain literal; a regex reads it without executing anything.
with open(os.path.join(_HERE, "plugin.py")) as fh:
    _src = fh.read()
_m = re.search(r"^\s{4}version\s*=\s*[\"']([^\"']+)[\"']", _src, re.M)
_py_version = _m.group(1) if _m else None

print("plugin.json: %r   plugin.py: %r" % (_json_version, _py_version))

# The regex is itself a thing that can silently stop working: if the constant
# is renamed or reindented, `None == None` would make the equality below pass
# while checking nothing. Assert it was actually found first.
check("the version constant is present in plugin.py", _py_version is not None)
check("plugin.json declares a version", bool(_json_version))
check("plugin.py and plugin.json agree on the version",
      _py_version == _json_version)

# Both must look like versions, or "" == "" would satisfy the comparison above.
_semver = re.compile(r"^\d+\.\d+\.\d+$")
check("plugin.json version is dotted numeric",
      bool(_semver.match(_json_version or "")))
check("plugin.py version is dotted numeric",
      bool(_semver.match(_py_version or "")))

# The class attribute must not be shadowed later in the file -- a second
# assignment would mean the one this test reads is not the one that ships.
check("only one version constant is defined",
      len(re.findall(r"^\s{4}version\s*=\s*[\"']", _src, re.M)), 1)

print()
if fails:
    print("FAILED: " + "; ".join(fails))
    sys.exit(1)
print("all version-sync tests passed")
