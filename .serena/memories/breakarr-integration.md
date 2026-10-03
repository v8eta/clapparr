# Clapparr ↔ breakarr: how these two plugins share one recording

Reviewed 2026-09-18. Both plugins act on the SAME `Recording`, the same
`custom_properties` blob and the same `ProgramData` table, in the same
Dispatcharr container. Neither imports the other and neither should.

## The deployed plugin IS this repo (synced 2026-09-27)

The local clone was fast-forwarded to origin/main `f8943f7` (1.6.0: d90e22e v1.5.0 "Defer the thumbnail when
a cutter owns it", 4571322 v1.6.0 RPDB/graded posters, f8943f7 version sources testable). `clapparr/plugin.py`
and `plugin.json` are byte-identical to `/opt/dispatcharr/plugins/clapparr/` on saltbox-filemonster. The
earlier "never committed" note was wrong: the commits were on GitHub, only this clone had not fetched them.

## ⭐ The integration contract: generic file claims, never names

`_emit` defers to an external thumbnail owner via `<base>-thumb.<owner>.json`,
and its comment says why it is generic rather than naming breakarr: a clapparr
update would silently revert a local patch and the protection would vanish with
no signal. **Every future seam follows this shape** — a file or a database key
either side can ignore. `_fire_webhook` (fires once, only when something
actually changed) is the second seam.

## ⭐⭐ Clapparr's EPG matcher is the fix for breakarr's measured 26.2% loss

`_fresh_sub_title` / `_fresh_plot` match `tvg_id` AND `title` AND a ±10 min
window (`EPG_WINDOW_MIN`), longest value wins across duplicate feed rows, and
fall back to the **first line of `description`** when `sub_title` is empty
(measured on The Block: 11 of 23 rows carry `sub_title`).

`breakarr_bridge._enrich` matches `tvg_id` + **exact** `start_time`. Measured
over 42 live completed recordings: the row is lost in 11 (26.2%), taking genre,
`live`/`new`/`previously_shown`, `length` and both neighbouring programmes.
**7 of the 11 are The Block** — the same show, the same phenomenon.

⛔ Do not drop the title from the predicate: `tvg_id` + window alone handed a
10 News+ recording The Bold and the Beautiful's synopsis.

## ⚠️ `custom_properties` is a shared JSON blob — take the lock

`_capture_epg_at_start` re-reads under `select_for_update()` inside
`transaction.atomic()` and merges ONE key, because the recording task is
concurrently writing `status` and `file_path` into the same blob. A plain
read-modify-write reverts them and the field still looks well-formed.
breakarr_bridge is read-only today; if that changes, copy this verbatim.

It also stamps `custom_properties["clapparr_sub_title_source"] = "epg_at_start"`
— typed provenance that breakarr benefits from and currently cannot see
(measured: `clapparr` appears in 0 of 177 emitted events).

## Agreed sources (two plugins, written separately, same expression)

`season` / `episode`: `cp.get(...) or prog.get(...)` — clapparr `plugin.py`
1005-1006, breakarr_bridge 470-471. The database, not the filename.

## Not clapparr's problem

`_extra_fields` deliberately omits `video`, `audio` and `subtitles` from the
NFO, so clapparr holds no caption vocabulary. breakarr's `caption_state` stays
the only one.

Full analysis: `breakarr:docs/superpowers/specs/2026-09-18-plugin-reporter-expansion.md` §5.
