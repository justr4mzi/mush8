# Mush8 — 6 GB iPhone fork

A fork of **[c22dev/mush8](https://github.com/c22dev/mush8)**, the native iOS/macOS port of Mario Kart 8 Deluxe.
All the hard work (the port itself) is c22dev's. This fork only adds a layer of patches so the game runs on
**sideloaded iPhones with 6 GB of RAM** (tested on an iPhone 15, iOS 27), with built-in performance settings.

> You need your own game dump. Nothing from the game is in this repository.

---

## Why this fork?

Upstream Mush8 asks for 6 GB of RAM or more ("8 GB is comfy"). On a 6 GB iPhone with an `.ipa` installed
through SideStore / AltStore, iOS lets one app use about **3 GB** (about 3.07 GB measured on an iPhone 15),
and the sideloaded build doesn't get the extended memory entitlements. On top of that, the A15/A16 GPUs
can't read the Switch's BC-compressed textures, so they're decoded on the CPU into much bigger textures.

In practice, the stock build was killed by iOS when loading the character select or a race, froze on
menus while shaders compiled, and showed black or invisible objects.

This fork makes it **fit in about 3 GB and stay playable**: a race peaks around 2.6 to 2.9 GB, at a steady
30 fps on screen (the game itself still runs at 60 updates per second).

## What's added

Everything lives in [`patch-heap.py`](patch-heap.py), applied on top of upstream at build time, plus a few
GitHub workflows.

### Memory
- **Smaller textures**: the sharpest mip levels of big BC textures are not decoded, and the texture is
  stored smaller (4x or 8x less per side). This is the biggest memory saving.
- **Low-memory trim**: when iOS is close to killing the app, textures unused for 5 s are dropped
  (they come back when needed). Freshly loaded textures are never dropped.
- **Upload limit**: at most ~96 MB of texture upload buffers in flight while a race loads
  (avoids a memory spike).
- **Prewarm keeps only what it has to**: pipelines found in the archive aren't kept in memory.

### Shaders (no more endless freezes)
- **Pipeline prewarm** at startup, which pauses as soon as the game needs a pipeline (the old prewarm
  could freeze the "1 player" screen for minutes).
- **Device pipeline archive** (`work/devarch/`): every pipeline compiled on the phone is saved, in small
  files, and found again on the next launch (a self-test in the log checks it). Before, nothing was ever
  found again. It is cleared by itself when iOS or the GPU changes (iOS update, `work/` copied from
  another iPhone).
- **Failed pipelines are retried** (the iOS shader compiler sometimes dies mid-build), instead of
  staying black until the next launch.
- **Recorded pipeline list shipped in the app** (`data/pipelines.tsv`, 2384 pipelines): their shaders are
  compiled ahead of time on the build Mac, so a phone that never ran the game (iPhone 14...) doesn't
  have to compile them for minutes.
- **Full prewarm before playing**: holds the game until every known pipeline is ready, with a progress
  screen.

### Frame rate
- **30 fps lock (frameskip)**: one frame out of two is drawn, evenly paced, while the game still updates
  at 60 Hz (multiplayer timing is unchanged). Render targets drawn rarely are never skipped, so 3D models
  don't turn black.
- No render-thread stall for pipelines of targets drawn every frame.
- Texture change checks every 2 frames instead of every frame.

### Launcher (start menu)
- Performance settings, unlocked save, FPS / memory counters, music from other apps, game volume,
  tools: see below.
- The optimised settings are **built in**: no `env.txt` needed anymore (an `env.txt` in Documents still
  overrides any `NX_*` setting).

### Fixes
- Mii `GetIndex` answers "not found" instead of a bogus index.
- `SignalToAddress` (kind 2) follows Atmosphère/yuzu semantics.
- iOS link fix ("compact unwind ... doesn't fit in 24 bits").
- Watchdog: if no frame is shown for 6 s, every thread's position is written to the log.

## Launcher options

| Option | Default | What it does |
|---|---|---|
| Console mode | Handheld (720p) | Handheld is lighter on the GPU and the battery |
| 30 fps lock (frameskip) | On | Draws 1 frame out of 2, evenly paced |
| Textures | Ultra low | **Ultra low**: least memory (recommended). **Very low**: a bit sharper, a few hundred MB more. **Low**: only huge textures shrunk. **Full**: no reduction (runs out of memory) |
| Background texture loading | Off | Experimental. Fewer small hitches, but it can run out of memory while a race loads |
| Graphics on a separate thread | Off | Experimental. The game and the translation of its graphics to Metal run on two CPU cores at the same time instead of one after the other (more FPS when the CPU is the limit) |
| Unlocked save | Off | Plays with a save packed in the IPA (everything unlocked) in `work/save-unlocked`; your own save in `work/save` is never touched. Uses ~200 MB more in races |
| Show FPS / Show memory | On | Two small counters at the top of the screen |
| Music from other apps | On | Keeps Spotify & co playing, the game sound is mixed in |
| Game volume | 100 % | Game sound only |
| Prewarm pipelines | On | Builds known pipelines at startup |
| Full prewarm before playing | On | Waits until all of them are ready (seconds once the archive is filled) |
| Full prewarm on first launch only | Off | Same, only while `work/devarch` is empty (new phone) |
| Background shader compiling | On | Off = compile while the game waits (more freezes) |
| Quiet mode (no log) | Off | No log, no watchdog: a bit less CPU. Turn it off to send a log |

Tools: **Share the log** / **Share the previous log**, **Clean up unneeded files** (old texture cache,
leftover `.metallib`, old archive), **Clear the shader cache**.

### Recommended
Handheld, 30 fps lock, **Ultra low**, Background texture loading **off**, all prewarm options on except
"first launch only". On a new phone, turn on "Full prewarm on first launch only" for the first start.

## Good to know
- **First time on a track / character / kart**: if a shader was never compiled, what it draws stays black
  or invisible until it's ready (up to a minute or two when memory is tight). Finish the race: next time
  it shows straight away. Sending `work/pipelines.tsv` to be merged into `data/pipelines.tsv` makes it
  ready for every phone.
- **Keep `work/`** when reinstalling (saves, pipeline archive, pipeline list). The pipeline archive only
  works on the same chip and iOS version.
- **Storage**: installing needs 2 to 3 GB free (SideStore keeps temporary copies; use its Clear Cache).
- Tested: iPhone 15 (A16, iOS 27). iPhone 14 (A15, 6 GB): should behave the same, not confirmed yet.
  iPhones with 4 GB (iPhone 11...) get ~2.1 GB per app: not enough.

## Known issues
- Memory stays close to the limit: heavier tracks with the unlocked save can still be killed by iOS.
- Ready-made GPU binaries built offline with `metal-tt` (Xcode 26 tools) crash the iOS 27 driver at
  start: removed, the on-device archive does the job.
- 28 MB of the 3.03 GB game heap is not visible to the GPU (Metal's 3 GB buffer limit).
- Geometry shaders aren't supported (one shader of the game).

## Building the IPA with GitHub Actions
No Mac needed: the **Build Mush8 IPA** workflow builds on a GitHub Mac and publishes an
**AES-encrypted zip** (`Mush8-ipa`) as an artifact.

Repository secrets:
- `KEYS_URL`, `BASE_URL`, `UPDATE_URL`: private links to your `prod.keys`, base game and update 4.0.1
  (Google Drive or Dropbox)
- `IPA_PASSWORD`: password of the zip
- `SAVE_URL` (optional): zip / rar / 7z of a save folder, packed in the IPA for the "Unlocked save" option

Inputs: `runner` (`macos-26` by default, `macos-15` works too: the newest Xcode is selected). The game
code and the iOS shader cache are kept in the Actions cache, so later builds skip the 12 GB download
unless the pipeline list changes. Build logs go to the `ci-logs` branch.

Other workflows: **Check patch** (compiles with the patches for macOS and iOS, no game files, ~10 min),
**Mac smoke test** (runs the game on a GitHub Mac with scripted input and publishes the text logs; needs
the extra secret `FIRMWARE_URL`, a zip of your own `Firmware` folder), **A16 lab** (Metal tool experiments).

Install the `.ipa` with SideStore / AltStore.

---

## Upstream: how Mush8 works
The Switch and Apple Silicon both run arm64, so the game's code can almost run as is. A few instructions
can't run outside a Switch (the x18 register, system calls, system registers), so they're rewritten by
`nxpatch`. `nxhost` plays the role of the Switch's OS (Horizon) and its services (files, saves,
controllers, audio, network...) and turns its graphics API (NVN) into Metal. Game shaders are translated
to Metal and compiled ahead of time. On iOS (no JIT) the game code is linked into the app at build time.

### Game files
Your own dump: `prod.keys`, the base game (`.nsp`), update 4.0.1 (`.nsp`), optionally the Booster Course
Pass (`.nsp`) and a `Firmware` folder. Import them from the app or copy them in Files → On My iPhone → Mush8.

### Building locally (Mac)
`./build.sh`, or `./make-app.sh` (macOS), `./make-ios.sh --install` / `./make-ios.sh --ipa` (iOS); run
`python3 patch-heap.py` first to apply this fork's patches. Needs an Apple Silicon Mac, Rust, Xcode 26+,
`xcodegen`.

### Multiplayer
In the launcher, under Multiplayer: **LAN** (same network, a Switch can join), **Online** (any
lan-play.com server) or **Wireless** (nearby Mush8 players, no router). The 30 fps lock doesn't change the
game's timing, so it's fine in multiplayer; keep both phones' shaders ready (full prewarm) to avoid freezes.

### Controls
Any controller (Switch Pro, Joy-Con, DualSense, Xbox...), touch controls on iOS when no controller is
connected, optional motion controls. An iPhone can also be a controller for a Mac ("Use as a controller").

### Upstream limitations
- sideloaded `.ipa` / free Apple IDs don't get extended virtual addressing (smaller memory layout)
- Nintendo's online play is gone, use Online (lan-play)
- 5 to 8 players split screen is Switch 2 only
- no amiibo, WebM videos play as a slideshow on iOS

## Credits
- **[c22dev](https://github.com/c22dev/mush8)** — Mush8
- This fork: memory, shader, frame pacing and launcher patches for 6 GB iPhones
