#!/usr/bin/env python3
"""Static file server for LogiTrack's built frontend (the `dist/` folder).

Why this exists instead of `python -m http.server`: the stock server sends NO cache
headers, so browsers heuristically cache index.html and keep loading the OLD bundle
after a rebuild — which is why you'd have to manually clear the cache to see changes.

This server fixes that: Vite fingerprints the asset filenames (index-<hash>.js/css),
so those are safe to cache forever; the HTML entry point and anything unhashed are
sent with no-cache so a fresh build always shows up on the next reload.

Usage:  python serve_dist.py [PORT] [HOST] [DIRECTORY]
        (defaults: 5173 127.0.0.1 dist)
"""
import os
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 5173
HOST = sys.argv[2] if len(sys.argv) > 2 else "127.0.0.1"
BASE = os.path.dirname(os.path.abspath(__file__))
DIRECTORY = os.path.join(BASE, sys.argv[3] if len(sys.argv) > 3 else "dist")


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=DIRECTORY, **kwargs)

    def end_headers(self):
        path = self.path.split("?", 1)[0]
        name = os.path.basename(path)
        # Fingerprinted assets (…-<hash>.js/css under /assets/) can be cached hard —
        # a new build produces new filenames, so this can never go stale.
        if path.startswith("/assets/") and "-" in name and "." in name:
            self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        else:
            # index.html and any unhashed file must always be revalidated.
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
        super().end_headers()

    def log_message(self, *args):
        pass  # keep frontend.log quiet


if __name__ == "__main__":
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Serving {DIRECTORY} at http://{HOST}:{PORT} (no-cache on index.html)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
