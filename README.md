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

Everything lives in [`patch-heap.py`](patch-heap.py): it is applied on top of upstream at build time
(each section finds an exact piece of upstream code and replaces it, and stops the build if upstream
changed). Plus a few GitHub workflows and tools.

### Memory (the 3 GB limit)
Where the memory goes on an iPhone 15, measured with the logs below: the game's own RAM (the 3 GB Switch
heap, which iOS compresses in part) is about 2 GB of the footprint on the menus; our own Metal objects are
the rest. A race load adds 400 to 700 MB in a few seconds, which is when iOS kills the app.

- **Smaller textures** (launcher "Textures" setting): the sharpest mip levels of big BC textures are not
  decoded and the texture is stored smaller. Ultra low = 8x less per side for textures of 128 px and more.
- **BC1 textures as ASTC**: iPhone GPUs can't read the Switch's BC textures, so they're decoded on the
  CPU. BC1 textures are now rewritten block by block as ASTC 4x4, a format the iPhone reads natively:
  1 byte per pixel instead of 4, same colours (within 2/255, checked on 20 000 random blocks against
  reference decoders; a block that mixes transparent pixels with several colours gets their average colour).
- **BC3 textures as ASTC** too, per texture: same colours as before (within 4/255), alpha exact on opaque,
  constant and cut-out (on/off) blocks. ASTC keeps only 4 alpha levels in a 4x4 block whose alpha varies,
  so a texture with soft alpha gradients (smoke, glass...) on more than 1% of its blocks stays RGBA (no
  visible loss). Measured on Mario Circuit: BC3 textures took 113 MB decoded, ~28 MB as ASTC.
  `NX_BC3_ASTC=0` turns it off. BC4/BC5/BC7 are still decoded (BC4 to 1 byte per pixel, BC5 to 2).
  `NX_BC1_ASTC=0` turns it off. Measured on the iPhone during a race: BC1 textures take 131 MB as ASTC,
  they'd take ~524 MB decoded; the race peaked at 2.57 GB with 500 MB to spare (it used to be killed).
- **Low-memory trim** (`NX_TRIM_MB`, 900 MB by default): when iOS allows less than that, textures unused
  for 5 s are dropped (they come back when needed).
- **Emergency trim** (`NX_TRIM_EMERGENCY_MB`, 400 MB by default): checked every 8 new textures while a
  race loads; below that, every texture not used right now is dropped. The prewarmed pipelines are kept
  (releasing them freed nothing measurable on the iPhone and left objects black for 2 laps while 96 of them
  were rebuilt); they're only released below 150 MB.
- **Purgeable textures**: textures the game can rewrite, unused for ~2 s (120 frames), are marked "volatile":
  iOS takes that memory back first instead of killing the app, and they're decoded again if needed
  (`NX_PURGEABLE=0` turns it off). `NX_PURGEABLE_ALL=1` does it for every decoded texture: tried on the
  iPhone, iOS took back 1458 textures in one race and decoding them again cost FPS and memory, so it's off.
- **Draw data buffers**: 4 MB pieces instead of 16 MB (less half-empty space per frame in flight), at most 4
  spare pieces kept, none when less than 600 MB is left (they used 96 MB on the iPhone).
- **Upload limit**: at most 96 MB of texture upload buffers in flight while a race loads, 24 MB when
  less than 600 MB is left.
- **Detailed memory log** (every 10 s, see "Reading the log").

### Shaders (no more endless freezes or black objects)
- **Pipeline prewarm** at startup, which pauses as soon as the game needs a pipeline.
- **Prewarmed pipelines are kept** in memory (`NX_KEEP_PREWARMED=0` for the old behaviour): before, they
  were thrown away and rebuilt from the archive at the start of a race, which showed black objects for
  0.5 to 10 s (up to 2 laps when memory is tight). Only released when iOS is about to kill the app.
- **Device pipeline archive** (`work/devarch/`): every pipeline compiled on the phone is saved and found
  again on the next launch. It is **cleared by itself** when iOS or the GPU changes (iOS update, `work/`
  copied from another iPhone), since an archive made for another iOS/GPU is useless.
- **Failed pipelines are retried** after 1 s and 3 s (the iOS shader compiler sometimes dies mid-build)
  instead of staying black until the next launch.
- **Recorded pipeline list shipped in the app** (`data/pipelines.tsv`, 10091 pipelines (Oct 22: +6466 from the overnight VS sessions on 91 of the 96 tracks and 4 of the 8 battle arenas), from iPhone runs
  and the Mac smoke runs): their shaders are compiled ahead of time on the build Mac, so a phone that never
  ran the game doesn't have to compile them for minutes.
- **Full prewarm before playing**: holds the game until every known pipeline is ready, with a progress
  screen.

### Speed
- **Graphics on a separate thread** (on by default): the game and the translation of its graphics to
  Metal run on two CPU cores at the same time instead of one after the other. Measured on an iPhone 15:
  ~45 game fps instead of ~36 on heavy scenes.
- **30 fps lock (frameskip)**: one frame out of two is drawn, evenly paced, while the game still updates
  at 60 Hz (multiplayer timing unchanged). Render targets drawn rarely are never skipped.
- **Smart 30 fps lock** (on with the 30 fps lock, `NX_SMART30=0` for the plain one): when a hot phone slows
  the game below ~50 updates per second, the plain lock showed only half of them (20-22 fps on screen for
  a game at 40-45). Then every frame is drawn, so the screen shows what the game makes (40-45); back to 1
  frame out of 2 once the game is at full speed. `[smart30]` lines in the log show the switches.
- Texture change checks every 2 frames instead of every frame.
- **Fewer scheduler passes**: the game makes thousands of unlock/signal system calls per second, most with
  nobody waiting; after those, the second scheduling check (global lock + walk over all threads) is skipped.
- **Game thread priorities**: the game's main thread (game logic) and the thread that presents frames get
  "interactive" priority (like our graphics thread), so iOS keeps them on the 2 fast cores of the A15/A16
  when the phone is hot (`NX_STOCK_QOS=1` for the old priorities). The phone logs showed the FPS drops come
  from the game's own CPU work when iOS slows a hot phone down, not from the GPU or the graphics translation.

### Fixes
- **Menu freeze after "Single Player"**: the video decoder waited for its next frame while holding a lock
  the game needed; it now waits at most 250 ms and shows the previous frame.
- An open render pass dropped by an early exit no longer kills the app ("Command encoder released without
  endEncoding", seen on a macOS 14 Mac).
- **Crash while looking up a pipeline**: Metal threw an exception ("-[OS_dispatch_data data]: unrecognized selector") when a pipeline was looked up in the archives while another thread wrote to an archive; all archive use now holds one lock. That was not enough (still 45 crashes overnight on the Macs): after an archive miss the full compile still had the archives attached, so Metal searched them again outside the lock. The archives are now detached before compiling a miss. Objective-C exceptions are logged with our call stack.
- Mii `GetIndex` answers "not found" instead of a bogus index; `SignalToAddress` (kind 2) follows
  Atmosphère/yuzu; iOS link fix ("compact unwind ... doesn't fit in 24 bits").
- Watchdog: if no frame is shown for 6 s, every thread's position is written to the log.

## Launcher options

| Option | Default | What it does |
|---|---|---|
| Console mode | Handheld (720p) | Handheld is lighter on the GPU and the battery |
| 30 fps lock (frameskip) | On | Draws 1 frame out of 2, evenly paced |
| Textures | Ultra low | **Ultra low**: least memory (recommended). **Very low**: a bit sharper, a few hundred MB more. **Low**: only huge textures (2048 px+) shrunk. **Full**: no reduction (runs out of memory) |
| Background texture loading | Off | Experimental. Fewer small hitches, but it can run out of memory while a race loads |
| Graphics on a separate thread | **On** | Game and graphics on two CPU cores at once (more FPS) |
| Unlocked save | Off | Plays with a save packed in the IPA (everything unlocked) in `work/save-unlocked`; your own save in `work/save` is never touched. Uses ~200 MB more in races |
| Show FPS / Show memory | On | Two small counters at the top of the screen |
| Music from other apps | On | Keeps Spotify & co playing, the game sound is mixed in |
| Game volume | 100 % | Game sound only |
| Vibration / Motion controls / iPhone controllers | On / Off / Off | |
| Prewarm pipelines | On | Builds known pipelines at startup |
| Full prewarm before playing | On | Waits until all of them are ready (seconds once the archive is filled) |
| Full prewarm on first launch only | Off | Same, only while `work/devarch` is empty (new phone) |
| Background shader compiling | On | Off = compile while the game waits (more freezes) |
| Quiet mode (no log) | Off | No log, no watchdog: a bit less CPU. Turn it off to send a log |

Tools: **Share the log** / **Share the previous log**, **Share the pipeline list** (`work/pipelines.tsv`,
to merge into `data/pipelines.tsv`), **Clean up unneeded files**, **Clear the shader cache**,
**Reset settings to default** (asks first).

## env.txt (advanced settings)

A text file named `env.txt` in the app's folder (Files → On My iPhone → Mush8, next to `prod.keys`) sets
`NX_*` options, one per line, `NAME=value`; lines starting with `#` are ignored. It is read at each launch.
Example:

```
# warn earlier when memory gets low
NX_TRIM_EMERGENCY_MB=800
# a crash test without the ASTC textures
NX_BC1_ASTC=0
```

Order: `env.txt` is read first, then the launcher settings. Most launcher settings only fill in what
`env.txt` didn't set, so **`env.txt` wins**. A few are always taken from the launcher (marked *launcher
wins* below). Delete the file (or the line) to go back to the defaults.

### Memory
| Variable | Default | Effect |
|---|---|---|
| `NX_TRIM_MB` | 900 | Below this much memory left (MB), drop textures unused for 5 s |
| `NX_TRIM_EMERGENCY_MB` | 400 | Below this, while loading: release prewarmed pipelines and every texture not used right now. `0` = off |
| `NX_BC1_ASTC` | 1 | `0` = decode BC1 textures to RGBA like before (4x more memory) |
| `NX_BC3_ASTC` | 1 | `0` = decode BC3 textures to RGBA like before |
| `NX_PURGEABLE` | 1 | `0` = don't let iOS reclaim unused textures |
| `NX_PURGEABLE_ALL` | 0 | `1` = every decoded texture can be reclaimed by iOS (costs FPS) |
| `NX_KEEP_PREWARMED` | 1 | `0` = drop prewarmed pipelines after the prewarm (less memory, black objects for a moment) |
| `NX_TEX_DROP` | 3 (Ultra low) | Mip levels not decoded on big BC textures (0 to 3). Setting it here replaces the launcher's Textures setting |
| `NX_TEX_DROP_SHRINK` | 1 | Store the texture smaller (with `NX_TEX_DROP`), instead of an enlarged copy |
| `NX_TEX_DROP_MIN` | 128 (Ultra low), 256 | Don't shrink below this size (px) |
| `NX_TEX_DROP_ALL` | off | Also shrink cube maps and BC6 lighting (can turn tracks dark) |
| `NX_TEX_SHRINK_MIN` | 2048 (Low) | Only shrink textures at least this big |
| `NX_TEX_16BIT` | 0 | `1`/`2` = 16-bit colour textures (half the memory, visible banding) |
| `NX_SYNC_TEXTURES` | 1 | Load textures on the game's thread (launcher "Background texture loading" off) |
| `NX_NO_TEX_CACHE` | 1 | `1` = no decoded-texture cache on disk (`work/texcache`) |
| `NX_TEX_CACHE_MB` | 1024 | Size of that cache when it is on |
| `NX_NO_MEMLOG` | off | `1` = no memory lines in the log |

### Graphics and speed
| Variable | Default | Effect |
|---|---|---|
| `NX_ASYNC_GFX` | 1 | Graphics on a separate thread (`0` = off) |
| `NX_FRAMESKIP` | 1 | Frames skipped between two drawn ones (0 = none, max 4) |
| `NX_SMART30` | 1 | `0` = plain 30 fps lock (1 frame out of 2 even when the game is slow) |
| `NX_AUTOSKIP` | off | `1` = skip frames only when late |
| `NX_HASH_EVERY` | 2 | Check textures for changes every N frames (1 to 30) |
| `NX_HANDHELD` | 1 | Handheld (720p) / docked — *launcher wins* |
| `NX_FPS` | 1 | FPS measurement for the counter and the log |
| `NX_HIDE_FPS`, `NX_HIDE_MEM` | off | Hide the counters — *launcher wins* |

### Shaders and pipelines
| Variable | Default | Effect |
|---|---|---|
| `NX_PREWARM` | 1 | Prewarm at startup — *launcher wins* |
| `NX_PREWARM_FULL` | 1 | Wait for the full prewarm before playing |
| `NX_SYNC_SHADERS` | off | Compile while the game waits — *launcher wins* |
| `NX_ASYNC_ALL` | off | Never wait for a pipeline, even on a brand-new render target |
| `NX_NO_DEVICE_ARCHIVE` | off | Don't use `work/devarch` |
| `NX_PIPELINE_LIST` | the app's list | Pipeline list to prewarm |
| `NX_RECORD_PIPELINES` | off | Record new pipelines into the shipped list instead of `work/pipelines.tsv` |
| `NX_SKIP_SHADERS` | — | Comma-separated shader hashes not to draw (debugging) |

### Sound, controls, network
| Variable | Default | Effect |
|---|---|---|
| `NX_VOLUME` | 1.0 | Game volume, 0.0 to 1.0 |
| `NX_RUMBLE`, `NX_MOTION`, `NX_PHONE_PADS` | 1, 0, 0 | Vibration, motion controls, iPhone controllers — *launcher wins* |
| `NX_EXTRA_PADS` | 0 | Extra virtual controllers |
| `NX_LANGUAGE` | iPhone's language | Game language (e.g. `fr`, `en-US`) |
| `NX_NET`, `NX_LAN`, `NX_LANPLAY_SERVER` | launcher | Multiplayer mode / server — *launcher wins* |
| `NX_SAVE_DIR` | `work/save` | Save folder (the Unlocked save option sets it) |

### Debugging (for logs, slow)
`NX_TRACE_ROMFS` (game files read), `NX_TRACE_IPC`, `NX_TRACE_SVC`, `NX_TRACE_BSD`, `NX_TRACE_MOVIE`,
`NX_TRACE_THREADS`, `NX_TRACE_TRACKS` (track names, on by default), `NX_MUTEX_LOG`, `NX_NVN_LOG=<n>` (graphics API calls logged, 400 by default),
`NX_DUMP_EVERY=<s>` / `NX_DUMP_AT=<s,s,...>` (screenshots into `work/`), `NX_DUMP_FRAME`, `NX_DUMP_MSL`,
`NX_DUMP_SHADERS`, `NX_SOFT_BC=1` (decode BC on the CPU like an iPhone, on a Mac), `NX_NO_WATCHDOG`,
`NX_AUTOINPUT` (scripted buttons, below), `NX_STOCK_QOS`, `NX_NO_SCHED`, `NX_COMPACT_MEMORY`.

## Reading the log
Share it from the launcher (**Share the log**; after a crash, **Share the previous log**).

- `[mem] 43 s: app uses 1973 MB, iOS still allows 1098 MB` — every 10 s. The app is killed when "still
  allows" reaches ~0. `jump to X MB` = a big rise in 1 s (race loading).
- `[mem] detail: game RAM … Metal … | resident … compressed … footprint …` — game RAM = the Switch heap
  pages in memory; Metal = everything given to the GPU (includes the 3 GB heap); compressed = what iOS
  squeezed (it slows everything down when it grows fast).
- `[mem] Metal besides the game RAM: X MB = textures …, draw scratch …, block copies …, uploads in flight …,
  other …` — our own GPU memory, by kind.
- `[mem] textures: …` and `[mem] BC by format …` — every ~10 s, texture count and size, BC textures by format.
- `[game] track: Gu_WaterPark (app uses 2300 MB)` — a track loaded (`NX_TRACE_TRACKS=0` to turn off).
- `[cpu] …% of a core in all: guest-0x51 60%, gfx-submit 25%, …` and `[cpu] system calls: …` — every 10 s,
  the CPU used by each thread and the game's most frequent system calls.
- `[mem] phone: heat …` — `serious`/`critical` = iOS slows the CPU/GPU down (lower FPS, slower shader builds).
- `[gfx] pipeline …: not in the archive, compiling` / `building took N ms` — a pipeline seen for the first
  time; long times = memory or heat pressure.
- `[fps] …` / `[prof] …` — frame rate and where the time goes. `[prof] game thread: graphics calls X ms
  (of which waiting for GPU fences Y ms)` = time the game spends in the graphics calls we translate;
  every 10 s `[prof] slowest graphics calls` lists the 6 that took the most time. With the 30 fps lock,
  30 fps on screen needs the game itself at 60 (one frame out of two is drawn).
- `[hang] no frame for 6 s` — the watchdog: what every thread was doing.

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

Other workflows:
- **Check patch**: compiles with the patches for macOS and iOS, no game files, ~10 min.
- **Mac smoke test**: runs the game on a GitHub Mac with scripted buttons and publishes text logs,
  low-resolution screenshots every minute (then every 10 min) and the recorded pipelines to the
  `ci-logs` branch. Needs the extra secrets `FIRMWARE_URL` (a zip of your own `Firmware` folder) and
  `SAVE_URL`. Inputs: `runner`, `modes` (`NX_ASYNC_GFX` values), `seconds`, `script` (`time:BUTTON,...`),
  `vs96=1` (VS race, 150cc, all tracks in order, 96 races, smart steering + auto accelerate: `tools/vs96.py`),
  `probe` (`tools/probe.py`), `monkey=1` (random play, `tools/monkey.py`), `soft_bc=1` (decode textures like
  an iPhone, for memory tests), `record`, `courses` with `vs96=1`: `inorder`, `random` (VS race, tracks in
  random order: later sessions reach the tracks not played yet) or `battle` (Battle -> Balloon Battle: the 8
  battle arenas are their own maps with their own pipelines; the arena changes from one battle to the next
  through random stick moves; a screenshot every minute for the first 25 min to check the menus),
  `prewarm_test=N` (RAM test instead of a play run: precompiles and prewarms the first N lines of
  `data/pipelines.tsv`, then the whole list, and writes the app's memory before/after to `prewarm-test.txt`).
  A run ends at 310 min so its pipelines are saved before GitHub's 360-min limit; they are also published
  every 10 min (`pipelines-live.tsv`).
  `NX_AUTOINPUT` takes `time:BUTTON` pairs (A, B, X, Y, L, R, PLUS, UP, DOWN, LEFT, RIGHT, SLEFT/SRIGHT for
  the stick...) or `@file` to read them from a file.
- **A16 lab**: Metal tool experiments.
- Game files for the workflows can come from Google Drive through a service account (`GDRIVE_SA_JSON`),
  which avoids Drive's "too many downloads" limit.

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
