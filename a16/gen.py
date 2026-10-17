#!/usr/bin/env python3
# Write a metal-tt pipeline script (mtlp-json) for every pipeline Mush8 has
# recorded, pointing at the iOS shader libraries the build already made.
# It mirrors state.rs `descriptor()` exactly, so the archive metal-tt builds
# from it matches the pipelines the iPhone asks for at runtime.
#
# usage: gen.py <shader cache dir> <pipeline list> <metal-enums.json> <out.mtlp-json>
import json, os, sys

if sys.argv[1] == "--probe":
    # one pipeline using every key, for a16/probe.sh
    lib = sys.argv[2]
    print(json.dumps({
        "libraries": {"paths": [{"label": "P", "path": lib}]},
        "pipelines": {"render_pipelines": [{
            "vertex_function": "alias:P#probe_vs",
            "fragment_function": "alias:P#probe_fs",
            "color_attachments": [
                {"pixel_format": "BGRA8Unorm_sRGB", "write_mask": "Red Alpha", "blending_enabled": True,
                 "rgb_blend_operation": "Max", "alpha_blend_operation": "ReverseSubtract",
                 "source_rgb_blend_factor": "SourceAlpha", "destination_rgb_blend_factor": "OneMinusSourceAlpha",
                 "source_alpha_blend_factor": "BlendColor", "destination_alpha_blend_factor": "Zero"},
                {},
                {"pixel_format": "RGBA8Uint", "write_mask": "All"},
            ],
            "depth_attachment_pixel_format": "Depth32Float_Stencil8",
            "stencil_attachment_pixel_format": "Depth32Float_Stencil8",
        }]},
    }, indent=1))
    sys.exit(0)

cache, plist, enums_path, out = sys.argv[1:5]
E = json.load(open(enums_path))
PIXEL = {int(k): v for k, v in E["pixel"].items()}
FACTOR = {int(k): v for k, v in E["factor"].items()}
OP = {int(k): v for k, v in E["op"].items()}

# names of the keys in the script; checked against a Metal-made script by probe.swift
K = json.loads(os.environ.get("A16_KEYS", "{}")) or {
    "colors": "color_attachments",
    "pixel_format": "pixel_format",
    "write_mask": "write_mask",
    "blending": "blending_enabled",
    "rgb_op": "rgb_blend_operation",
    "alpha_op": "alpha_blend_operation",
    "src_rgb": "source_rgb_blend_factor",
    "dst_rgb": "destination_rgb_blend_factor",
    "src_alpha": "source_alpha_blend_factor",
    "dst_alpha": "destination_alpha_blend_factor",
    "depth": "depth_attachment_pixel_format",
    "stencil": "stencil_attachment_pixel_format",
}

# same tables as state.rs blend_factor / blend_op (NVN value -> Metal enum value)
NVN_FACTOR = {1: 0, 2: 1, 3: 2, 4: 3, 5: 4, 6: 5, 7: 8, 8: 9, 9: 6, 10: 7, 11: 10,
              16: 15, 17: 16, 18: 17, 19: 18, 0x61: 11, 0x62: 12, 0x63: 13, 0x64: 14}
NVN_OP = {2: 1, 3: 2, 4: 3, 5: 4}

def factor(n):
    return FACTOR[NVN_FACTOR.get(n, 1)]

def op(n):
    return OP[NVN_OP.get(n, 0)]

def write_mask(m):
    # metal-pipelines-script(5): space-separated names, "None" or "All"
    if m & 0xF == 0xF:
        return "All"
    names = [n for bit, n in [(1, "Red"), (2, "Green"), (4, "Blue"), (8, "Alpha")] if m & bit]
    return " ".join(names) or "None"

index = {}
for l in open(f"{cache}/index.tsv"):
    k, _, f = l.rstrip("\n").partition("\t")
    if f:
        index[int(k, 16)] = f
guest = {}
for l in open(f"{cache}/guest.tsv"):
    p = l.rstrip("\n").split("\t")
    if len(p) == 12:
        guest[(int(p[0], 16), int(p[1], 16), int(p[2], 16), int(p[3], 16))] = (int(p[4], 16), p[5])

labels = {}

def func(spec):
    gk, a, b, c = (int(x, 16) for x in spec.split(":"))
    g = guest.get((gk, a, b, c))
    if not g:
        return None
    f = index.get(g[0])
    if not f:
        return None
    label = labels.setdefault(f, f"L{len(labels)}")
    return f"alias:{label}#{g[1]}"

LIMIT = int(os.environ.get("A16_LIMIT", "0"))
ENABLE = os.environ.get("A16_ENABLE", "supportsFamily(apple8) && !supportsFamily(apple9) && !supportsFamily(mac2)")
pipes, seen, missing = [], set(), 0
for l in open(plist):
    if LIMIT and len(pipes) >= LIMIT:
        break
    l = l.rstrip("\n")
    if not l or l in seen:
        continue
    seen.add(l)
    f = l.split("\t")
    if len(f) < 8:
        continue
    vs, fs = func(f[0]), func(f[1])
    if not vs or not fs:
        missing += 1
        continue
    colors = [int(x, 16) for x in f[2].split(",")] if f[2] else []
    depth, stencil = int(f[3], 16), f[4] == "1"
    color_state, channel_mask = int(f[5], 16), int(f[6], 16)
    blend = [int(x, 16) for x in f[7].split(",")]
    # only GPUs of the iPhone 15's family (A15/A16 = apple8): older GPUs cannot
    # build some of these pipelines (too many buffers) and would fail the run
    p = {"enable": ENABLE, "vertex_function": vs, "fragment_function": fs}
    atts = []
    last = max((i for i, fmt in enumerate(colors) if fmt), default=-1)
    for i, fmt in enumerate(colors[: last + 1]):
        if fmt == 0:
            atts.append({})  # unused slot: defaults (Invalid format), like the runtime descriptor
            continue
        a = {K["pixel_format"]: PIXEL[fmt], K["write_mask"]: write_mask(channel_mask >> (4 * i) & 0xF)}
        if color_state >> (8 + i) & 1:
            w0, w1 = blend[i] & 0xFFFFFFFF, blend[i] >> 32
            a.update({
                K["blending"]: True,
                K["rgb_op"]: op(w0 >> 24 & 0xF),
                K["alpha_op"]: op(w0 >> 28 & 0xF),
                K["src_rgb"]: factor(w1 & 0x7F),
                K["dst_rgb"]: factor(w1 >> 8 & 0x7F),
                K["src_alpha"]: factor(w1 >> 16 & 0x7F),
                K["dst_alpha"]: factor(w1 >> 24 & 0x7F),
            })
        atts.append(a)
    if atts:
        p[K["colors"]] = atts
    if depth:
        p[K["depth"]] = PIXEL[depth]
        if stencil:
            p[K["stencil"]] = PIXEL[depth]
    pipes.append(p)

script = {
    "libraries": {"paths": [{"label": lab, "path": os.path.abspath(f"{cache}/{f}")} for f, lab in labels.items()]},
    "pipelines": {"render_pipelines": pipes},
}
json.dump(script, open(out, "w"), indent=1)
print(f"[a16] {len(pipes)} pipelines in the script, {missing} skipped (shader not in the iOS cache), {len(labels)} libraries")
