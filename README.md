# yooouuutuuube

A JavaScript/Canvas port of the three video effects from the defunct Flash site
yooouuutuuube.com (David Kraftsow, 2009–2010): **Feedback**, **Mosaic** and
**Zoetrope**. Each one follows the original ActionScript, decompiled from the
archived SWFs.

## Running

```sh
python3 serve.py        # http://localhost:8731/mosaic.html
```

Paste a YouTube/Vimeo link (or a bare YouTube id), a direct mp4/webm URL, or
pick a local file. `test.mp4` is a synthetic test pattern you can load with
`?url=test.mp4`.

`serve.py` is only needed for YouTube/Vimeo links: it asks
[yt-dlp](https://github.com/yt-dlp/yt-dlp) for the direct stream URLs and the
browser streams from YouTube over your own connection. Those URLs are signed
for the IP that resolved them, so this has to run on the viewer's machine.
Local files and direct URLs work from any static host, or straight from disk.

## Controls

- Mouse movement shows the UI; it hides after 4 s idle, as in the original.
  **H** keeps it hidden.
- Zoetrope: arrow keys change direction, **Space** toggles flux mode, dragging
  (with flux on) resizes the frame.

## URL parameters

`mode` (`feedback` | `mosaic` | `zoetrope`), `url`, `vol` (0–100), `fbScale`,
`zWidth`, `zHeight`, `zDirection` (e.g. `top_right`, `left_down`, `rand`),
`zFlux` (`1`/`0`).
