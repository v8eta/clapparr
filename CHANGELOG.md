# Changelog

All notable changes to Clapparr are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
Clapparr adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.4.1] — 2026-08-28

### Fixed

- **The plugin reported its version as 1.3.0.** `Plugin.version` in `plugin.py`
  was not bumped for the 1.4.0 release, and that attribute is what Dispatcharr
  displays, so the UI showed 1.3.0 for a 1.4.0 plugin. The manifest and this
  changelog were correct; only the value the user actually sees was wrong.

## [1.4.0] — 2026-08-27

### Added

- **Episode names are re-read from the EPG at write time.** The plot was already
  re-read for this reason — a provider usually publishes the episode synopsis
  *after* a recording is scheduled — but the title was still taken from the
  scheduling snapshot, so it fell back to a date while the plot came out
  correct. NFOs carried `title "Wednesday 26 August 2026"` beside a real
  synopsis, while the EPG held `sub_title "Main Ensuite Week"` for that slot.
- **`on_recording_start` captures the episode name while the programme is on
  air**, fixing the snapshot at source rather than compensating for it later.
  Uses Dispatcharr's documented `events` dispatch.
- **EPG detail that was previously discarded now reaches the NFO**: `<genre>`,
  `<year>`, `<country>`, `<ratings>`/`<rating>`, `<actor>`/`<director>`/
  `<writer>`, `<uniqueid type="tvdb">`, `<tag>` and `<thumb>`. The EPG row was
  already being fetched for the plot, so each additional field is one dictionary
  lookup.
- Repeat/first-run flags (`previously_shown`, `new`, `premiere`, `live`) are
  written as `<tag>` elements.

### Changed

- **A generic show blurb is no longer preferred over a real episode synopsis.**
  Selecting the longest available description returned a marketing paragraph
  whenever a feed carried one, because a blurb is usually longer than a synopsis.
  Generic candidates are now deprioritised rather than discarded, so if every
  candidate is generic one is still returned.
- The episode-name lookup also reads the **first line of `description`**, since
  some feeds leave `sub_title` empty and use `"<Episode Name>\n<synopsis>"`
  instead. Guarded to require a second line, so a single-line synopsis can never
  end up in the title field.

### Fixed

- **A local thumbnail now always takes precedence over EPG artwork.** The local
  file is a frame of the actual recording; the EPG icon is promotional art for
  the programme, and is used only when no local thumbnail exists.

### Notes

- `video`, `audio` and `subtitles` are deliberately **not** carried into the
  NFO. Plex and Kodi read those from the media file, where they are
  authoritative; an EPG's claim about aspect ratio or audio channels is a
  prediction, and a wrong one would override the truth.
- Using `previously_shown_details.start` to borrow a repeat's first-run
  description was evaluated and **rejected**. The pointer resolves for a small
  minority of rows, and where it does resolve it usually points at a *different
  episode* — the original air date plus a time window matches whatever occupied
  that slot. It would replace correct synopses with confidently wrong ones,
  serials worst of all.

## [1.3.0] — 2026-08-20

### Changed

- Rebranded to Clapparr.

## [1.2.1] — 2026-08-20

### Fixed

- Secret settings fields are masked in the UI.
- The documented event dispatch is cited rather than assumed.

## [1.2.0] — 2026-08-20

### Added

- Show artwork is refreshed in Plex after a write.

### Fixed

- Sidecars are given the same ownership as the directory holding them, so
  tooling running as the media user can manage the files it is responsible for.

## [1.1.0] — 2026-08-20

### Added

- A post-write webhook.

## [1.0.0] — 2026-08-20

### Added

- Initial release as *DVR NFO Generator* for Dispatcharr: Kodi/Plex NFO
  sidecars, posters and episode thumbnails for DVR recordings.

[1.4.1]: https://github.com/v8eta/clapparr/compare/v1.4.0...v1.4.1
[1.4.0]: https://github.com/v8eta/clapparr/compare/v1.3.0...v1.4.0
[1.3.0]: https://github.com/v8eta/clapparr/compare/v1.2.1...v1.3.0
[1.2.1]: https://github.com/v8eta/clapparr/compare/v1.2.0...v1.2.1
[1.2.0]: https://github.com/v8eta/clapparr/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/v8eta/clapparr/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/v8eta/clapparr/releases/tag/v1.0.0
