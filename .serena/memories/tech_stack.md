# clapparr — tech stack

- Python 3, stdlib-heavy (urllib for the TVMaze lookup and the post-write
  webhook — no `requests` dependency observed).
- Runs INSIDE the Dispatcharr container as a plugin (`plugin.json` +
  `plugin.py`), not a standalone service.
- No database of its own — reads Dispatcharr's `Recording` row directly.
