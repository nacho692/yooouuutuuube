#!/usr/bin/env python3
"""Local server for mosaic.html.

Serves this directory (with HTTP Range support, which plain `http.server`
lacks) on 127.0.0.1, plus one endpoint:

  GET /resolve?url=<youtube/vimeo/... link>
      Asks yt-dlp for the direct stream URLs and returns
      {"src": <video url>, "audio": <audio url or null>}. The browser then
      streams from YouTube itself; nothing passes through this server.
      YouTube serves video and audio as separate streams, so the page plays
      the audio in its own element. The URLs are signed for this machine's
      IP and expire after ~6 h, so they only work on the machine that
      resolved them.

Usage: python3 serve.py [port]   (default 8731)
"""
import json
import os
import re
import subprocess
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

ROOT = os.path.dirname(os.path.abspath(__file__))
# H.264 so any browser decodes it; capped at 720p.
FORMAT = ('bv*[vcodec^=avc1][height<=720]+ba[ext=m4a]/'
          'b[ext=mp4][height<=720]/bv*[height<=720]+ba/b')


def resolve(url):
    proc = subprocess.run(
        ['yt-dlp', '--no-playlist', '--no-warnings', '-f', FORMAT, '-g', '--', url],
        capture_output=True, text=True, timeout=120)
    lines = [l.strip() for l in proc.stdout.splitlines() if l.strip()]
    if proc.returncode != 0 or not lines:
        err = [l for l in proc.stderr.splitlines() if 'ERROR' in l]
        raise RuntimeError(err[-1] if err else (proc.stderr.strip()[-300:] or 'yt-dlp failed'))
    return {'src': lines[0], 'audio': lines[1] if len(lines) > 1 else None}


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=ROOT, **kw)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == '/resolve':
            return self.handle_resolve(parse_qs(parsed.query).get('url', [''])[0])
        return super().do_GET()

    def handle_resolve(self, url):
        if not re.match(r'^https?://', url):
            return self.send_json(400, {'error': 'expected an http(s) link'})
        try:
            self.send_json(200, resolve(url))
        except Exception as e:  # yt-dlp error or timeout
            self.send_json(502, {'error': str(e)})

    def send_json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # Single-range support so <video> can seek and loop without refetching.
    def send_head(self):
        self._range = None
        rng = self.headers.get('Range')
        path = self.translate_path(self.path)
        if not rng or not os.path.isfile(path):
            return super().send_head()
        m = re.match(r'bytes=(\d*)-(\d*)$', rng.strip())
        size = os.path.getsize(path)
        if not m or (not m.group(1) and not m.group(2)):
            return super().send_head()
        if m.group(1):
            start = int(m.group(1))
            end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
        else:
            start, end = max(0, size - int(m.group(2))), size - 1
        if start >= size or start > end:
            self.send_response(416)
            self.send_header('Content-Range', f'bytes */{size}')
            self.end_headers()
            return None
        f = open(path, 'rb')
        f.seek(start)
        self._range = end - start + 1
        self.send_response(206)
        self.send_header('Content-Type', self.guess_type(path))
        self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
        self.send_header('Content-Length', str(self._range))
        self.send_header('Accept-Ranges', 'bytes')
        self.end_headers()
        return f

    def copyfile(self, source, outputfile):
        if not getattr(self, '_range', None):
            return super().copyfile(source, outputfile)
        left = self._range
        while left > 0:
            chunk = source.read(min(65536, left))
            if not chunk:
                break
            outputfile.write(chunk)
            left -= len(chunk)

    def end_headers(self):
        if not getattr(self, '_range', None) and self.command in ('GET', 'HEAD'):
            self.send_header('Accept-Ranges', 'bytes')
        super().end_headers()


if __name__ == '__main__':
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8731
    print(f'http://localhost:{port}/mosaic.html')
    ThreadingHTTPServer(('127.0.0.1', port), Handler).serve_forever()
