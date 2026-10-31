# env.txt (iOS)

On iOS, Mush8 reads an optional `env.txt` from the app's Documents folder (Files → On My iPhone → Mush8, or Finder → your device → Files → Mush8) before starting the game. Each line is `KEY=value`; empty lines and lines starting with `#` are ignored. Spaces around the key and value are trimmed.

```
# example env.txt
NX_FPS=1
NX_LANGUAGE=fr
NX_SPLIT60=1
```

The game log is written to `work/nxhost.log` in the same folder (the previous run is kept as `work/nxhost.prev.log`), which is where `NX_FPS` and the other logging options print.

## Launcher settings win

The launcher's settings (`config.json`) are applied **after** `env.txt`. For the keys below, change the setting in the launcher instead: a value in `env.txt` is overwritten whenever the launcher sets that key.

| Launcher setting | Key it sets | When |
|---|---|---|
| Network mode | `NX_NET` (and `NX_LAN=1` for LAN) | always |
| Online server | `NX_LANPLAY_SERVER` | when not empty |
| Prewarm pipelines | `NX_PREWARM=1` | unless turned off |
| Background shader compile | `NX_SYNC_SHADERS=1` | when turned off |
| Rumble | `NX_RUMBLE=0` | when turned off |
| Rumble strength | `NX_RUMBLE_MIN` | when saved in the launcher |
| Spatial audio | `NX_SPATIAL=0` | when turned off |
| Motion controls | `NX_MOTION=1` | when turned on |
| Handheld | `NX_HANDHELD=1` | unless turned off |
| Remote / browser controllers | `NX_PHONE_PADS=1`, `NX_BROWSER_PADS=1` | when turned on |
| Aspect | `NX_ASPECT=auto` | unless set to 16:9 |
| HUD at the edges / HUD scale | `NX_HUD=center`, `NX_HUD_SCALE` | HUD centered / scale saved in the launcher |

## Options only reachable through env.txt

| Key | Values | What it does |
|---|---|---|
| `NX_FPS` | any value | Logs fps, worst frame time and GPU time per frame every second |
| `NX_LANGUAGE` | language tag (`fr`, `en-GB`, `es-419`, `ja`…) | Game language. Defaults to the device's first preferred language |
| `NX_SPLIT60` | `1` | 60 fps split screen instead of 30 |
| `NX_4K` | `1` | 4K output when docked (handheld off), if the memory layout and Metal buffer limit allow it |
| `NX_COMPACT_MEMORY` | any value | Skip trying the full memory layout and go straight to the compact one |
| `NX_ASPECT` | ratio (`21:9`, `2.16`) | Forces an aspect ratio, clamped to 1–4. Only applies when the launcher's aspect is 16:9, since otherwise it sets `auto` |
| `NX_HUD_SCALE` | `0.5`–`1.5` | HUD scale (clamped). Overwritten once the launcher has saved a HUD scale |
| `NX_RUMBLE_MIN` | `0`–`0.9` (default `0.45`) | Minimum rumble strength. Overwritten once the launcher has saved a rumble strength |
| `NX_LAN_IF` | interface name (`en0`…) | Network interface used for LAN |
| `NX_AUDIO_LEAD_MS` | milliseconds | Audio buffer lead. Defaults to the output latency plus a small margin |
| `NX_SOFT_BC` | any value | Decode BC textures on the CPU even if the GPU supports them |
| `NX_GPU_HEAP_CHUNK_BYTES` | bytes | Caps the size of each Metal buffer used to map the GPU heap |
| `NX_INSTANCE` | any string | Mixes this string into the pseudo device ID the game sees |

`NX_SHADER_CACHE` and `NX_PIPELINE_LIST` are set by the app to the bundled shaders before `env.txt` is read; overriding them is only useful if you ship your own shader cache.

The remaining `NX_TRACE_*`, `NX_DUMP_*` and similar keys in the source are debugging aids and can slow the game down a lot.
