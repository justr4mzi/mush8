#!/usr/bin/env python3
# Experimental patch for Mush8: when iOS refuses to give the GPU the whole
# guest heap in one buffer, retry with smaller sizes instead of giving up,
# and log the Metal / memory limits so we know exactly what iOS allows.
import pathlib, sys

p = pathlib.Path("crates/nxhost/src/gfx/mod.rs")
s = p.read_text()

start = s.index("pub fn wrap_heap(base: u64, size: u64) -> bool {")
end = s.index("pub fn heap() -> Option<&'static HeapBuffer>")

new = r'''#[cfg(target_os = "ios")]
fn available_memory() -> u64 {
    unsafe extern "C" {
        fn os_proc_available_memory() -> usize;
    }
    unsafe { os_proc_available_memory() as u64 }
}

#[cfg(not(target_os = "ios"))]
fn available_memory() -> u64 {
    0
}

pub fn wrap_heap(base: u64, size: u64) -> bool {
    let dev = &gpu().device.0;
    eprintln!(
        "[gfx] wrap_heap: request {size:#x}, maxBufferLength {:#x}, recommendedMaxWorkingSetSize {:#x}, available memory {:#x}",
        dev.maxBufferLength(),
        dev.recommendedMaxWorkingSetSize(),
        available_memory()
    );
    let ptr = std::ptr::NonNull::new(base as *mut std::ffi::c_void).unwrap();
    let mut len = size.min(dev.maxBufferLength() as u64) & !0x3fff;
    while len >= 0x1000_0000 {
        let b = unsafe { dev.newBufferWithBytesNoCopy_length_options_deallocator(ptr, len as usize, MTLResourceOptions::StorageModeShared, None) };
        match b {
            Some(b) => {
                let gpu = b.gpuAddress();
                if len < size {
                    eprintln!("[gfx] guest heap only partly mapped for the GPU: {len:#x} of {size:#x} at {gpu:#x}");
                } else {
                    eprintln!("[gfx] guest heap {base:#x}+{size:#x} mapped for the GPU at {gpu:#x}");
                }
                *HEAP.write().unwrap() = Some(Box::leak(Box::new(HeapBuffer { buffer: Shared(b), base, size: len, gpu })));
                return true;
            }
            None => {
                eprintln!("[gfx] GPU refused a {len:#x}-byte heap buffer");
                len = ((len / 4) * 3) & !0x3fff;
            }
        }
    }
    eprintln!("[gfx] could not wrap the guest heap {base:#x}+{size:#x} for the GPU");
    false
}

'''

s = s[:start] + new + s[end:]
p.write_text(s)
print("patched", p)

# Mii editor applet (0x12): the game opens it when you pick "Mii" with no Mii
# saved. Mush8 does not implement it and the game aborts. Answer "canceled".
a = pathlib.Path("crates/nxhost/src/services/am.rs")
t = a.read_text()
t = t.replace(
    "const APPLET_CONTROLLER: u32 = 0x0C;",
    "const APPLET_CONTROLLER: u32 = 0x0C;\nconst APPLET_MII_EDIT: u32 = 0x12;",
)
t = t.replace(
    "            APPLET_WEB | APPLET_OFFLINE_WEB => Some(web_applet(&input)),",
    "            APPLET_WEB | APPLET_OFFLINE_WEB => Some(web_applet(&input)),\n"
    "            APPLET_MII_EDIT => {\n"
    "                eprintln!(\"[am] Mii editor applet: answering canceled\");\n"
    "                let mut out = vec![0u8; 0x20];\n"
    "                out[0..4].copy_from_slice(&1u32.to_le_bytes());\n"
    "                out[4..8].copy_from_slice(&(-1i32).to_le_bytes());\n"
    "                Some(out)\n"
    "            }",
)
assert "APPLET_MII_EDIT =>" in t
a.write_text(t)
print("patched", a)

# Prewarm: build the recorded pipelines with 4 threads instead of 2 at startup.
st = pathlib.Path("crates/nxhost/src/nvn/draw/state.rs")
u = st.read_text()
old = "                for _ in 0..2 {\n                    s.spawn(|| {\n                        unsafe { libc::pthread_set_qos_class_self_np"
assert u.count(old) == 1, "prewarm loop not found"
u = u.replace(old, old.replace("0..2", "0..4"))
st.write_text(u)
print("patched", st)

# Thread priorities (A16 = 2 performance + 4 efficiency cores):
# - game threads ask for USER_INITIATED so iOS keeps them on the fast cores,
# - background shader/pipeline compile threads ask for UTILITY so they go to
#   the efficiency cores instead of stealing CPU from the game during a race.
# Put NX_STOCK_QOS=1 in env.txt to turn this off.
k = pathlib.Path("crates/nxhost/src/kernel.rs")
v = k.read_text()
old = "        crate::diag::install_altstack();\n        let mut ctx = GuestThread::new(t.handle, t.tlr);"
assert v.count(old) == 1, "guest thread spawn not found"
v = v.replace(old, "        crate::diag::install_altstack();\n"
    "        if std::env::var_os(\"NX_STOCK_QOS\").is_none() {\n"
    "            unsafe { libc::pthread_set_qos_class_self_np(libc::qos_class_t::QOS_CLASS_USER_INITIATED, 0) };\n"
    "        }\n"
    "        let mut ctx = GuestThread::new(t.handle, t.tlr);")
k.write_text(v)
print("patched", k)

sh = pathlib.Path("crates/nxhost/src/nvn/draw/shaders.rs")
w = sh.read_text()
old = "                    let job = rx.lock().unwrap().recv();"
assert w.count(old) == 1, "shader job loop not found"
w = w.replace(old, "                    if std::env::var_os(\"NX_STOCK_QOS\").is_none() {\n"
    "                        unsafe { libc::pthread_set_qos_class_self_np(libc::qos_class_t::QOS_CLASS_UTILITY, 0) };\n"
    "                    }\n" + old)
sh.write_text(w)
print("patched", sh)

# Optional, off by default: NX_ASYNC_ALL=1 in env.txt never blocks a frame on a
# shader/pipeline compile, even for a new render target. Fewer freezes, but
# some effects can be missing or wrong for a moment.
ex = pathlib.Path("crates/nxhost/src/nvn/draw/exec.rs")
x = ex.read_text()
old = "        let wait = self.pass.as_ref().unwrap().fresh;"
assert x.count(old) == 1, "wait flag not found"
x = x.replace(old, "        let wait = self.pass.as_ref().unwrap().fresh && std::env::var_os(\"NX_ASYNC_ALL\").is_none();")
ex.write_text(x)
print("patched", ex)

# On-device pipeline cache. Every pipeline the iPhone has to compile (0.5 to 4.5 s
# each on an A16) is also saved, in the background, into work/device.metalar.
# On the next launches that file is loaded and those pipelines come out ready in
# a few ms instead of being compiled again: fewer freezes each time you play.
# Put NX_NO_DEVICE_ARCHIVE=1 in env.txt to turn it off. Delete
# work/device.metalar to reset it (do it after an iOS update).
st = pathlib.Path("crates/nxhost/src/nvn/draw/state.rs")
u = st.read_text()

a0 = u.index("static ARCHIVES: LazyLock<Option<Shared<NSArray<Archive>>>> = LazyLock::new(|| {")
a1 = u.index("#[derive(Clone, PartialEq, Eq, Hash)]\nstruct Recorded")
u = u[:a0] + r'''const DEVICE_ARCHIVE: &str = "work/device.metalar";

fn device_archive_on() -> bool {
    std::env::var_os("NX_NO_DEVICE_ARCHIVE").is_none()
}

fn open_archive(path: Option<&str>) -> Option<Retained<Archive>> {
    let d = MTLBinaryArchiveDescriptor::new();
    if let Some(p) = path {
        d.setUrl(Some(&NSURL::fileURLWithPath(&NSString::from_str(p))));
    }
    gfx::gpu().device.0.newBinaryArchiveWithDescriptor_error(&d).ok()
}

static ARCHIVES: LazyLock<Option<Shared<NSArray<Archive>>>> = LazyLock::new(|| {
    let mut paths: Vec<String> = Vec::new();
    if std::env::var_os("NX_NO_PIPELINE_ARCHIVE").is_none() {
        let dir = super::shaders::aot_dir();
        let mut files: Vec<String> = std::fs::read_dir(dir)
            .map(|r| {
                r.flatten()
                    .map(|e| e.file_name().to_string_lossy().into_owned())
                    .filter(|n| n.starts_with("pipelines") && n.ends_with(".metalar"))
                    .collect()
            })
            .unwrap_or_default();
        files.sort();
        paths.extend(files.into_iter().map(|f| format!("{dir}/{f}")));
    }
    if device_archive_on() && std::path::Path::new(DEVICE_ARCHIVE).exists() {
        paths.push(DEVICE_ARCHIVE.to_string());
    }
    let archives: Vec<Retained<Archive>> = paths.iter().filter_map(|p| open_archive(Some(p))).collect();
    eprintln!("[gfx] {} pipeline archive(s) loaded ({})", archives.len(), paths.join(", "));
    (!archives.is_empty()).then(|| Shared(NSArray::from_retained_slice(&archives)))
});

fn harvest(d: Retained<MTLRenderPipelineDescriptor>) {
    static TX: LazyLock<Option<Mutex<std::sync::mpsc::Sender<Shared<MTLRenderPipelineDescriptor>>>>> = LazyLock::new(|| {
        if !device_archive_on() {
            return None;
        }
        if std::fs::metadata(DEVICE_ARCHIVE).map_or(false, |m| m.len() > 200 << 20) {
            eprintln!("[gfx] device pipeline archive is over 200 MB, not adding to it");
            return None;
        }
        let (tx, rx) = std::sync::mpsc::channel::<Shared<MTLRenderPipelineDescriptor>>();
        std::thread::Builder::new()
            .name("pipeline-archive".into())
            .spawn(move || {
                unsafe { libc::pthread_set_qos_class_self_np(libc::qos_class_t::QOS_CLASS_BACKGROUND, 0) };
                let existing = std::path::Path::new(DEVICE_ARCHIVE).exists();
                let Some(archive) = (if existing { open_archive(Some(DEVICE_ARCHIVE)) } else { None }).or_else(|| open_archive(None)) else {
                    eprintln!("[gfx] could not create the device pipeline archive");
                    return;
                };
                let archive = Shared(archive);
                let (mut added, mut pending) = (0usize, 0usize);
                loop {
                    let timed_out = match rx.recv_timeout(std::time::Duration::from_secs(5)) {
                        Ok(d) => {
                            objc2::rc::autoreleasepool(|_| {
                                d.0.setBinaryArchives(None);
                                if add(&archive, &d.0) {
                                    added += 1;
                                    pending += 1;
                                }
                            });
                            false
                        }
                        Err(std::sync::mpsc::RecvTimeoutError::Timeout) => true,
                        Err(_) => return,
                    };
                    if pending > 0 && (timed_out || pending >= 32) {
                        objc2::rc::autoreleasepool(|_| {
                            let _ = std::fs::create_dir_all("work");
                            let tmp = format!("{DEVICE_ARCHIVE}.tmp");
                            match archive.0.serializeToURL_error(&NSURL::fileURLWithPath(&NSString::from_str(&tmp))) {
                                Ok(()) => {
                                    let _ = std::fs::rename(&tmp, DEVICE_ARCHIVE);
                                    eprintln!("[gfx] device pipeline archive saved ({added} new pipelines this run)");
                                }
                                Err(e) => eprintln!("[gfx] could not save the device pipeline archive: {}", e.localizedDescription()),
                            }
                        });
                        pending = 0;
                    }
                }
            })
            .ok()?;
        Some(Mutex::new(tx))
    });
    if let Some(tx) = &*TX {
        let _ = tx.lock().unwrap().send(Shared(d));
    }
}

''' + u[a1:]

old = "    match dev.newRenderPipelineStateWithDescriptor_error(&d) {\n        Ok(p) => Some(Shared(p)),"
assert u.count(old) == 1, "pipeline build not found"
u = u.replace(old, "    match dev.newRenderPipelineStateWithDescriptor_error(&d) {\n        Ok(p) => {\n            harvest(d.clone());\n            Some(Shared(p))\n        }")

old = """                                if let Ok(p) = gfx::gpu().device.0.newRenderPipelineStateWithDescriptor_error(&d) {
                                    PREWARMED.lock().unwrap().insert(r.clone(), Shared(p));"""
assert u.count(old) == 1, "prewarm build not found"
u = u.replace(old, """                                let dev = &gfx::gpu().device.0;
                                let hit = (*ARCHIVES).as_ref().and_then(|_| dev.newRenderPipelineStateWithDescriptor_options_reflection_error(&d, MTLPipelineOption::FailOnBinaryArchiveMiss, None).ok());
                                let from_archive = hit.is_some();
                                if from_archive {
                                    PREWARM_HITS.fetch_add(1, Relaxed);
                                }
                                if let Some(p) = hit.or_else(|| dev.newRenderPipelineStateWithDescriptor_error(&d).ok()) {
                                    if !from_archive {
                                        harvest(d.clone());
                                    }
                                    PREWARMED.lock().unwrap().insert(r.clone(), Shared(p));""")

old = "            let (next, built) = (AtomicUsize::new(0), AtomicUsize::new(0));\n            std::thread::scope(|s| {\n                for _ in 0..4 {"
assert u.count(old) == 1, "prewarm counters not found"
u = u.replace(old, "            static PREWARM_HITS: AtomicUsize = AtomicUsize::new(0);\n" + old)
old = 'eprintln!("[gfx] prewarmed {}/{} recorded pipelines in {:.1} s", built.into_inner(), list.len(), t0.elapsed().as_secs_f32());'
assert u.count(old) == 1, "prewarm log not found"
u = u.replace(old, 'eprintln!("[gfx] prewarmed {}/{} recorded pipelines in {:.1} s ({} from the device archive)", built.into_inner(), list.len(), t0.elapsed().as_secs_f32(), PREWARM_HITS.load(Relaxed));')
st.write_text(u)
print("patched", st, "(device pipeline archive)")

# Optional, off by default: NX_TEX_DROP=1 (or 2) in env.txt drops the sharpest
# 1 (or 2) mip levels of the BC textures the iPhone has to decode on the CPU
# (the A16 cannot read them directly). Those textures then use 4x (or 16x) less
# RAM and decode faster: blurrier, but much less risk of iOS killing the app
# for memory. Textures smaller than NX_TEX_DROP_MIN pixels (default 128, after
# the skip) and textures with no mip levels are left alone.
tx = pathlib.Path("crates/nxhost/src/nvn/draw/textures.rs")
y = tx.read_text()

old = """        let levels = key.levels.min(max_levels).max(1);
        let d = MTLTextureDescriptor::new();
        unsafe {
            d.setTextureType(ty);
            d.setPixelFormat(format);
            d.setWidth(key.width as usize);
            d.setHeight(height as usize);"""
assert y.count(old) == 1, "texture create not found"
y = y.replace(old, """        let levels = key.levels.min(max_levels).max(1);
        let skip = tex_drop(&key, height, levels, render_target, depth_format);
        let levels = levels - skip;
        let d = MTLTextureDescriptor::new();
        unsafe {
            d.setTextureType(ty);
            d.setPixelFormat(format);
            d.setWidth((key.width >> skip).max(1) as usize);
            d.setHeight((height >> skip).max(1) as usize);""")

old = "fn upload(b: &TexBuilder, key: &TexKey, tex: &Texture, levels: u32) {\n"
assert y.count(old) == 1
y = y.replace(old, """fn tex_drop(key: &TexKey, height: u32, levels: u32, render_target: bool, depth_format: bool) -> u32 {
    static DROP: std::sync::LazyLock<u32> = std::sync::LazyLock::new(|| std::env::var("NX_TEX_DROP").ok().and_then(|v| v.trim().parse::<u32>().ok()).unwrap_or(0).min(3));
    static MIN: std::sync::LazyLock<u32> = std::sync::LazyLock::new(|| std::env::var("NX_TEX_DROP_MIN").ok().and_then(|v| v.trim().parse::<u32>().ok()).unwrap_or(128).max(64));
    let n = *DROP;
    if n == 0 || render_target || depth_format || gfx::bc::soft(key.format).is_none() || matches!(key.target, 2 | 3 | 10) {
        return 0;
    }
    if levels <= n || (key.width >> n) < *MIN || (height >> n) < *MIN {
        return 0;
    }
    n
}

/// How many top mip levels this Metal texture is missing compared to the guest texture.
fn dropped(key: &TexKey, tex: &Texture) -> u32 {
    let w = tex.width() as u32;
    if w == 0 || w >= key.width { 0 } else { (key.width / w).ilog2() }
}

""" + old)

old = """    let soft = gfx::bc::soft(key.format);
    let mut pieces = Vec::new();"""
assert y.count(old) == 1
y = y.replace(old, """    let soft = gfx::bc::soft(key.format);
    let skip = dropped(key, tex);
    let mut pieces = Vec::new();""")

old = """            let levels = levels.min(layout.level_offsets.len() as u32);
            for layer in 0..shape.layers {
                for level in 0..levels {"""
assert y.count(old) == 1
y = y.replace(old, """            let levels = (levels + skip).min(layout.level_offsets.len() as u32);
            for layer in 0..shape.layers {
                for level in skip..levels {""")

old = """                Some(k) => {
                    let mut data = vec![0u8; total];
                    fill(&mut data);
                    decode_and_stage(tex, k, data, pieces);"""
assert y.count(old) == 1
y = y.replace(old, """                Some(k) => {
                    let mut data = vec![0u8; total];
                    fill(&mut data);
                    for p in &mut pieces {
                        p.sub.0 -= skip;
                    }
                    decode_and_stage(tex, k, data, pieces);""")

old = """        None => {
            let stride = u32_at(b, 0x5c);"""
assert y.count(old) == 1
y = y.replace(old, """        None => {
            if skip > 0 {
                return;
            }
            let stride = u32_at(b, 0x5c);""")
tx.write_text(y)
print("patched", tx, "(NX_TEX_DROP)")

md = pathlib.Path("crates/nxhost/src/nvn/draw/mod.rs")
z = md.read_text()
old = "        let layered = matches!(tex.0.textureType(), MTLTextureType::Type2DArray | MTLTextureType::TypeCube | MTLTextureType::TypeCubeArray);\n        let slices = if layered { z..z + d.max(1) } else { 0..1 };\n        if tex.0.storageMode() == MTLStorageMode::Private {"
assert z.count(old) == 1, "write_texels not found"
z = z.replace(old, """        let skip = {
            let tw = tex.0.width() as u32;
            if tw == 0 || tw >= key.width { 0 } else { (key.width / tw).ilog2() as usize }
        };
        if level < skip {
            return;
        }
        let level = level - skip;
""" + old)
md.write_text(z)
print("patched", md, "(NX_TEX_DROP)")

# GPU binaries for the A16, built offline on the GitHub Mac with Apple's own
# translator (metal-tt), so the iPhone loads ready pipelines instead of
# compiling them (the 0.5 to 46 s freezes). make-ios.sh already builds
# work/shader_cache_ios.noindex/pipelines.metalar (pipeline list + Metal IR);
# tt-a16.sh turns it into applegpu_g15p binaries shipped in the app.
# If anything fails, the build still finishes, just without that file
# (see the "A16 archive log" step).
# Put NX_NO_PIPELINE_ARCHIVE=1 in env.txt to ignore it on the phone.
tt = pathlib.Path("tt-a16.sh")
tt.write_text(r'''#!/bin/sh
CACHE="$1"
SRC="$CACHE/pipelines.metalar"
OUT="$CACHE/pipelines-a16.metalar"
[ -f "$SRC" ] || { echo "[a16] no $SRC, nothing to translate"; exit 0; }
STAMP="$(shasum "$SRC" | cut -d' ' -f1)"
if [ -f "$OUT" ] && [ "$(cat "$OUT.stamp" 2>/dev/null)" = "$STAMP" ]; then echo "[a16] up to date"; exit 0; fi
# newest installed Xcode for metal-tt: its GPU compiler is the closest to the iPhone's iOS
DEV="$(python3 - <<'EOF'
import glob, plistlib
best = None
for app in glob.glob("/Applications/Xcode*.app"):
    try:
        v = plistlib.load(open(app + "/Contents/Info.plist", "rb"))["CFBundleShortVersionString"]
        k = tuple(int(x) for x in v.split(".") if x.isdigit())
        if best is None or k > best[0]:
            best = (k, app)
    except Exception:
        pass
print(best[1] + "/Contents/Developer" if best else "")
EOF
)"
[ -n "$DEV" ] && export DEVELOPER_DIR="$DEV"
echo "[a16] using $(xcodebuild -version | head -1) ($DEV)"
W="$CACHE/a16"
rm -rf "$W"; mkdir -p "$W"
xcrun metal-lipo -archs "$SRC" || true
xcrun metal-source -flatbuffers=json "$SRC" -o "$W/src" || { echo "[a16] metal-source failed"; exit 1; }
find "$W/src" -type f | head -20
J="$(find "$W/src" -type f -name '*.mtlp-json' | head -1)"
[ -n "$J" ] || { echo "[a16] no pipeline script in the archive"; exit 1; }
python3 - "$J" "$CACHE" "$W/src" <<'EOF' || exit 1
import glob, hashlib, json, os, sys
j, cache, src = sys.argv[1:]
d = json.load(open(j))
print("[a16] script keys:", list(d.keys()))
by_hash = {}
for f in glob.glob(cache + "/*.metallib") + glob.glob(src + "/**/*", recursive=True):
    if os.path.isfile(f) and not f.endswith("mtlp-json"):
        by_hash[hashlib.sha256(open(f, "rb").read()).hexdigest().upper()] = os.path.abspath(f)
libs = d.get("libraries", {}).get("paths", [])
ok = 0
for lib in libs:
    label, path = lib.get("label", ""), lib.get("path", "")
    cand = [path, os.path.join(os.path.dirname(j), path), os.path.join(src, path), os.path.join(cache, os.path.basename(path))]
    hit = next((os.path.abspath(c) for c in cand if c and os.path.isfile(c)), None) or by_hash.get(label.upper())
    if not hit:
        hit = next((f for f in glob.glob(src + "/**/*" + label + "*", recursive=True) if os.path.isfile(f)), None)
    if hit:
        lib["path"] = hit
        ok += 1
    else:
        print("[a16] library not found:", label, path)
print(f"[a16] {ok}/{len(libs)} libraries found")
print("[a16] script sizes:", {k: (len(v) if isinstance(v, (list, dict)) else v) for k, v in d.items()})
json.dump(d, open(j, "w"))
EOF
xcrun -sdk iphoneos metal-tt --help 2>&1 | head -80 || true
date
# at most 50 minutes, so the build can never run out of time because of this
perl -e 'alarm shift; exec @ARGV' 3000 xcrun -sdk iphoneos metal-tt -target air64-apple-ios26.0 "$J" -o "$W/all.metalar" || { echo "[a16] metal-tt failed or took too long"; exit 1; }
date
xcrun metal-lipo -archs "$W/all.metalar"
xcrun metal-lipo -thin applegpu_g15p -output "$OUT" "$W/all.metalar" || { echo "[a16] no applegpu_g15p slice"; exit 1; }
echo "$STAMP" > "$OUT.stamp"
rm -rf "$W"
ls -lh "$OUT"
echo "[a16] done"
''')
print("wrote", tt)

mk = pathlib.Path("make-ios.sh")
m = mk.read_text()
old = 'NX_MODULES_DIR="$PATCHED" IPHONEOS_DEPLOYMENT_TARGET=26.0 \\\n'
assert m.count(old) == 1, "make-ios.sh build line not found"
m = m.replace(old, 'sh ./tt-a16.sh "$CACHE" > work/a16.log 2>&1 || echo "A16 pipeline archive failed, building without it"\ntail -5 work/a16.log || true\n\n' + old)
old = 'cp "$LIST" "$RES/shaders/pipelines.tsv"\n'
assert m.count(old) == 1, "make-ios.sh copy line not found"
m = m.replace(old, old + 'if [ -f "$CACHE/pipelines-a16.metalar" ]; then cp "$CACHE/pipelines-a16.metalar" "$RES/shaders/"; fi\n')
mk.write_text(m)
print("patched", mk)

# Pipelines your iPhone recorded (Documents/work/pipelines.tsv), uploaded to the
# repo as phone-pipelines.tsv: they get precompiled for the A16 too.
ph = pathlib.Path("phone-pipelines.tsv")
if ph.exists():
    pathlib.Path("work").mkdir(exist_ok=True)
    pathlib.Path("work/pipelines.tsv").write_text(ph.read_text())
    print("using", ph, "(", len([l for l in ph.read_text().splitlines() if l.strip()]), "pipelines )")
