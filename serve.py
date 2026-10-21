#!/usr/bin/env python3
"""Local server for mosaic.html.

Serves this directory (with HTTP Range support, which plain `http.server`
lacks) on 127.0.0.1, plus one endpoint:

  GET /resolve?url=<youtube/vimeo/... link>
      Asks yt-dlp for the direct stream URLs and returns
      {"src": "/p/<token>", "audio": "/p/<token>" or null}.
  GET /p/<token>
      Streams the googlevideo URL behind that token (Range passed through).
      YouTube signs stream URLs for the IP that resolved them and refuses
      other IPs (or accepts them only after a delay), so the bytes are
      fetched from here, the machine that resolved them, and relayed.
      Tokens are random and expire with the URL (~6 h); the real URL never
      reaches the browser and only *.googlevideo.com is ever fetched.
      YouTube serves video and audio as separate streams, so the page plays
      the audio in its own element.

Only YouTube and Vimeo links are resolved, at most two at a time, so a
public deployment can't be used to make yt-dlp fetch arbitrary URLs.

Usage: python3 serve.py [port]   (default 8731; HOST env sets the bind
address, default 127.0.0.1)
"""
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

ROOT = os.path.dirname(os.path.abspath(__file__))
HOST = os.environ.get('HOST', '127.0.0.1')
# Same hosts the page sends here.
RESOLVE_HOSTS = re.compile(r'^(www\.|m\.|music\.)?(youtube\.com|youtube-nocookie\.com|youtu\.be|vimeo\.com|player\.vimeo\.com)$')
_slots = threading.BoundedSemaphore(2)
GOOGLEVIDEO = re.compile(r'^[\w.-]+\.googlevideo\.com$')
TOKEN_TTL = 6 * 3600
_tokens = {}  # token -> (url, expires)
_tokens_lock = threading.Lock()
# YouTube-shaped paths (/watch?v=, /shorts/ID, youtu.be-style /ID), so
# swapping youtube.com for this host in a video link opens it here.
YT_PATH = re.compile(r'^/(?:watch|(?:shorts/|embed/|live/)?([\w-]{11}))/?$')
# Default: H.264, capped at 720p. ?fmt=vp9 asks for VP9+Opus (software
# decoded on some phones that choke on H.264); ?fmt=av1 for AV1 up to 480p, ?fmt=low for 360p at <=30 fps.
FORMATS = {
    '': ('bv*[vcodec^=avc1][height<=720]+ba[ext=m4a]/'
         'b[ext=mp4][height<=720]/bv*[height<=720]+ba/b'),
    'vp9': 'bv*[ext=webm][protocol=https][height<=720]+ba[ext=webm][protocol=https]/bv*[height<=720]+ba/b',
    # AV1 is software-decoded on most phones; Firefox for Android can't copy
    # hardware-decoded (H.264/VP9) frames into a canvas, so it draws black.
    'av1': ('bv*[vcodec^=av01][ext=mp4][protocol=https][height<=480]+ba[ext=m4a][protocol=https]/'
            'bv*[vcodec^=av01][protocol=https][height<=480]+ba/b'),
    'low': ('bv*[vcodec^=avc1][height<=360][fps<=30]+ba[ext=m4a]/'
            'b[ext=mp4][height<=360]/bv*[height<=360]+ba/b'),
}


def resolve(url, fmt=''):
    proc = subprocess.run(
        ['yt-dlp', '--no-playlist', '--no-warnings', '-f', FORMATS.get(fmt, FORMATS['']), '-g', '--', url],
        capture_output=True, text=True, timeout=120)
    lines = [l.strip() for l in proc.stdout.splitlines() if l.strip()]
    if proc.returncode != 0 or not lines:
        err = [l for l in proc.stderr.splitlines() if 'ERROR' in l]
        raise RuntimeError(err[-1] if err else (proc.stderr.strip()[-300:] or 'yt-dlp failed'))
    for l in lines:
        if not GOOGLEVIDEO.match(urlparse(l).hostname or ''):
            raise RuntimeError('unexpected stream host')
    warm(lines)
    return {'src': mint(lines[0]), 'audio': mint(lines[1]) if len(lines) > 1 else None}


def warm(urls, limit=45):
    """Wait until Google starts answering for fresh stream URLs.

    A new googlevideo URL is refused (403) for its first ~20 s, whoever asks;
    handing it to the player right away makes it fail with "source not
    supported". Probe each with a 1-byte range until it stops saying 403.
    """
    def probe(u):
        end = time.time() + limit
        while time.time() < end:
            req = urllib.request.Request(u, headers={'User-Agent': 'Mozilla/5.0', 'Range': 'bytes=0-0'})
            try:
                urllib.request.urlopen(req, timeout=15).close()
                return
            except urllib.error.HTTPError as e:
                if e.code != 403:
                    return
            except Exception:
                return
            time.sleep(1)
    ts = [threading.Thread(target=probe, args=(u,)) for u in urls]
    for t in ts:
        t.start()
    for t in ts:
        t.join()


def mint(url):
    token = secrets.token_urlsafe(12)
    now = time.time()
    with _tokens_lock:
        for t in [t for t, (_, exp) in _tokens.items() if exp < now]:
            del _tokens[t]
        _tokens[token] = (url, now + TOKEN_TTL)
    return '/p/' + token


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=ROOT, **kw)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == '/resolve':
            q = parse_qs(parsed.query)
            return self.handle_resolve(q.get('url', [''])[0], q.get('fmt', [''])[0])
        if parsed.path.startswith('/p/'):
            return self.handle_proxy(parsed.path[3:])
        m = YT_PATH.match(parsed.path)
        if m and not os.path.exists(self.translate_path(parsed.path)):
            q = parse_qs(parsed.query)
            if m.group(1):
                q['v'] = [m.group(1)]
            if q.get('v'):
                self.send_response(302)
                self.send_header('Location', '/?' + urlencode(q, doseq=True))
                self.send_header('Content-Length', '0')
                self.end_headers()
                return None
        return super().do_GET()

    def handle_resolve(self, url, fmt=''):
        parsed = urlparse(url)
        if parsed.scheme not in ('http', 'https') or not RESOLVE_HOSTS.match(parsed.hostname or ''):
            return self.send_json(400, {'error': 'expected a YouTube or Vimeo link'})
        if not _slots.acquire(timeout=30):
            return self.send_json(429, {'error': 'resolver busy, try again'})
        try:
            self.send_json(200, resolve(url, fmt))
        except Exception as e:  # yt-dlp error or timeout
            self.send_json(502, {'error': str(e)})
        finally:
            _slots.release()

    def handle_proxy(self, token):
        with _tokens_lock:
            entry = _tokens.get(token)
        if not entry or entry[1] < time.time():
            return self.send_json(404, {'error': 'unknown or expired stream'})
        req = urllib.request.Request(entry[0], headers={'User-Agent': 'Mozilla/5.0'})
        if self.headers.get('Range'):
            req.add_header('Range', self.headers['Range'])
        try:
            up = urllib.request.urlopen(req, timeout=30)
        except urllib.error.HTTPError as e:
            up = e  # relay 403/416/... as is
        except Exception as e:
            return self.send_json(502, {'error': str(e)})
        self._range = None
        try:
            self.send_response(up.status)
            for h in ('Content-Type', 'Content-Length', 'Content-Range'):
                if up.headers.get(h):
                    self.send_header(h, up.headers[h])
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            while True:
                chunk = up.read(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass  # player seeked away or closed
        finally:
            up.close()

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
    print(f'http://localhost:{port}/')
    ThreadingHTTPServer((HOST, port), Handler).serve_forever()
