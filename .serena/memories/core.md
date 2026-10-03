# clapparr — core map

A Dispatcharr plugin ("DVR NFO Generator") that writes Kodi/Plex NFO metadata
sidecars, poster.jpg and per-episode thumbnails for DVR recordings, reading
title/description/season/episode/poster straight off Dispatcharr's own
`Recording` DB row (no separate scrape needed for the common case).

Output layout per show:
```
<show>/tvshow.nfo                 once per show
<show>/poster.jpg
<show>/<recording>.nfo
<show>/<recording>-thumb.jpg
```
Developed/tested against Plex only (agent: Plex TV Series Agent (NFO)); also
read by Kodi/Jellyfin/Emby per the NFO format spec.

## Layout
- `clapparr/plugin.py` — the entire plugin: single `Plugin` class (Dispatcharr
  plugin entrypoint convention — same shape as breakarr's
  `plugin/breakarr_bridge/plugin.py`), plus module functions: `_write`/`_run`
  (main NFO/thumbnail write), `_fuzzy_show_match` + `_TVMAZE_SEARCH` (TVMaze
  lookup fallback when Dispatcharr's own metadata is thin), `_match_dir_owner`
  (which show directory a recording belongs to), `_rewrite_path` (container
  path -> what the media server actually sees).
- `clapparr/plugin.json` — Dispatcharr plugin manifest.
- `clapparr/test_*.py` — `test_fuzzy_match.py`, `test_ownership_plex.py`,
  `test_webhook.py`. See `mem:conventions` for how they're run.
- `registry/`, `assets/` — plugin registry/marketing assets, not core logic.

## Interaction with breakarr/dispatcharr (verified 2026-09-14)
clapparr's only filesystem writes are its own temp-file-then-`os.replace`
idiom on ITS OWN artifacts (thumbnails, plan files) — `plugin.py:216/1143/
1148/1228/1231`. It never touches DVR caption/subtitle sidecars
(`<recording>.subs*.ts`) at all; one comment explicitly says subtitles are
deliberately not carried by clapparr. Safe to treat as fully decoupled from
breakarr's caption pipeline.

See `mem:tech_stack`, `mem:conventions`.
