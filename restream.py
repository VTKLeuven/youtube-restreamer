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
    python restream.py --on-demand            # headless: pick the stream in the browser

Viewers: VLC -> Media -> Open Network Stream -> http://<this-pc>:8080/stream
Control page (change stream/quality, see who's watching): http://<this-pc>:8080/

Only this PC talks to the internet; every viewer gets a copy of the same bytes.
Every option can also be set with an environment variable (see --help), which
is how the Docker container is configured.
"""

import argparse
import collections
import http.server
import json
import os
import queue
import signal
import socket
import sys
import threading
import time
import urllib.parse

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


_log_lock = threading.Lock()


def log(msg):
    with _log_lock:
        sys.stdout.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
        sys.stdout.flush()


class Hub:
    """Fans one byte stream out to many viewers.

    Keeps the stream from the start of the most recent keyframe / HLS segment
    (including the PAT/PMT tables), so a viewer that joins mid-stream gets a
    picture immediately instead of waiting for the next keyframe.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.clients = {}               # queue -> (viewer ip, connected since)
        self.last_left = float("-inf")  # monotonic time the last viewer left
        self.on_change = lambda: None   # called (outside the lock) when viewers come or go
        self.mbit = 0.0
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

    def subscribe(self, who):
        q = queue.Queue(maxsize=CLIENT_QUEUE_CHUNKS)
        with self.lock:
            if self.backlog:
                q.put_nowait(bytes(self.backlog))
            self.clients[q] = (who, time.time())
        self.on_change()
        return q

    def unsubscribe(self, q):
        with self.lock:
            self._drop(q)
        self.on_change()

    def _drop(self, q):
        if self.clients.pop(q, None) and not self.clients:
            self.last_left = time.monotonic()

    def viewers(self):
        with self.lock:
            return len(self.clients)

    def viewer_list(self):
        with self.lock:
            return list(self.clients.values())

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
                    self._drop(q)
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


def pick_stream(streams, quality):
    if not streams:
        raise PluginError("no playable streams (is the stream live right now?)")
    for name in quality.split(","):
        name = name.strip()
        if name in streams:
            return name, streams[name]
    raise PluginError(f"quality '{quality}' not available, choose from: {', '.join(streams)}")


class Source:
    """The single upstream connection. Its URL/quality can be switched at runtime.

    With on_demand the download only runs while someone is watching (plus
    idle_timeout seconds after the last viewer leaves), so an always-on server
    doesn't eat the throttled link when nobody is looking.
    """

    def __init__(self, hub, on_demand=False, idle_timeout=30):
        self.hub = hub
        self.on_demand = on_demand
        self.idle_timeout = idle_timeout
        self.session = streamlink.Streamlink()
        self.cond = threading.Condition()
        self.url = None
        self.quality = "best"
        self.gen = 0                  # bumped on every change, so the reader knows to switch
        self.phase = "idle"
        self.state = ""
        self.error = None
        self.playing = None           # quality actually opened
        self.fd = None                # open upstream, closed by set() to switch right away
        self.qualities = []           # available for the current URL
        hub.on_change = self.poke
        self._set_state("idle", "no stream selected")

    def poke(self):
        with self.cond:
            self.cond.notify_all()

    def set(self, url, quality="best"):
        with self.cond:
            if url != self.url:
                self.qualities = []
            self.url = url or None
            self.quality = quality or "best"
            self.gen += 1
            self.error = None
            log(f"source: {self.url or 'stopped'}" + (f" ({self.quality})" if self.url else ""))
            if not self.url:
                self._set_state("idle", "no stream selected")
            self.cond.notify_all()
            fd = self.fd
        if fd:
            try:
                fd.close()  # wakes the reader instead of waiting for the next segment
            except Exception:
                pass

    def _set_state(self, phase, text):
        if (phase, text) != (self.phase, self.state):
            self.phase, self.state = phase, text
            log(f"state: {text}")

    def _wanted(self):
        if not self.url:
            return False
        if not self.on_demand:
            return True
        return self.hub.viewers() > 0 or time.monotonic() - self.hub.last_left < self.idle_timeout

    def status(self):
        now = time.time()
        return {
            "phase": self.phase,
            "state": self.state,
            "error": self.error,
            "url": self.url,
            "quality": self.quality,
            "playing": self.playing,
            "qualities": self.qualities,
            "mbit": round(self.hub.mbit, 2) if self.phase == "streaming" else 0,
            "on_demand": self.on_demand,
            "viewers": [{"ip": ip, "seconds": int(now - since)} for ip, since in self.hub.viewer_list()],
        }

    def run(self):
        delay = 2
        while True:
            with self.cond:
                while not self._wanted():
                    if self.url:
                        self._set_state("standby", "standby: starts downloading when someone opens the stream")
                    else:
                        self._set_state("idle", "no stream selected")
                    self.cond.wait(timeout=2)
                gen, url, quality = self.gen, self.url, self.quality
            self._set_state("connecting", "connecting to " + url)
            try:
                streams = self.session.streams(url)
                if gen == self.gen:
                    self.qualities = list(streams)
                name, stream = pick_stream(streams, quality)
                fd = stream.open()
                with self.cond:
                    self.fd = fd
            except (PluginError, NoPluginError, OSError) as e:
                self.error = str(e)
                self._set_state("error", f"error: {e} (retrying in {delay}s)")
                with self.cond:
                    changed = self.cond.wait_for(lambda: self.gen != gen, timeout=delay)
                delay = 2 if changed else min(delay * 2, 30)
                continue
            self.hub.reset()
            self.error = None
            self.playing = name
            self._set_state("streaming", f"streaming {name}")
            delay = 2
            try:
                while gen == self.gen and self._wanted():
                    data = fd.read(READ_SIZE)
                    if not data:
                        if gen == self.gen:
                            log("upstream: stream ended, reconnecting")
                        break
                    self.hub.publish(data)
            except Exception as e:  # network hiccup, YouTube URL expiry, ...
                if gen == self.gen:
                    log(f"upstream: read error ({e}), reconnecting")
            finally:
                with self.cond:
                    self.fd = None
                self.playing = None
                try:
                    fd.close()
                except Exception:
                    pass
            if gen == self.gen and not self._wanted() and self.url:
                log("upstream: nobody watching, download paused")
            with self.cond:
                self.cond.wait_for(lambda: self.gen != gen, timeout=1)


def list_qualities(url):
    streams = streamlink.Streamlink().streams(url)
    if not streams:
        raise PluginError("no playable streams (is the stream live right now?)")
    return list(streams)


def make_handler(hub, source):
    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def log_message(self, fmt, *args):
            pass

        def _send(self, code, body, ctype):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, obj, code=200):
            self._send(code, json.dumps(obj).encode(), "application/json")

        def _body(self):
            n = min(int(self.headers.get("Content-Length") or 0), 64 * 1024)
            try:
                return json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                return {}

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            u = urllib.parse.urlsplit(self.path)
            if u.path == "/favicon.ico":
                self.send_error(404)
            elif u.path == "/api/status":
                self._json(source.status())
            elif u.path == "/api/qualities":
                url = urllib.parse.parse_qs(u.query).get("url", [""])[0].strip()
                try:
                    self._json({"qualities": list_qualities(url)})
                except Exception as e:
                    self._json({"error": f"cannot open that URL: {e}"}, 400)
            elif u.path == "/" and "text/html" in self.headers.get("Accept", ""):
                self._send(200, CONTROL_PAGE, "text/html; charset=utf-8")
            else:
                # /stream, or / from VLC/ffmpeg (they don't ask for HTML)
                self._stream()

        def do_POST(self):
            path = urllib.parse.urlsplit(self.path).path
            if path == "/api/play":
                body = self._body()
                url = str(body.get("url") or "").strip()
                if not url.startswith(("http://", "https://")):
                    self._json({"error": "enter a full URL starting with https://"}, 400)
                    return
                source.set(url, str(body.get("quality") or "best").strip())
                self._json(source.status())
            elif path == "/api/stop":
                source.set(None, source.quality)
                self._json(source.status())
            else:
                self.send_error(404)

        def _stream(self):
            self.send_response(200)
            self.send_header("Content-Type", "video/mp2t")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            if self.command == "HEAD":
                return
            who = self.client_address[0]
            q = hub.subscribe(who)
            log(f"viewer connected:    {who}  ({hub.viewers()} watching)")
            try:
                while True:
                    try:
                        chunk = q.get(timeout=60)
                    except queue.Empty:
                        continue  # upstream is (re)connecting; keep the viewer
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


CONTROL_PAGE = b"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>LAN Restreamer</title>
<style>
:root{--bg:#f4f5f7;--card:#fff;--fg:#1c1f24;--muted:#5f6873;--line:#d8dce2;--accent:#c4302b;
--ok:#1f883d;--warn:#9a6700;--err:#cf222e;--idle:#8c959f}
@media (prefers-color-scheme:dark){:root{--bg:#0f1115;--card:#181b22;--fg:#e6e8eb;--muted:#9aa3ad;
--line:#2b303a;--accent:#d7372f;--ok:#3fb950;--warn:#d29922;--err:#f85149;--idle:#6e7681}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:640px;margin:0 auto;padding:24px 16px}
h1{font-size:20px;margin:0 0 16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;margin-bottom:16px}
.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.muted{color:var(--muted);font-size:14px}
.dot{width:10px;height:10px;border-radius:50%;background:var(--idle);flex:none}
.dot.streaming{background:var(--ok)}.dot.connecting,.dot.standby{background:var(--warn)}.dot.error{background:var(--err)}
#state{font-weight:600;overflow-wrap:anywhere}
code{background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:4px 8px;font-size:14px;overflow-wrap:anywhere}
label{display:block;font-size:14px;color:var(--muted);margin-bottom:12px}
input,select{display:block;width:100%;margin-top:4px;padding:8px 10px;font:inherit;color:var(--fg);
background:var(--bg);border:1px solid var(--line);border-radius:6px}
select{width:auto;min-width:120px}
button{font:inherit;padding:8px 14px;border-radius:6px;border:1px solid var(--line);background:var(--bg);color:var(--fg);cursor:pointer}
button.primary{background:var(--accent);border-color:var(--accent);color:#fff}
button:disabled{opacity:.5;cursor:default}
ul{margin:6px 0 0;padding-left:20px}
</style></head><body><main>
<h1>LAN Restreamer</h1>
<section class="card">
  <div class="row"><span id="dot" class="dot"></span><span id="state">connecting...</span></div>
  <div id="detail" class="muted" style="margin-top:4px"></div>
  <p class="row" style="margin:14px 0 0">In VLC (Ctrl+N):<code id="vlcurl"></code><button id="copy" type="button">Copy</button></p>
</section>
<section class="card">
  <div id="vcount" style="font-weight:600">Viewers</div>
  <ul id="viewers" class="muted"></ul>
</section>
<form class="card" id="form">
  <label>YouTube live URL<input id="url" type="url" required placeholder="https://www.youtube.com/@channel/live"></label>
  <div class="row" style="align-items:flex-end;margin-bottom:12px">
    <label style="margin:0">Quality<select id="q"><option>best</option></select></label>
    <button id="check" type="button">Check qualities</button>
  </div>
  <div class="row"><button class="primary" id="play">Play</button><button id="stop" type="button">Stop</button></div>
  <div id="msg" class="muted" style="margin-top:8px">Lower quality uses less of the internet connection.</div>
</form>
</main><script>
const $ = id => document.getElementById(id);
const streamUrl = location.origin + "/stream";
$("vlcurl").textContent = streamUrl;
$("copy").onclick = () => navigator.clipboard.writeText(streamUrl).then(() => msg("Copied."), () => msg(streamUrl));
function msg(t) { $("msg").textContent = t; }
async function api(path, body) {
  const r = await fetch(path, body === undefined ? {} :
    {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
  const j = await r.json();
  if (!r.ok) throw new Error(j.error || r.statusText);
  return j;
}
function fillQualities(list, selected) {
  const opts = list.length ? list : ["best"], s = $("q");
  s.replaceChildren(...opts.map(n => Object.assign(document.createElement("option"), {textContent: n})));
  s.value = opts.includes(selected) ? selected : (opts.includes("best") ? "best" : opts[0]);
}
function ago(s) { return s < 60 ? s + " s" : s < 3600 ? Math.floor(s / 60) + " min" : Math.floor(s / 3600) + " h " + Math.floor(s % 3600 / 60) + " min"; }
let first = true;
function render(s) {
  $("dot").className = "dot " + s.phase;
  $("state").textContent = s.state;
  $("detail").textContent = s.phase === "streaming"
    ? `Downloading ${s.mbit.toFixed(1)} Mbit/s from the internet, shared by all viewers.` + (s.url ? " Source: " + s.url : "")
    : (s.on_demand && s.phase !== "idle" ? "Download runs only while someone is watching." : "");
  $("vcount").textContent = s.viewers.length === 1 ? "1 viewer" : s.viewers.length + " viewers";
  $("viewers").replaceChildren(...s.viewers.map(v =>
    Object.assign(document.createElement("li"), {textContent: `${v.ip}, watching for ${ago(v.seconds)}`})));
  if (first && s.url) { $("url").value = s.url; fillQualities(s.qualities, s.quality); }
  first = false;
}
async function refresh() {
  try { render(await api("/api/status")); }
  catch (e) { $("dot").className = "dot error"; $("state").textContent = "restreamer not reachable"; }
}
$("url").oninput = () => fillQualities([], "best");
$("check").onclick = async () => {
  const url = $("url").value.trim();
  if (!url) return msg("Enter a URL first.");
  $("check").disabled = true; msg("Checking...");
  try { const j = await api("/api/qualities?url=" + encodeURIComponent(url)); fillQualities(j.qualities, $("q").value); msg("Pick a quality and press Play."); }
  catch (e) { msg(e.message); }
  $("check").disabled = false;
};
$("form").onsubmit = async ev => {
  ev.preventDefault();
  try { render(await api("/api/play", {url: $("url").value.trim(), quality: $("q").value})); msg("Switched. VLC viewers stay connected."); }
  catch (e) { msg(e.message); }
};
$("stop").onclick = async () => {
  try { render(await api("/api/stop", {})); msg("Stopped. Nothing is downloaded until you press Play."); }
  catch (e) { msg(e.message); }
};
refresh(); setInterval(refresh, 2000);
</script></body></html>
"""


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


def status_loop(hub, source, tick=2, window=5):
    """Bandwidth averaged over window*tick seconds; logged every window*tick seconds."""
    samples = collections.deque([hub.bytes_in], maxlen=window + 1)
    n = 0
    while True:
        time.sleep(tick)
        samples.append(hub.bytes_in)
        hub.mbit = (samples[-1] - samples[0]) * 8 / ((len(samples) - 1) * tick) / 1e6
        n += 1
        if n % window == 0 and source.phase == "streaming":
            log(f"status: {hub.viewers()} watching, downloading {hub.mbit:.1f} Mbit/s (only once, shared)")


def ask_interactively(args):
    """Double-click friendly: ask for anything that wasn't given on the command line."""
    while not args.url:
        args.url = input("YouTube live URL: ").strip()
    if args.quality is None:
        try:
            names = list_qualities(args.url)
        except (PluginError, NoPluginError) as e:
            sys.exit(f"Cannot open that URL: {e}")
        print("Available qualities:", ", ".join(names))
        print("Tip: lower quality = less of your throttled bandwidth used.")
        while args.quality not in names:
            args.quality = input("Quality [best]: ").strip() or "best"


def env_flag(name):
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def main():
    env = os.environ.get
    p = argparse.ArgumentParser(
        description="Share one YouTube live stream with many VLC players on your LAN.",
        epilog="Environment variables URL, QUALITY, PORT, ON_DEMAND, IDLE_TIMEOUT and "
               "ADVERTISE set the same options (used by the Docker image); PUBLIC_PORT is "
               "the port printed with ADVERTISE if it differs from PORT.")
    p.add_argument("url", nargs="?", default=env("URL") or None,
                   help="YouTube (or Twitch, ...) live stream URL [$URL]. Without it: ask, "
                        "or (no console) wait until one is picked on the control page")
    p.add_argument("-q", "--quality", default=env("QUALITY") or None,
                   help="e.g. 720p, 480p, best, worst; comma list = fallbacks [$QUALITY, default best]")
    p.add_argument("-p", "--port", type=int, default=int(env("PORT") or 8080),
                   help="HTTP port for viewers and the control page [$PORT, default 8080]")
    p.add_argument("--bind", default="0.0.0.0", help="address to listen on (default: all interfaces)")
    p.add_argument("--on-demand", action="store_true", default=env_flag("ON_DEMAND"),
                   help="only download while someone is watching [$ON_DEMAND=1]")
    p.add_argument("--idle-timeout", type=float, default=float(env("IDLE_TIMEOUT") or 30),
                   help="with --on-demand: seconds to keep downloading after the last viewer leaves "
                        "[$IDLE_TIMEOUT, default 30]")
    p.add_argument("--advertise", default=env("ADVERTISE") or None,
                   help="host[:port] to print for viewers, e.g. when running in Docker [$ADVERTISE]")
    args = p.parse_args()

    if args.url is None and sys.stdin.isatty():
        ask_interactively(args)

    hub = Hub()
    source = Source(hub, on_demand=args.on_demand, idle_timeout=args.idle_timeout)
    if args.url:
        source.set(args.url, args.quality or "best")
    try:
        server = http.server.ThreadingHTTPServer((args.bind, args.port), make_handler(hub, source))
    except OSError as e:
        sys.exit(f"Cannot listen on port {args.port} ({e}). Try another one with --port 8081")
    server.daemon_threads = True

    if args.advertise:
        # PUBLIC_PORT: the host port when Docker maps it to a different container port
        port = env("PUBLIC_PORT") or args.port
        hosts = [args.advertise if ":" in args.advertise else f"{args.advertise}:{port}"]
    else:
        hosts = [f"{ip}:{args.port}" for ip in lan_addresses()]
    print()
    print("=" * 64)
    print(" Restreaming:", args.url or "nothing yet, pick a stream on the control page")
    print(" In VLC on the other PCs: Media > Open Network Stream, enter:")
    for h in hosts:
        print(f"     http://{h}/stream")
    print(" Control page (change stream / quality) in a browser:")
    for h in hosts:
        print(f"     http://{h}/")
    if os.path.exists("/.dockerenv") and not args.advertise:
        print(" (inside Docker the addresses above may be the container's; set ADVERTISE)")
    print(" Press Ctrl+C to stop.")
    print("=" * 64)
    print(flush=True)

    threading.Thread(target=source.run, daemon=True).start()
    threading.Thread(target=status_loop, args=(hub, source), daemon=True).start()

    def terminate(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, terminate)  # `docker stop`: exit right away, not after 10 s

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("stopping")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
