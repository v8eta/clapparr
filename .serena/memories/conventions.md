# clapparr — conventions

- Tests are standalone scripts, not pytest: module-level `PASS = FAIL = 0`
  counters (breakarr's sibling convention uses a `FAILED = []` list instead —
  same spirit, different counter shape). Run directly: `python3 clapparr/test_X.py`.
- Test files load `plugin.py` via `importlib.util.spec_from_file_location`
  rather than a normal package import — clapparr is not installed as a
  package; this mirrors how Dispatcharr's own plugin loader loads it at
  runtime, so tests exercise the same load path.
- Security-sensitive detail worth remembering: the webhook code must strip
  credentials OUT of a URL into a header before building the request —
  `urllib`'s own exceptions quote the URL back verbatim, so a credential left
  in the URL leaks into logs on any request failure (`test_webhook.py`'s own
  stated rationale).
