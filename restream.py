#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["streamlink>=6.0"]
# ///
"""
LAN restreamer: download ONE copy of a YouTube (or Twitch, ...) live stream
and share it with any number of VLC players on the local network.

    python restream.py https://www.youtube.com/@SomeChannel/live
    python restream.py <url> --quality 720p --port 8080

Then on every other PC: VLC -> Media -> Open Network Stream -> http://<this-pc>:8080/

Only this PC talks to the internet; every viewer gets a copy of the same bytes.
"""

import argparse
import collections
import http.server
import queue
import socket
import sys
import threading
import time

try:
    import streamlink
    from streamlink.exceptions import NoPluginError, PluginError
except ImportError:
    sys.exit("streamlink is not installed. Run:  python -m pip install streamlink")

TS_PACKET = 188
TS_SYNC = 0x47
READ_SIZE = 64 * 1024
CLIENT_QUEUE_CHUNKS = 256        # ~16 MB per viewer before we consider it stuck
MAX_BACKLOG = 8 * 1024 * 1024    # never keep more than this for late joiners


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


class Hub:
    """Fans one byte stream out to many viewers.

    Keeps the stream from the start of the most recent keyframe / HLS segment
    (including the PAT/PMT tables), so a viewer that joins mid-stream gets a
    picture immediately instead of waiting for the next keyframe.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.clients = set()
        self.backlog = bytearray()
        self.bytes_in = 0
        self._pending = bytearray()   # partial TS packet carried between reads
        self._is_ts = None            # unknown until the first bytes arrive
        self._gop_start = None        # backlog offset of the PAT before the newest keyframe
        self._last_pat = None         # backlog offset of the most recent PAT packet
        self._rai_seen = False        # does this stream flag its keyframes?

    def reset(self):
        """Called when the upstream (re)connects: drop half-packets and old GOPs."""
        with self.lock:
            self._pending.clear()
            self.backlog.clear()
            self._is_ts = None
            self._gop_start = None
            self._last_pat = None
            self._rai_seen = False

    def subscribe(self):
        q = queue.Queue(maxsize=CLIENT_QUEUE_CHUNKS)
        with self.lock:
            if self.backlog:
                q.put_nowait(bytes(self.backlog))
            self.clients.add(q)
        return q

    def unsubscribe(self, q):
        with self.lock:
            self.clients.discard(q)

    def viewers(self):
        with self.lock:
            return len(self.clients)

    def publish(self, data):
        with self.lock:
            self.bytes_in += len(data)
            out = self._align(data)
            if not out:
                return
            self._remember(out)
            for q in list(self.clients):
                try:
                    q.put_nowait(out)
                except queue.Full:
                    # Viewer stopped reading (paused VLC, dead Wi-Fi...). Kick it,
                    # VLC can simply reconnect. Never let it slow the others down.
                    self.clients.discard(q)
                    while True:
                        try:
                            q.get_nowait()
                        except queue.Empty:
                            break
                    q.put_nowait(None)

    def _align(self, data):
        """Return only whole 188-byte TS packets (or raw data if it isn't TS)."""
        if self._is_ts is None:
            self._is_ts = data[:1] == bytes([TS_SYNC])
            if not self._is_ts:
                log("upstream is not MPEG-TS; late joiners may wait a bit for a picture")
        if not self._is_ts:
            return data
        buf = self._pending
        buf += data
        # Resync if we lost packet alignment.
        if buf and buf[0] != TS_SYNC:
            i = 0
            while i < len(buf) and not (buf[i] == TS_SYNC and
                                        (i + TS_PACKET >= len(buf) or buf[i + TS_PACKET] == TS_SYNC)):
                i += 1
            del buf[:i]
        n = len(buf) - len(buf) % TS_PACKET
        out = bytes(buf[:n])
        del buf[:n]
        return out

    def _remember(self, out):
        """Append to the backlog and trim it to start at the newest keyframe."""
        base = len(self.backlog)
        self.backlog += out
        if self._is_ts:
            for off in range(0, len(out), TS_PACKET):
                pid = ((out[off + 1] & 0x1F) << 8) | out[off + 2]
                if pid == 0:
                    self._last_pat = base + off
                    if not self._rai_seen:
                        # HLS segments (YouTube) start with PAT, PMT, keyframe
                        # but don't flag the keyframe: segment start is the GOP start.
                        self._gop_start = self._last_pat
                elif self._last_pat is not None and is_video_keyframe(out, off):
                    # flagged keyframe: start at the PAT before it so the decoder finds the PMT
                    self._rai_seen = True
                    self._gop_start = self._last_pat
            if self._gop_start:
                cut = self._gop_start
                del self.backlog[:cut]
                self._last_pat -= cut
                self._gop_start = 0
        if len(self.backlog) > MAX_BACKLOG:
            cut = len(self.backlog) - MAX_BACKLOG
            cut -= cut % TS_PACKET
            del self.backlog[:cut]
            self._gop_start = None
            self._last_pat = None


def is_video_keyframe(pkt, off):
    """TS packet that starts a video PES and has random_access_indicator set."""
    b1, b3 = pkt[off + 1], pkt[off + 3]
    if not (b1 & 0x40 and b3 & 0x20 and b3 & 0x10):   # PUSI + adaptation field + payload
        return False
    af_len = pkt[off + 4]
    if af_len == 0 or not pkt[off + 5] & 0x40:
        return False
    p = off + 5 + af_len
    return (p + 4 <= off + TS_PACKET and pkt[p:p + 3] == b"\x00\x00\x01"
            and 0xE0 <= pkt[p + 3] <= 0xEF)


def pick_stream(session, url, quality):
    streams = session.streams(url)
    if not streams:
        raise PluginError("no playable streams (is the stream live right now?)")
    for name in quality.split(","):
        name = name.strip()
        if name in streams:
            return name, streams[name]
    raise PluginError(f"quality '{quality}' not available, choose from: {', '.join(streams)}")


def upstream_loop(hub, session, url, quality, stop):
    """Keep exactly one connection to the source open, reconnecting forever."""
    delay = 2
    while not stop.is_set():
        try:
            name, stream = pick_stream(session, url, quality)
            log(f"upstream: opening {name}")
            fd = stream.open()
        except (PluginError, NoPluginError, OSError) as e:
            log(f"upstream: {e}; retrying in {delay}s")
            stop.wait(delay)
            delay = min(delay * 2, 30)
            continue
        hub.reset()
        delay = 2
        try:
            while not stop.is_set():
                data = fd.read(READ_SIZE)
                if not data:
                    log("upstream: stream ended, reconnecting")
                    break
                hub.publish(data)
        except Exception as e:  # network hiccup, YouTube URL expiry, ...
            log(f"upstream: read error ({e}), reconnecting")
        finally:
            try:
                fd.close()
            except Exception:
                pass
        stop.wait(1)


def make_handler(hub):
    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def log_message(self, fmt, *args):
            pass

        def _headers(self):
            self.send_response(200)
            self.send_header("Content-Type", "video/mp2t")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()

        def do_HEAD(self):
            self._headers()

        def do_GET(self):
            if self.path.startswith("/favicon"):
                self.send_error(404)
                return
            self._headers()
            who = self.client_address[0]
            q = hub.subscribe()
            log(f"viewer connected:    {who}  ({hub.viewers()} watching)")
            try:
                while True:
                    try:
                        chunk = q.get(timeout=60)
                    except queue.Empty:
                        continue  # upstream is reconnecting; keep the viewer
                    if chunk is None:
                        log(f"viewer too slow, dropped: {who}")
                        break
                    self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, socket.timeout):
                pass
            finally:
                hub.unsubscribe(q)
                log(f"viewer disconnected: {who}  ({hub.viewers()} watching)")

    return Handler


def lan_addresses():
    addrs = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))  # no packet is sent
            addrs.append(s.getsockname()[0])
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in addrs and not ip.startswith("127."):
                addrs.append(ip)
    except OSError:
        pass
    return addrs or ["<this-pc-ip>"]


def status_loop(hub, stop, interval=10):
    last = hub.bytes_in
    while not stop.wait(interval):
        now = hub.bytes_in
        mbit = (now - last) * 8 / interval / 1e6
        last = now
        log(f"status: {hub.viewers()} watching, downloading {mbit:.1f} Mbit/s (only once, shared)")


def ask_interactively(args, session):
    """Double-click friendly: ask for anything that wasn't given on the command line."""
    while not args.url:
        args.url = input("YouTube live URL: ").strip()
    if args.quality is None:
        try:
            names = list(session.streams(args.url))
        except (PluginError, NoPluginError) as e:
            sys.exit(f"Cannot open that URL: {e}")
        if not names:
            sys.exit("No playable streams found. Is the stream live right now?")
        print("Available qualities:", ", ".join(names))
        print("Tip: lower quality = less of your throttled bandwidth used.")
        while args.quality not in names:
            args.quality = input("Quality [best]: ").strip() or "best"


def main():
    p = argparse.ArgumentParser(description="Share one YouTube live stream with many VLC players on your LAN.")
    p.add_argument("url", nargs="?", help="YouTube (or Twitch, ...) live stream URL")
    p.add_argument("-q", "--quality", help="e.g. 720p, 480p, best, worst; comma list = fallbacks (default: ask, or best)")
    p.add_argument("-p", "--port", type=int, default=8080, help="HTTP port viewers connect to (default 8080)")
    p.add_argument("--bind", default="0.0.0.0", help="address to listen on (default: all interfaces)")
    args = p.parse_args()

    session = streamlink.Streamlink()
    if args.url is None:
        ask_interactively(args, session)
    quality = args.quality or "best"

    hub = Hub()
    try:
        server = http.server.ThreadingHTTPServer((args.bind, args.port), make_handler(hub))
    except OSError as e:
        sys.exit(f"Cannot listen on port {args.port} ({e}). Try another one with --port 8081")
    server.daemon_threads = True

    stop = threading.Event()
    threading.Thread(target=upstream_loop, args=(hub, session, args.url, quality, stop), daemon=True).start()
    threading.Thread(target=status_loop, args=(hub, stop), daemon=True).start()

    print()
    print("=" * 64)
    print(" Restreaming:", args.url)
    print(" In VLC on the other PCs: Media > Open Network Stream, enter:")
    for ip in lan_addresses():
        print(f"     http://{ip}:{args.port}/")
    print(" Press Ctrl+C to stop.")
    print("=" * 64)
    print(flush=True)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("stopping")
    finally:
        stop.set()
        server.server_close()


if __name__ == "__main__":
    main()
