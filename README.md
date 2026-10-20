# YouTube LAN restreamer

Watch one YouTube live stream on several PCs while downloading it **only once**.

One PC (the "sharing PC") pulls the stream from YouTube; every other PC on the
LAN opens it in **VLC**. No browser and no web page are involved; the viewers
just receive a copy of the same video bytes.

```
YouTube ──(1× bandwidth, throttled link)──> sharing PC ──LAN──> VLC on PC 2
                                                       ├─LAN──> VLC on PC 3
                                                       └─LAN──> VLC on PC 4 …
```

## Sharing PC

You need Python 3.10+ (https://www.python.org/downloads/, tick *Add python.exe
to PATH*) **or** [uv](https://docs.astral.sh/uv/). The first run installs
[streamlink](https://streamlink.github.io/) automatically, so it needs internet.

* **Windows:** double-click `start-windows.bat`
* **Linux / macOS:** run `./start-linux-mac.sh`

It asks for the URL and a quality:

```
YouTube live URL: https://www.youtube.com/@SomeChannel/live
Available qualities: 144p, 240p, 360p, 480p, 720p, 1080p, worst, best
Quality [best]: 720p

================================================================
 Restreaming: https://www.youtube.com/@SomeChannel/live
 In VLC on the other PCs: Media > Open Network Stream, enter:
     http://192.168.1.59:8080/
 Press Ctrl+C to stop.
================================================================
```

Both `…/watch?v=…` links and `…/@channel/live` links work.

**Pick the lowest quality that looks OK.** Each step down saves throttled
bandwidth. The status line shows how much is used: `downloading 2.6 Mbit/s`.

Windows may ask to allow Python through the firewall. Click **Allow** (private
networks), or the other PCs can't connect.

You can skip the questions with arguments:

```
start-windows.bat https://www.youtube.com/@SomeChannel/live --quality 720p --port 8080
python restream.py <url> -q 720p,480p,best        # comma list = fallbacks
```

## Viewer PCs

VLC → **Media → Open Network Stream** (Ctrl+N) → enter the address printed on the
sharing PC, e.g. `http://192.168.1.59:8080/` → **Play**.

From a terminal: `vlc http://192.168.1.59:8080/`

The sharing PC can watch too: `http://localhost:8080/` uses no extra bandwidth.

## Good to know

* The download runs as long as the program runs, even with nobody watching.
  Close it (Ctrl+C) when you're done.
* If the internet hiccups or YouTube rotates its URLs, it reconnects on its own.
  VLC keeps playing after a short pause.
* A viewer who joins late starts at the latest segment and gets a picture
  right away.
* A viewer that stops reading (paused for minutes, broken Wi-Fi) is disconnected
  so it can't slow down the others. Press Play again in VLC.
* Works with anything streamlink supports (Twitch, …), not only YouTube.
  YouTube *live* streams are the main target; plain uploaded videos may not work.
* Stuttering on every viewer at once means the throttled link is too slow for
  the chosen quality, so restart with a lower one. Stuttering on one viewer
  only: in VLC raise *Tools → Preferences → All → Input/Codecs → Network
  caching* (e.g. to 3000 ms).

## Why not an existing tool?

The free tools I looked at all fall short of "one download, many VLC viewers,
zero config":

* `streamlink --player-external-http` serves only one client at a time and
  re-downloads the stream for every new client.
* liveproxy starts a separate yt-dlp/streamlink download per viewer.
* VLC `--sout` or MediaMTX + ffmpeg + yt-dlp can do it, but they take a hand-built
  command or config file, and VLC's own YouTube support breaks often.

This tool is a single Python file (~350 lines) on top of streamlink.
