# DVR NFO Generator

A [Dispatcharr](https://github.com/Dispatcharr/Dispatcharr) plugin that writes Kodi/Plex NFO sidecars, posters and episode thumbnails for DVR recordings, so they present with real titles, summaries and artwork instead of `Episode 08-18`.

Dispatcharr already knows the title, sub-title, description, season, episode and poster URL of everything it records — it stores them on the `Recording` row. This plugin writes that back out beside the file, where a media server can read it.

```
<show>/tvshow.nfo                 <tvshow>          — once per show
<show>/poster.jpg                 show poster
<show>/<recording>.nfo            <episodedetails>
<show>/<recording>-thumb.jpg      episode still
```

Read by **Plex** (agent: *Plex TV Series Agent (NFO)*), **Emby**, **Jellyfin** and **Kodi**.

## Install

**From the Dispatcharr plugin browser** — search for *DVR NFO Generator*.

**Manually** — download `plugin-dvr-nfo-generator-vX.Y.Z.zip` from [Releases](../../releases) and upload it in Dispatcharr under **Plugins → Import**, or unzip it into your plugins directory:

```
/data/plugins/dvr_nfo_generator/
```

Then enable the plugin and press **Generate missing sidecars**.

## Plex setup

Plex will ignore these files unless the library is set up to read them:

1. Library → **Edit** → **Advanced** → Agent = **Plex TV Series Agent (NFO)**
2. Keep **Use local assets** enabled (it is on by default)

> [!IMPORTANT]
> **Plex takes the episode index from the *filename*, and its NFO agent skips any episode it could not index.** A recording named `Seven News - 2026-08-18.mkv` becomes a date-based episode with no season/episode, and Plex will ignore its `.nfo` entirely — adding `<season>`/`<episode>` to the NFO does not help.
>
> Your Dispatcharr TV path template must produce `SxxExx` in the filename. If your recordings are landing on the date-based *fallback* template despite the EPG having episode numbers, you are hitting [Dispatcharr#1307](https://github.com/Dispatcharr/Dispatcharr/issues/1307).

## Actions

| Action | What it does |
|---|---|
| *(automatic)* | Runs when a recording finishes, if enabled |
| **Generate missing sidecars** | Writes anything absent, leaves existing files alone |
| **Regenerate all sidecars** | Rewrites everything, replacing existing files |
| **Preview** | Shows what would be written, touches nothing |
| **Test the webhook** | Fires the webhook against your most recent recording |

## Settings

| Setting | Default | Notes |
|---|---|---|
| Generate automatically after each recording | on | Waits for the remux, in the background |
| Write `tvshow.nfo` per show folder | on | |
| Download show `poster.jpg` | on | Uses the poster URL Dispatcharr stored |
| Look up artwork on TVmaze when the recording has none | on | See *Artwork fallback* below |
| Minimum title coverage for a fuzzy match | `0.6` | Raise toward `1.0` for near-exact names only |
| Prefer shows from this country | *(blank)* | 2-letter code; breaks ties between identically named shows |
| Thumbnail | representative | `off`, `fixed`, or `representative` |
| Position through the recording | `40`% | Kept clear of the opening and closing minutes |
| Skip the first N seconds | `150` | Avoids pre-roll, idents and opening titles |
| Thumbnail width | `640` px | |
| When the EPG has no episode title | air date (long) | Also: short date, episode number, or show name |
| Overwrite existing sidecars | off | So hand-edited metadata is never clobbered |
| Wait up to N minutes for the remux | `30` | Long recordings take longer |
| Webhook URL | *(blank)* | Called once per recording after its sidecars are written |
| Method | `GET` | `POST` sends the fields as JSON |
| Rewrite path prefix — from / to | *(blank)* | Translate Dispatcharr's path into what the other service sees |
| Extra header | *(blank)* | One header as `Name: value` |

### Notifying another service

Set a **Webhook URL** and the plugin calls it once per recording, after that recording's sidecars are written — and only when something actually changed, so a no-op sweep over a complete library doesn't spray the receiver.

Placeholders: `{path}` `{dir}` `{file}` `{show}`.

The obvious use is a scan relay such as [autopulse](https://github.com/dan-online/autopulse), so the media server rescans just that path instead of the whole library:

```
http://user:pass@autopulse:2875/triggers/manual?path={path}
```

Dispatcharr writes to its own container paths, which usually are not what the media server calls the same file, so set the rewrite pair:

| From | To |
|---|---|
| `/data/recordings/TV_Shows` | `/mnt/unionfs/dvr` |

Use **Test the webhook** to check the URL and the rewrite against a real recording without waiting for one to finish.

> [!NOTE]
> Credentials placed in the URL are converted to an `Authorization` header and stripped from the URL *before* the request is built, so they cannot appear in a success line or in a `urllib` exception (which quotes the URL back). They are still stored in plain text in Dispatcharr's plugin config, so prefer a scoped credential.

> [!TIP]
> **A scan re-reads episode NFOs, but not show-level artwork.** Measured against Plex by changing content and watching a single item:
>
> | Change | Path-scoped scan | `PUT /library/metadata/{key}/refresh?force=1` |
> |---|---|---|
> | Episode `.nfo` (title, summary) | picked up | not needed |
> | Show `poster.jpg` | ignored | required |
>
> So the webhook on its own keeps episode titles and summaries current — changing the title style and regenerating propagates with no forced refresh. Show artwork is the exception, and rare, since a poster is fetched once.

### Episode titles for daily programmes

News and current-affairs strands usually carry no episode sub-title in the EPG, so every episode would otherwise be called the same thing as the show. When the sub-title is missing the title falls back to the air date — `Wednesday 19 August 2026` — which at least sorts and reads correctly.

### Artwork fallback

Dispatcharr matches TVmaze on the **exact** EPG title, so a variant title stores no poster URL at all: `Highway Patrol Special` finds nothing, because TVmaze carries the series as `Highway Patrol`.

TVmaze does not degrade gracefully here — `search/shows?q=Highway Patrol Special` returns **zero** results — so the query is trimmed a word at a time (at most 3 calls) until something comes back. Only the *query* is relaxed; candidates are still judged against the full original title.

Loose title matching is how the wrong show's artwork gets attached to a recording, so a candidate is accepted only if it clears every one of these:

- its name is a contiguous **word-prefix** of the EPG title (`highway patrol` of `highway patrol special`) — never a loose character overlap
- that name is at least 2 words and 8 characters, so a bare `News` cannot swallow `News Special`
- those words cover at least *minimum title coverage* of the EPG title
- it actually carries an image
- it is the **sole** survivor — TVmaze lists both an AU and a US `Highway Patrol`, so an equal-coverage tie is refused as ambiguous unless the country preference separates them

Every decision is logged with its reasoning, so a wrong poster is traceable to the rule that admitted it:

```
[dvr_nfo_generator] artwork fallback for 'Highway Patrol Special':
  query 'highway patrol' matched 'Highway Patrol' (AU), coverage 0.67
```

Set this off if you would rather have no poster than a possibly wrong one.

> [!NOTE]
> A `poster.jpg` written *after* Plex last scanned that show only appears once that show's metadata is refreshed (`PUT /library/metadata/{ratingKey}/refresh?force=1`). A library-wide refresh issued *before* the file exists will not pick it up.

### Thumbnails

`representative` hands ffmpeg's `thumbnail` filter a batch of frames and takes the one most representative of the batch, which keeps it off fades and hard cuts. The sample point stays clear of the opening minutes and the final tenth of the file.

> [!TIP]
> If you run a commercial-detection pass that **cuts** recordings afterwards, consider setting thumbnails to `off` and generating them from the cut file instead. This plugin runs when the recording ends — before any such pass — so a mid-file frame can land inside an ad break.

## Timing

`recording_end` is emitted from inside Dispatcharr's recording task **before** that same task remuxes the recording into its final file and records the path. The plugin therefore hands off to a background thread that waits for the recording to report `completed`, and returns immediately.

A handler that blocked waiting for the finished file would deadlock the task that produces it.

## Requirements

- Dispatcharr v0.20.0 or newer
- `ffmpeg` / `ffprobe` in the Dispatcharr container (present in the official image) — only needed for thumbnails

## Tests

`test_fuzzy_match.py` covers the artwork matcher, including live TVmaze lookups and the adversarial cases the guards exist for. `test_webhook.py` covers path rewriting and credential handling, with no network access.

```bash
python3 dvr_nfo_generator/test_fuzzy_match.py
python3 dvr_nfo_generator/test_webhook.py
```

## Licence

MIT — see [LICENSE](LICENSE).
