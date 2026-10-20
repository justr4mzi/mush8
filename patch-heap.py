#!/usr/bin/env python3
# Experimental patch for Mush8: when iOS refuses to give the GPU the whole
# guest heap in one buffer, retry with smaller sizes instead of giving up,
# and log the Metal / memory limits so we know exactly what iOS allows.
import os, pathlib, sys

p = pathlib.Path("crates/nxhost/src/gfx/mod.rs")
s = p.read_text()

start = s.index("pub fn wrap_heap(base: u64, size: u64) -> bool {")
end = s.index("pub fn heap() -> Option<&'static HeapBuffer>")

new = r'''#[cfg(target_os = "ios")]
pub fn available_memory() -> u64 {
    unsafe extern "C" {
        fn os_proc_available_memory() -> usize;
    }
    unsafe { os_proc_available_memory() as u64 }
}

#[cfg(not(target_os = "ios"))]
pub fn available_memory() -> u64 {
    0
}

/// Physical memory the app uses, as iOS counts it for its memory limit.
fn footprint() -> u64 {
    unsafe extern "C" {
        fn proc_pid_rusage(pid: i32, flavor: i32, buffer: *mut u64) -> i32;
    }
    let mut info = [0u64; 64];
    // RUSAGE_INFO_V0: 16-byte uuid, then user_time, system_time, pkg_idle_wkups,
    // interrupt_wkups, pageins, wired_size, resident_size, phys_footprint
    if unsafe { proc_pid_rusage(std::process::id() as i32, 0, info.as_mut_ptr()) } == 0 { info[9] } else { 0 }
}

/// Every 10 s: memory used by the app and memory iOS still allows (NX_NO_MEMLOG=1 to turn off).
fn start_memlog() {
    static ONCE: std::sync::Once = std::sync::Once::new();
    if std::env::var_os("NX_NO_MEMLOG").is_some() || ONCE.is_completed() {
        return;
    }
    ONCE.call_once(|| {});
    let _ = std::thread::Builder::new().name("memlog".into()).spawn(|| {
        let t0 = std::time::Instant::now();
        let mut low = u64::MAX;
        loop {
            std::thread::sleep(std::time::Duration::from_secs(10));
            let (used, left) = (footprint(), available_memory());
            low = low.min(left);
            eprintln!("[mem] {:.0} s: app uses {} MB, iOS still allows {} MB (lowest so far {} MB)", t0.elapsed().as_secs_f32(), used >> 20, left >> 20, low >> 20);
        }
    });
}

pub fn wrap_heap(base: u64, size: u64) -> bool {
    start_memlog();
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

# Prewarm: thread count left at 2.
st = pathlib.Path("crates/nxhost/src/nvn/draw/state.rs")
u = st.read_text()
old = "                for _ in 0..2 {\n                    s.spawn(|| {\n                        unsafe { libc::pthread_set_qos_class_self_np"
assert u.count(old) == 1, "prewarm loop not found"
# (kept at 2 threads: with 4 the game waited behind them, see below)
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
    if std::env::var_os("NX_PIPELINE_ARCHIVE").is_some() {
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
                unsafe { libc::pthread_set_qos_class_self_np(libc::qos_class_t::QOS_CLASS_UTILITY, 0) };
                let existing = std::path::Path::new(DEVICE_ARCHIVE).exists();
                let Some(archive) = (if existing { open_archive(Some(DEVICE_ARCHIVE)) } else { None }).or_else(|| open_archive(None)) else {
                    eprintln!("[gfx] could not create the device pipeline archive");
                    return;
                };
                let archive = Shared(archive);
                let (mut added, mut pending, mut rejected) = (0usize, 0usize, 0usize);
                loop {
                    let timed_out = match rx.recv_timeout(std::time::Duration::from_secs(2)) {
                        Ok(d) => {
                            objc2::rc::autoreleasepool(|_| {
                                d.0.setBinaryArchives(None);
                                // some pipelines (e.g. without a usable fragment stage) make the
                                // whole archive fail to serialize: check each one alone first
                                let ok = open_archive(None).is_some_and(|t| {
                                    let t = Shared(t);
                                    let probe = format!("{DEVICE_ARCHIVE}.probe");
                                    let r = if !add(&t, &d.0) {
                                        Err("could not add it".to_string())
                                    } else {
                                        t.0.serializeToURL_error(&NSURL::fileURLWithPath(&NSString::from_str(&probe))).map_err(|e| e.localizedDescription().to_string())
                                    };
                                    let _ = std::fs::remove_file(&probe);
                                    if let Err(e) = &r {
                                        rejected += 1;
                                        if rejected <= 3 {
                                            let e: String = e.chars().take(300).collect();
                                            eprintln!("[gfx] pipeline left out of the device archive: {e}");
                                        }
                                    }
                                    r.is_ok()
                                });
                                if ok && add(&archive, &d.0) {
                                    added += 1;
                                    pending += 1;
                                }
                            });
                            false
                        }
                        Err(std::sync::mpsc::RecvTimeoutError::Timeout) => true,
                        Err(_) => return,
                    };
                    if pending > 0 && (timed_out || pending >= 8) {
                        objc2::rc::autoreleasepool(|_| {
                            let _ = std::fs::create_dir_all("work");
                            let tmp = format!("{DEVICE_ARCHIVE}.tmp");
                            match archive.0.serializeToURL_error(&NSURL::fileURLWithPath(&NSString::from_str(&tmp))) {
                                Ok(()) => {
                                    let _ = std::fs::rename(&tmp, DEVICE_ARCHIVE);
                                    eprintln!("[gfx] device pipeline archive saved ({added} new pipelines this run, {rejected} left out)");
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

old = "            let (next, built) = (AtomicUsize::new(0), AtomicUsize::new(0));\n            std::thread::scope(|s| {\n                for _ in 0..2 {"
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
        if skip > 0 {
            DROPS.lock().unwrap().insert(key, skip);
        } else {
            DROPS.lock().unwrap().remove(&key);
        }
        let d = MTLTextureDescriptor::new();
        unsafe {
            d.setTextureType(ty);
            d.setPixelFormat(format);
            d.setWidth(key.width as usize);
            d.setHeight(height as usize);""")

old = "fn upload(b: &TexBuilder, key: &TexKey, tex: &Texture, levels: u32) {\n"
assert y.count(old) == 1
y = y.replace(old, """fn tex_drop(key: &TexKey, height: u32, levels: u32, render_target: bool, depth_format: bool) -> u32 {
    static DROP: std::sync::LazyLock<u32> = std::sync::LazyLock::new(|| std::env::var("NX_TEX_DROP").ok().and_then(|v| v.trim().parse::<u32>().ok()).unwrap_or(0).min(3));
    static MIN: std::sync::LazyLock<u32> = std::sync::LazyLock::new(|| std::env::var("NX_TEX_DROP_MIN").ok().and_then(|v| v.trim().parse::<u32>().ok()).unwrap_or(128).max(64));
    let n = *DROP;
    if n == 0 || render_target || depth_format || matches!(key.target, 2 | 3 | 10) {
        return 0;
    }
    // Lighting data must keep its exact size: cube maps (reflections, sky light) and
    // BC6 HDR textures (light maps, environment) turn whole tracks black when shrunk.
    // NX_TEX_DROP_ALL=1 shrinks them too.
    static ALL: std::sync::LazyLock<bool> = std::sync::LazyLock::new(|| std::env::var_os("NX_TEX_DROP_ALL").is_some());
    match gfx::bc::soft(key.format) {
        None => return 0,
        Some(gfx::bc::Kind::Bc6(_)) if !*ALL => return 0,
        _ if matches!(key.target, 8 | 9) && !*ALL => return 0,
        _ => {}
    }
    if levels <= n || (key.width >> n) < *MIN || (height >> n) < *MIN {
        return 0;
    }
    n
}

/// Textures whose sharpest mip levels are not decoded: those levels get an
/// enlarged copy of the first decoded level instead. The Metal texture keeps the
/// game's exact size and mip count (shrinking it turned some objects black:
/// shaders and copies expect the real size).
static DROPS: std::sync::LazyLock<std::sync::Mutex<std::collections::HashMap<TexKey, u32>>> = std::sync::LazyLock::new(Default::default);

fn dropped(key: &TexKey, _tex: &Texture) -> u32 {
    DROPS.lock().unwrap().get(key).copied().unwrap_or(0)
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
y = y.replace(old, """            let levels = levels.min(layout.level_offsets.len() as u32);
            let skip = if skip < levels { skip } else { 0 };
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
                    decode_and_stage(tex, k, data, pieces, skip, (key.width, key.height));""")

old = """        None => {
            let stride = u32_at(b, 0x5c);"""
assert y.count(old) == 1
y = y.replace(old, """        None => {
            let stride = u32_at(b, 0x5c);""")
tx.write_text(y)
print("patched", tx, "(NX_TEX_DROP)")

# (write_texels needs no change: dropped textures keep their full size)

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
# Build A16 (applegpu_g15p) GPU binaries for every recorded pipeline with
# Apple's metal-tt, from a pipeline script written by a16/gen.py.
CACHE="$1"
OUT="$CACHE/pipelines-a16.metalar"
LIST="$CACHE/pipelines.all.tsv"
[ -f "$LIST" ] && [ -f "$CACHE/index.tsv" ] || { echo "[a16] no shader cache yet"; exit 0; }
STAMP="$(cat "$LIST" "$CACHE/index.tsv" "$CACHE/guest.tsv" a16/gen.py | shasum | cut -d' ' -f1)"
if [ -f "$OUT" ] && [ "$(cat "$OUT.stamp" 2>/dev/null)" = "$STAMP" ]; then echo "[a16] up to date"; ls -lh "$OUT"; exit 0; fi
rm -f "$OUT" "$OUT.stamp"
python3 a16/gen.py "$CACHE" "$LIST" a16/metal-enums.json "$CACHE/a16.mtlp-json" || exit 1
date
perl -e 'alarm shift; exec @ARGV' 2400 xcrun -sdk iphoneos metal-tt -target air64-apple-ios26.0 -arch applegpu_g15p -compress-resources "$CACHE/a16.mtlp-json" -o "$OUT.tmp" || { rm -f "$OUT.tmp"; echo "[a16] metal-tt failed"; exit 1; }
date
rm -f "$CACHE/a16.mtlp-json"
mv "$OUT.tmp" "$OUT"
echo "$STAMP" > "$OUT.stamp"
xcrun metal-lipo -archs "$OUT"
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
# Not shipped for now: on iOS 27 the Metal driver crashes loading this archive
# (built with the Xcode 26.6 toolchain). Set SHIP_A16=1 to ship it anyway.
if os.environ.get("SHIP_A16"):
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

# Safety for NX_TEX_DROP: a texture with dropped mip levels has fewer levels in
# Metal than in the game, so never blit from/to a level that does not exist
# (that would be undefined behaviour on the GPU).
cp_ = pathlib.Path("crates/nxhost/src/nvn/draw/copy.rs")
c = cp_.read_text()
old = """        if sw == 0 || sh == 0 || dw == 0 || dh == 0 {
            return Ok(());
        }
        let (slw, slh) = level_size(&src.0, src_level);"""
assert c.count(old) == 1, "copy() start not found"
c = c.replace(old, """        if sw == 0 || sh == 0 || dw == 0 || dh == 0 {
            return Ok(());
        }
        if src_level as usize >= src.0.mipmapLevelCount() || dst_level as usize >= dst.0.mipmapLevelCount() {
            return Ok(());
        }
        let (slw, slh) = level_size(&src.0, src_level);""")
old = """    let [x, y, z, w, h, d] = r.map(|v| v.max(0) as usize);
    if w == 0 || h == 0 {
        return Ok(());
    }
    let (bw, bh, bpb) = super::super::format_info(nvn_format);"""
assert c.count(old) == 1, "copy_into() start not found"
c = c.replace(old, """    let [x, y, z, w, h, d] = r.map(|v| v.max(0) as usize);
    if w == 0 || h == 0 || level as usize >= src.mipmapLevelCount() {
        return Ok(());
    }
    let (bw, bh, bpb) = super::super::format_info(nvn_format);""")
cp_.write_text(c)
print("patched", cp_)

# When iOS is about to run out of memory for the app (less than NX_TRIM_MB,
# default 400 MB left), drop the textures the game has not used for 5 s.
# Only textures that can be rebuilt from the game's memory are dropped; they
# come back (decoded again) the next time the game draws with them.
# NX_TRIM_MB=0 in env.txt turns it off.
tx = pathlib.Path("crates/nxhost/src/nvn/draw/textures.rs")
y = tx.read_text()
old = "    uploaded: FastMap<TexKey, ([u64; WINDOWS], u64, u64)>,\n    pub frame: u64,\n}"
assert y.count(old) == 1, "Textures struct not found"
y = y.replace(old, "    uploaded: FastMap<TexKey, ([u64; WINDOWS], u64, u64)>,\n    last_used: FastMap<TexKey, u64>,\n    pub frame: u64,\n}")
old = "        if let Some(t) = self.map.get(&key) {\n            let t = t.clone();\n            let frame = self.frame;\n"
assert y.count(old) == 1, "texture hit path not found"
y = y.replace(old, "        if let Some(t) = self.map.get(&key) {\n            let t = t.clone();\n            let frame = self.frame;\n            self.last_used.insert(key, frame);\n")
old = "    pub fn cached(&self, key: &TexKey)"
assert y.count(old) == 1
y = y.replace(old, """    /// Drop textures unused for a while when iOS is close to killing the app.
    pub fn trim(&mut self) {
        static LIMIT: std::sync::LazyLock<u64> = std::sync::LazyLock::new(|| std::env::var("NX_TRIM_MB").ok().and_then(|v| v.trim().parse::<u64>().ok()).unwrap_or(400) << 20);
        if *LIMIT == 0 || gfx::available_memory() == 0 || gfx::available_memory() > *LIMIT {
            return;
        }
        let frame = self.frame;
        let old: std::collections::HashSet<TexKey> = self
            .uploaded
            .keys()
            .filter(|k| self.last_used.get(*k).map_or(true, |&f| f + 300 < frame))
            .copied()
            .collect();
        if old.is_empty() {
            return;
        }
        self.evict(&|k| old.contains(k));
        self.last_used.retain(|k, _| !old.contains(k));
        eprintln!("[mem] low memory ({} MB left): dropped {} unused textures", gfx::available_memory() >> 20, old.len());
    }

""" + old)
old = "        self.uploaded.retain(|k, _| !gone(k));\n    }"
assert y.count(old) == 1
y = y.replace(old, "        self.uploaded.retain(|k, _| !gone(k));\n        self.last_used.retain(|k, _| !gone(k));\n    }")
tx.write_text(y)
md = pathlib.Path("crates/nxhost/src/nvn/draw/mod.rs")
z = md.read_text()
old = "        e.textures.frame += 1;\n"
assert z.count(old) == 1, "frame counter not found"
z = z.replace(old, "        e.textures.frame += 1;\n        if e.textures.frame % 30 == 0 {\n            e.textures.trim();\n        }\n")
md.write_text(z)
print("patched", tx, md, "(low-memory texture trim)")

# ---------------------------------------------------------------------------
# Profiling ([prof] line next to [fps], when NX_FPS=1), frameskip
# (NX_FRAMESKIP=N) and less frequent texture change checks (NX_HASH_EVERY=N).
gm = pathlib.Path("crates/nxhost/src/gfx/mod.rs")
g = gm.read_text()
old = "pub mod bc;\n"
assert g.count(old) == 1
g = g.replace(old, old + '''pub mod prof {
    //! Time counters, printed once per second by nvn::draw::count_fps (NX_FPS=1).
    use std::sync::atomic::{AtomicU64, Ordering::Relaxed};
    pub static SUBMIT_NS: AtomicU64 = AtomicU64::new(0);
    pub static UPLOAD_NS: AtomicU64 = AtomicU64::new(0);
    pub static PRESENT_WAIT_NS: AtomicU64 = AtomicU64::new(0);
    pub static PIPE_NS: AtomicU64 = AtomicU64::new(0);
    pub static DRAWS: AtomicU64 = AtomicU64::new(0);
    pub static SKIPPED: AtomicU64 = AtomicU64::new(0);
    pub struct Timer(&'static AtomicU64, std::time::Instant);
    impl Timer {
        pub fn new(a: &'static AtomicU64) -> Self {
            Timer(a, std::time::Instant::now())
        }
    }
    impl Drop for Timer {
        fn drop(&mut self) {
            self.0.fetch_add(self.1.elapsed().as_nanos() as u64, Relaxed);
        }
    }
    pub fn take_ms(a: &AtomicU64) -> f64 {
        a.swap(0, Relaxed) as f64 / 1e6
    }
}
''')
old = "    let Some(drawable) = layer.0.nextDrawable() else { return };\n"
assert g.count(old) == 1
g = g.replace(old, "    let drawable = { let _t = prof::Timer::new(&prof::PRESENT_WAIT_NS); layer.0.nextDrawable() };\n    let Some(drawable) = drawable else { return };\n")
gm.write_text(g)

md = pathlib.Path("crates/nxhost/src/nvn/draw/mod.rs")
z = md.read_text()
old = "pub fn submit(handles: &[u64]) {\n    gfx::wait_foreground();\n"
assert z.count(old) == 1
z = z.replace(old, old + "    let _prof = gfx::prof::Timer::new(&gfx::prof::SUBMIT_NS);\n")
old = "        e.last_submit = Some(cb);\n"
assert z.count(old) == 1
z = z.replace(old, '''        if prof_on() {
            let mut v = GPU_CBS.lock().unwrap();
            if v.len() < 2000 {
                v.push(cb.clone());
            }
        }
''' + old)
old = '''                Cmd::Draw(d) => {
                    let _ = self.draw(d);
                }'''
assert z.count(old) == 1
z = z.replace(old, '''                Cmd::Draw(d) => {
                    if skip_frame() {
                        continue;
                    }
                    gfx::prof::DRAWS.fetch_add(1, Relaxed);
                    let _ = self.draw(d);
                }''')
old = "fn count_fps() {\n"
assert z.count(old) == 1
z = z.replace(old, '''fn prof_on() -> bool {
    static ON: LazyLock<bool> = LazyLock::new(|| std::env::var_os("NX_FPS").is_some());
    *ON
}

static GPU_CBS: Mutex<Vec<Shared<gfx::CommandBuffer>>> = Mutex::new(Vec::new());

/// GPU time (ms) of the finished frame command buffers since the last call.
fn gpu_ms() -> f64 {
    let mut t = 0.0;
    GPU_CBS.lock().unwrap().retain(|cb| {
        if cb.0.status() == MTLCommandBufferStatus::Completed {
            t += (cb.0.GPUEndTime() - cb.0.GPUStartTime()).max(0.0);
            false
        } else {
            cb.0.status() != MTLCommandBufferStatus::Error
        }
    });
    t * 1000.0
}

static FRAME: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);

static FRAMESKIP_ON: LazyLock<bool> = LazyLock::new(|| std::env::var("NX_FRAMESKIP").ok().and_then(|v| v.trim().parse::<u64>().ok()).unwrap_or(0) > 0);

/// With frameskip, a skipped frame never waits for the screen, so the game
/// would run faster than 60 updates per second. Keep a 60 Hz cadence: a
/// skipped frame sleeps until its slot; shown frames are paced by the display.
fn pace_frame(skip: bool) {
    const IV: std::time::Duration = std::time::Duration::from_micros(16_667);
    static NEXT: Mutex<Option<std::time::Instant>> = Mutex::new(None);
    let now = std::time::Instant::now();
    let deadline = {
        let n = NEXT.lock().unwrap();
        match *n {
            Some(d) if d + IV * 3 > now => d,
            _ => now,
        }
    };
    if skip && deadline > now {
        crate::kernel::blocking(|| std::thread::sleep(deadline - now));
    }
    *NEXT.lock().unwrap() = Some(deadline.max(now) + IV);
}

/// NX_FRAMESKIP=N: draw and show 1 frame out of N+1; the game itself still runs every frame.
fn skip_frame() -> bool {
    static N: LazyLock<u64> = LazyLock::new(|| std::env::var("NX_FRAMESKIP").ok().and_then(|v| v.trim().parse::<u64>().ok()).unwrap_or(0).min(4));
    *N > 0 && FRAME.load(Relaxed) % (*N + 1) != 0
}

''' + old)
old = '''        eprintln!("[fps] {:.1} fps, worst frame {worst:.1} ms", *n as f32 / now.duration_since(*start).as_secs_f32());
'''
assert z.count(old) == 1
z = z.replace(old, old + '''        let secs = now.duration_since(*start).as_secs_f64() * 1000.0;
        let frames = (*n).max(1) as f64;
        let (submit, upload, wait, pipe) = (
            gfx::prof::take_ms(&gfx::prof::SUBMIT_NS),
            gfx::prof::take_ms(&gfx::prof::UPLOAD_NS),
            gfx::prof::take_ms(&gfx::prof::PRESENT_WAIT_NS),
            gfx::prof::take_ms(&gfx::prof::PIPE_NS),
        );
        let gpu = gpu_ms();
        let draws = gfx::prof::DRAWS.swap(0, Relaxed);
        let skipped = gfx::prof::SKIPPED.swap(0, Relaxed);
        eprintln!(
            "[prof] per frame: game/other {:.1} ms, gfx cpu {:.1} ms (texture uploads {:.1}), waiting for display {:.1} ms, gpu {:.1} ms, {} draws | pipeline builds {:.0} ms/s, frames not shown {}",
            ((secs - submit - wait) / frames).max(0.0),
            submit / frames,
            upload / frames,
            wait / frames,
            gpu / frames,
            draws / (*n).max(1) as u64,
            pipe * 1000.0 / secs,
            skipped
        );
        #[cfg(target_os = "ios")]
        crate::gfx::show_fps(format!("{:.0} fps", ((frames - skipped as f64) * 1000.0 / secs).max(0.0)));
''')
old = "pub fn present(texture: u64) {\n    count_fps();\n"
assert z.count(old) == 1
z = z.replace(old, '''pub fn present(texture: u64) {
    count_fps();
    let skip = skip_frame();
    FRAME.fetch_add(1, Relaxed);
    if *FRAMESKIP_ON {
        pace_frame(skip);
    }
    if skip {
        gfx::prof::SKIPPED.fetch_add(1, Relaxed);
        return;
    }
''')
md.write_text(z)

tx = pathlib.Path("crates/nxhost/src/nvn/draw/textures.rs")
y = tx.read_text()
old = "fn upload(b: &TexBuilder, key: &TexKey, tex: &Texture, levels: u32) {\n"
assert y.count(old) == 1
y = y.replace(old, old + "    let _prof = gfx::prof::Timer::new(&gfx::prof::UPLOAD_NS);\n")
# NX_HASH_EVERY=N: look for game-side texture changes every N frames instead of every frame
old = "                    if checked != frame {\n"
assert y.count(old) == 1
y = y.replace(old, '''                    static EVERY: std::sync::LazyLock<u64> = std::sync::LazyLock::new(|| std::env::var("NX_HASH_EVERY").ok().and_then(|v| v.trim().parse::<u64>().ok()).unwrap_or(1).clamp(1, 30));
                    if checked != frame && frame >= checked + *EVERY {
''')
tx.write_text(y)

st = pathlib.Path("crates/nxhost/src/nvn/draw/state.rs")
u = st.read_text()
old = "    let r = build_pipeline_inner(vs, fs, t, color_state, channel_mask, blend, name);\n"
assert u.count(old) == 1
u = u.replace(old, "    let r = {\n        let _prof = gfx::prof::Timer::new(&gfx::prof::PIPE_NS);\n        build_pipeline_inner(vs, fs, t, color_state, channel_mask, blend, name)\n    };\n")
st.write_text(u)
print("patched profiling, NX_FRAMESKIP, NX_HASH_EVERY")

# Watchdog: if no frame is shown for 6 s (a freeze), write every thread's
# position (symbolized, with a short backtrace) to the log, once per freeze.
# NX_NO_WATCHDOG=1 turns it off.
md = pathlib.Path("crates/nxhost/src/nvn/draw/mod.rs")
z = md.read_text()
old = "pub fn present(texture: u64) {\n    count_fps();\n"
assert z.count(old) == 1
z = z.replace(old, "pub fn present(texture: u64) {\n    start_watchdog();\n    count_fps();\n")
z += r'''

fn start_watchdog() {
    static ONCE: std::sync::Once = std::sync::Once::new();
    if std::env::var_os("NX_NO_WATCHDOG").is_some() {
        return;
    }
    ONCE.call_once(|| {
        let _ = std::thread::Builder::new().name("watchdog".into()).spawn(|| {
            let (mut last, mut same, mut dumped) = (FRAME.load(Relaxed), 0u32, false);
            // NX_DUMP_EVERY=N: also dump every thread's position every N seconds,
            // for freezes where frames keep coming but the game ignores input.
            let every: u64 = std::env::var("NX_DUMP_EVERY").ok().and_then(|v| v.trim().parse().ok()).unwrap_or(0);
            let mut secs = 0u64;
            loop {
                std::thread::sleep(std::time::Duration::from_secs(1));
                secs += 1;
                if every > 0 && secs % every == 0 {
                    eprintln!("[hang] periodic dump at {secs} s (frame {}): thread positions", FRAME.load(Relaxed));
                    dump_threads();
                }
                let f = FRAME.load(Relaxed);
                if f != last {
                    (last, same, dumped) = (f, 0, false);
                    continue;
                }
                same += 1;
                if same >= 6 && !dumped {
                    dumped = true;
                    eprintln!("[hang] no frame for {same} s (frame {f}): thread positions");
                    dump_threads();
                }
            }
        });
    });
}

// Guest addresses via diag::symbolize; anything else (Mush8 itself, iOS
// libraries) via dladdr: "image+offset symbol+offset".
fn host_sym(addr: u64) -> String {
    let s = crate::diag::symbolize(addr);
    if !s.starts_with("0x") || addr == 0 {
        return s;
    }
    unsafe {
        let mut info: libc::Dl_info = std::mem::zeroed();
        if libc::dladdr(addr as *const libc::c_void, &mut info) == 0 || info.dli_fname.is_null() {
            return s;
        }
        let file = std::ffi::CStr::from_ptr(info.dli_fname).to_string_lossy();
        let file = file.rsplit('/').next().unwrap_or("?").to_string();
        let mut out = format!("{file}+{:#x}", addr - info.dli_fbase as u64);
        if !info.dli_sname.is_null() {
            let n = std::ffi::CStr::from_ptr(info.dli_sname).to_string_lossy();
            let n: String = n.chars().take(120).collect();
            out.push_str(&format!(" {n}+{:#x}", addr - info.dli_saddr as u64));
        }
        out
    }
}

fn dump_threads() {
    unsafe extern "C" {
        fn task_threads(task: u32, list: *mut *mut u32, count: *mut u32) -> i32;
        fn thread_get_state(t: u32, flavor: i32, state: *mut u32, count: *mut u32) -> i32;
        fn thread_suspend(t: u32) -> i32;
        fn thread_resume(t: u32) -> i32;
        fn mach_thread_self() -> u32;
        fn vm_read_overwrite(task: u32, addr: u64, size: u64, data: u64, out: *mut u64) -> i32;
        fn pthread_from_mach_thread_np(t: u32) -> libc::pthread_t;
        fn pthread_getname_np(t: libc::pthread_t, name: *mut libc::c_char, len: usize) -> i32;
        static mach_task_self_: u32;
    }
    const ARM_THREAD_STATE64: i32 = 6;
    unsafe {
        let task = mach_task_self_;
        let me = mach_thread_self();
        let (mut list, mut n) = (std::ptr::null_mut(), 0u32);
        if task_threads(task, &mut list, &mut n) != 0 {
            eprintln!("[hang] task_threads failed");
            return;
        }
        // grab every state first (no allocation while a thread is suspended)
        let mut states: Vec<(u32, [u64; 34])> = Vec::with_capacity(n as usize);
        for i in 0..n as usize {
            let t = *list.add(i);
            if t == me {
                continue;
            }
            let mut st = [0u64; 34];
            let mut count = 68u32;
            thread_suspend(t);
            let r = thread_get_state(t, ARM_THREAD_STATE64, st.as_mut_ptr() as *mut u32, &mut count);
            thread_resume(t);
            if r == 0 {
                states.push((t, st));
            }
        }
        let read = |addr: u64| -> Option<u64> {
            let mut v = 0u64;
            let mut got = 0u64;
            (addr != 0 && addr % 8 == 0 && vm_read_overwrite(task, addr, 8, &mut v as *mut u64 as u64, &mut got) == 0 && got == 8).then_some(v)
        };
        for (t, st) in states {
            // x0..x28, fp (29), lr (30), sp (31), pc (32)
            let (fp, lr, pc) = (st[29], st[30], st[32]);
            let mut name = [0 as libc::c_char; 64];
            pthread_getname_np(pthread_from_mach_thread_np(t), name.as_mut_ptr(), name.len());
            let name = std::ffi::CStr::from_ptr(name.as_ptr()).to_string_lossy().into_owned();
            let mut line = format!("[hang] thread {t:#x} '{name}': pc {} | lr {}", host_sym(pc), host_sym(lr));
            let mut f = fp;
            for _ in 0..16 {
                let (Some(next), Some(ret)) = (read(f), read(f.wrapping_add(8))) else { break };
                if ret == 0 {
                    break;
                }
                line.push_str(&format!(" <- {}", host_sym(ret)));
                if next <= f {
                    break;
                }
                f = next;
            }
            eprintln!("{line}");
        }
    }
}
'''
md.write_text(z)
print("patched", md, "(freeze watchdog)")

# Mii GetIndex (cmd 21): a real Switch answers "not found" (module 126, desc 1)
# for a Mii that is not in the database, e.g. the default Mii the game saved
# for player 1. Answering success with index -1 froze the game on
# Solo > 1 player once a Mii had been saved.
mi = pathlib.Path("crates/nxhost/src/services/mii.rs")
z = mi.read_text()
old = "            21 => rsp.push_u32(u32::MAX),\n"
assert z.count(old) == 1
z = z.replace(old, "            21 => {\n                eprintln!(\"[mii] GetIndex: Mii not in the database, answering not found\");\n                return 0x27E;\n            }\n")
mi.write_text(z)
print("patched", mi, "(GetIndex not found)")

# svcSignalToAddress, SignalAndModifyByWaitingCountIfEqual (kind 2): compute the
# new value exactly like the real kernel (Atmosphere/yuzu KAddressArbiter):
# - count <= 0: value - 2 if anyone waits, else value + 1
# - count > 0 : look at the waiters AFTER the first one (up to count+1 of them):
#     none -> value + 1, <= count -> value - 1, more -> value
# Mush8 counted all waiters (off by one) and used value - 1 for count <= 0, so
# nn::os light events/semaphores could believe nobody (or somebody) was still
# waiting: a timing-dependent lost wakeup (menus freezing on Solo > 1 player).
kp = pathlib.Path("crates/nxhost/src/kernel.rs")
z = kp.read_text()
old = """            let new = if count <= 0 {
                if waiting > 0 { value - 1 } else { value + 1 }
            } else if waiting == 0 {
                value + 1
            } else if waiting <= count {
                value - 1
            } else {
                value
            };"""
assert z.count(old) == 1
new = """            let new = if waiting == 0 {
                value.wrapping_add(1)
            } else if count <= 0 {
                value.wrapping_sub(2)
            } else {
                let others = (waiting - 1).min(count + 1);
                if others == 0 {
                    value.wrapping_add(1)
                } else if others <= count {
                    value.wrapping_sub(1)
                } else {
                    value
                }
            };"""
z = z.replace(old, new)
kp.write_text(z)
print("patched", kp, "(SignalToAddress waiting-count semantics)")

# NX_TRACE_SYMS helpers for timing experiments: NX_TRACE_QUIET=1 hooks the
# functions without printing anything, NX_TRACE_DELAY_US=N waits N microseconds
# after each hooked call (the printing used to slow these calls down, and that
# alone made Solo > 1 player stop freezing).
lp = pathlib.Path("crates/nxhost/src/loader.rs")
z = lp.read_text()
old = '''    let r = f(a.0[0], a.0[1], a.0[2], a.0[3], a.0[4], a.0[5], a.0[6], a.0[7]);
    eprintln!("[trace] {name}({:#x}, {:#x}, {:#x}, {:#x}) -> {r:#x}", a.0[0], a.0[1], a.0[2], a.0[3]);
'''
assert z.count(old) == 1
new = '''    let r = f(a.0[0], a.0[1], a.0[2], a.0[3], a.0[4], a.0[5], a.0[6], a.0[7]);
    static QUIET: std::sync::LazyLock<bool> = std::sync::LazyLock::new(|| std::env::var_os("NX_TRACE_QUIET").is_some());
    static DELAY: std::sync::LazyLock<u64> = std::sync::LazyLock::new(|| std::env::var("NX_TRACE_DELAY_US").ok().and_then(|v| v.trim().parse().ok()).unwrap_or(0));
    if *DELAY > 0 {
        std::thread::sleep(std::time::Duration::from_micros(*DELAY));
    }
    if *QUIET {
        return r;
    }
    eprintln!("[trace] {name}({:#x}, {:#x}, {:#x}, {:#x}) -> {r:#x}", a.0[0], a.0[1], a.0[2], a.0[3]);
'''
z = z.replace(old, new)
lp.write_text(z)
print("patched", lp, "(NX_TRACE_QUIET, NX_TRACE_DELAY_US)")


# On-screen fps counter (top centre) whenever NX_FPS=1 is in env.txt.
io = pathlib.Path("crates/nxhost/src/gfx/ios.rs")
z = io.read_text()
z = z.replace("use objc2_core_foundation::CGRect;\n", "use objc2_core_foundation::{CGPoint, CGRect, CGSize};\n")
z += """
thread_local! {
    static FPS_LABEL: RefCell<Option<Retained<UILabel>>> = const { RefCell::new(None) };
}

/// Shows `text` in a small label at the top of the screen (NX_FPS=1).
pub fn show_fps(text: String) {
    dispatch2::DispatchQueue::main().exec_async(move || {
        let Some(mtm) = MainThreadMarker::new() else { return };
        FPS_LABEL.with(|cell| {
            let mut cell = cell.borrow_mut();
            if cell.is_none() {
                let Some(view) = root_view() else { return };
                let w = UICoordinateSpace::bounds(&*view).size.width;
                let label = UILabel::new(mtm);
                label.setFrame(CGRect::new(CGPoint::new(w / 2.0 - 50.0, 4.0), CGSize::new(100.0, 22.0)));
                unsafe { label.setTextColor(Some(&UIColor::yellowColor())) };
                label.setBackgroundColor(Some(&UIColor::blackColor()));
                label.setTextAlignment(NSTextAlignment::Center);
                label.setUserInteractionEnabled(false);
                view.addSubview(&label);
                *cell = Some(label);
            }
            if let Some(label) = cell.as_ref() {
                label.setText(Some(&NSString::from_str(&text)));
            }
        })
    });
}
"""
io.write_text(z)
gm = pathlib.Path("crates/nxhost/src/gfx/mod.rs")
g = gm.read_text()
old = "pub use ios::{run_app, set_touch_controls, wait_foreground};"
assert g.count(old) == 1
g = g.replace(old, "pub use ios::{run_app, set_touch_controls, show_fps, wait_foreground};")
gm.write_text(g)
print("patched", io, "(on-screen fps)")

# NX_FRAMESKIP, smarter: on a skipped frame only skip the draws and clears into
# render targets that were also rendered in the last N frames (the per-frame
# scene, shadows, post effects...). Targets rendered once or rarely (kart
# preview, Mii faces, environment maps, the "1st race" scene setup) are still
# drawn, otherwise they stayed black.
md = pathlib.Path("crates/nxhost/src/nvn/draw/mod.rs")
z = md.read_text()
old = """                Cmd::Draw(d) => {
                    if skip_frame() {
                        continue;
                    }"""
assert z.count(old) == 1
z = z.replace(old, """                Cmd::Draw(d) => {
                    if skip_frame() && SKIP_RT.load(Relaxed) {
                        continue;
                    }""")
old = """                Cmd::SetRenderTargets { colors, depth } => {
"""
assert z.count(old) == 1
z = z.replace(old, old + """                    note_render_targets(colors, depth);
""")
for arm in ("                Cmd::ClearColor { index, rgba } => {\n", "                Cmd::ClearDepthStencil { depth: z, stencil } => {\n"):
    assert z.count(arm) == 1, arm
    z = z.replace(arm, arm + """                    if skip_frame() && SKIP_RT.load(Relaxed) {
                        continue;
                    }
""")
old = "/// NX_FRAMESKIP=N: draw and show 1 frame out of N+1; the game itself still runs every frame.\n"
assert z.count(old) == 1
z = z.replace(old, '''static SKIP_RT: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);
static RT_REPEAT: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);

/// True when the current render targets were also rendered in one of the last
/// 2 frames: a pipeline still compiling can then be skipped for a frame or two
/// instead of stalling the whole game (see exec.rs).
fn rt_repeat() -> bool {
    static OFF: LazyLock<bool> = LazyLock::new(|| std::env::var_os("NX_NO_ASYNC_REPEAT").is_some());
    !*OFF && RT_REPEAT.load(Relaxed)
}

/// Called for every render target change when frameskip is on: decides whether
/// the draws into these targets may be skipped on a skipped frame (only if the
/// same targets were rendered in one of the last N frames).
fn note_render_targets(colors: &[Option<Box<cmd::TexBuilder>>], depth: &Option<Box<cmd::TexBuilder>>) {
    static LAST: LazyLock<Mutex<std::collections::HashMap<u64, u64>>> = LazyLock::new(|| Mutex::new(std::collections::HashMap::new()));
    static N: LazyLock<u64> = LazyLock::new(|| std::env::var("NX_FRAMESKIP").ok().and_then(|v| v.trim().parse::<u64>().ok()).unwrap_or(0).min(4));
    let mut h = 0xcbf2_9ce4_8422_2325u64;
    let mut mix = |b: &[u8]| {
        for &x in b {
            h = (h ^ x as u64).wrapping_mul(0x100_0000_01b3);
        }
    };
    for c in colors.iter().chain(std::iter::once(depth)) {
        match c {
            Some(b) => mix(&b[..]),
            None => mix(&[0xff]),
        }
    }
    let frame = FRAME.load(Relaxed);
    let mut last = LAST.lock().unwrap();
    let prev = last.get(&h).copied();
    RT_REPEAT.store(prev.is_some_and(|f| frame > f && frame - f <= (*N).max(1) + 1), Relaxed);
    let recent = prev.is_some_and(|f| frame > f && frame - f <= *N);
    let skip = *N > 0 && skip_frame() && recent;
    SKIP_RT.store(skip, Relaxed);
    if !skip {
        last.insert(h, frame);
        if last.len() > 4096 {
            last.retain(|_, f| frame.saturating_sub(*f) < 600);
        }
    }
}

''' + old)
md.write_text(z)
print("patched", md, "(frameskip keeps rarely rendered targets)")

# A render target the game draws every frame (the race scene, menus) no longer
# waits for a pipeline that is still compiling: the draw is skipped for a frame
# or two instead of freezing the game for up to several seconds. Targets drawn
# for the first time still wait (they could otherwise stay empty).
# NX_NO_ASYNC_REPEAT=1 in env.txt turns it off.
ex = pathlib.Path("crates/nxhost/src/nvn/draw/exec.rs")
x = ex.read_text()
old = "        let wait = self.pass.as_ref().unwrap().fresh && std::env::var_os(\"NX_ASYNC_ALL\").is_none();"
assert x.count(old) == 1
x = x.replace(old, "        let wait = self.pass.as_ref().unwrap().fresh && std::env::var_os(\"NX_ASYNC_ALL\").is_none() && !super::rt_repeat();")
ex.write_text(x)
print("patched", ex, "(no stall on targets drawn every frame)")


# NX_TEX_DROP, part 2: fill the dropped (sharpest) mip levels with a nearest-
# neighbour enlargement of the first decoded level, on the CPU (cheap compared
# with BC decoding, which is what NX_TEX_DROP saves).
tx = pathlib.Path("crates/nxhost/src/nvn/draw/textures.rs")
y = tx.read_text()
old = "fn decode_and_stage(tex: &Texture, k: gfx::bc::Kind, data: Vec<u8>, mut pieces: Vec<Piece>) {\n"
assert y.count(old) == 1
y = y.replace(old, "fn decode_and_stage(tex: &Texture, k: gfx::bc::Kind, data: Vec<u8>, mut pieces: Vec<Piece>, skip: u32, full: (u32, u32)) {\n")
old = "    stage_with(tex, decoded.len(), &pieces, |dst| dst.copy_from_slice(&decoded));\n}"
assert y.count(old) == 1
y = y.replace(old, """    if skip > 0 {
        let mut extra = Vec::new();
        for p in pieces.iter().filter(|p| p.sub.0 == skip) {
            let (w, h, d) = p.size;
            let bpp = p.row / (w.max(1) as usize);
            if bpp == 0 || w == 0 || h == 0 {
                continue;
            }
            for l in 0..skip {
                let (lw, lh) = ((full.0 >> l).max(1), (full.1 >> l).max(1));
                let sh = skip - l;
                let row = lw as usize * bpp;
                let mut out = Vec::with_capacity(row * lh as usize * d as usize);
                for z in 0..d as usize {
                    for yy in 0..lh {
                        let sy = ((yy >> sh).min(h - 1)) as usize;
                        let src = p.offset + z * p.image + sy * p.row;
                        for xx in 0..lw {
                            let a = src + ((xx >> sh).min(w - 1)) as usize * bpp;
                            match decoded.get(a..a + bpp) {
                                Some(px) => out.extend_from_slice(px),
                                None => out.resize(out.len() + bpp, 0),
                            }
                        }
                    }
                }
                let offset = decoded.len();
                decoded.extend_from_slice(&out);
                extra.push(Piece { offset, row, image: row * lh as usize, size: (lw, lh, d), sub: (l, p.sub.1) });
                decoded.resize(decoded.len().next_multiple_of(16), 0);
            }
        }
        pieces.extend(extra);
    }
    stage_with(tex, decoded.len(), &pieces, |dst| dst.copy_from_slice(&decoded));
}""")
old = "                Some(k) => decode_and_stage(tex, k, src.to_vec(), pieces),\n"
assert y.count(old) == 1
y = y.replace(old, "                Some(k) => decode_and_stage(tex, k, src.to_vec(), pieces, 0, (key.width, key.height)),\n")
tx.write_text(y)
print("patched", tx, "(NX_TEX_DROP keeps full size)")


# The game first: prewarm (rebuilding every recorded pipeline at startup, 1-2
# minutes) used to compete with the pipelines the game needs right now; the
# menus could then wait up to a minute (the Solo > 1 player "freeze"). Prewarm
# now pauses while any pipeline the game asked for is being compiled.
st = pathlib.Path("crates/nxhost/src/nvn/draw/state.rs")
u = st.read_text()
old = """        self.pending.insert(key);
        super::shaders::spawn_job(Box::new(move || {
            let p = build_pipeline(&jvs, &jfs, &jt, cs, cm, &blend, &name);
            DONE.lock().unwrap().push((key, p));
        }));"""
assert u.count(old) == 1
u = u.replace(old, """        self.pending.insert(key);
        DEMAND.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
        super::shaders::spawn_job(Box::new(move || {
            let p = build_pipeline(&jvs, &jfs, &jt, cs, cm, &blend, &name);
            DONE.lock().unwrap().push((key, p));
            DEMAND.fetch_sub(1, std::sync::atomic::Ordering::SeqCst);
        }));""")
old = """            let p = crate::kernel::blocking(|| build_pipeline(&jvs, &jfs, &jt, cs, cm, &blend, &name));"""
assert u.count(old) == 1
u = u.replace(old, """            DEMAND.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
            let p = crate::kernel::blocking(|| build_pipeline(&jvs, &jfs, &jt, cs, cm, &blend, &name));
            DEMAND.fetch_sub(1, std::sync::atomic::Ordering::SeqCst);""")
old = """                        while let Some(r) = list.get(next.fetch_add(1, Relaxed)) {
"""
assert u.count(old) == 1
u = u.replace(old, """                        while let Some(r) = list.get(next.fetch_add(1, Relaxed)) {
                            while DEMAND.load(std::sync::atomic::Ordering::SeqCst) > 0 {
                                std::thread::sleep(std::time::Duration::from_millis(5));
                            }
""")
old = "pub fn prewarm() {\n"
assert u.count(old) == 1
u = u.replace(old, "/// Pipelines the game is waiting for right now (prewarm pauses meanwhile).\nstatic DEMAND: std::sync::atomic::AtomicUsize = std::sync::atomic::AtomicUsize::new(0);\n\n" + old)
st.write_text(u)
print("patched", st, "(prewarm yields to the game)")


# ---------------------------------------------------------------------------
# Texture decoding off the render thread (+ optional disk cache).
# The A16 cannot read the Switch's BC textures, so they are decoded on the CPU.
# That used to happen while the game waited (hitches when new things appear).
# Now the guest data is copied right away and decoded by 2 background threads;
# the texture shows up a few frames later instead of freezing the game.
# NX_SYNC_TEXTURES=1 in env.txt restores the old behaviour.
# Decoded textures are also kept in work/texcache (NX_TEX_CACHE_MB, default
# 1024 MB; NX_NO_TEX_CACHE=1 turns it off): next time they load from disk.
tx = pathlib.Path("crates/nxhost/src/nvn/draw/textures.rs")
y = tx.read_text()
old = "                    decode_and_stage(tex, k, data, pieces, skip, (key.width, key.height));\n"
assert y.count(old) == 1
y = y.replace(old, "                    decode_async(tex, k, data, pieces, skip, (key.width, key.height));\n")
old = "                Some(k) => decode_and_stage(tex, k, src.to_vec(), pieces, 0, (key.width, key.height)),\n"
assert y.count(old) == 1
y = y.replace(old, "                Some(k) => decode_async(tex, k, src.to_vec(), pieces, 0, (key.width, key.height)),\n")

old = "fn decode_and_stage(tex: &Texture, k: gfx::bc::Kind, data: Vec<u8>, mut pieces: Vec<Piece>, skip: u32, full: (u32, u32)) {\n"
assert y.count(old) == 1
y = y.replace(old, r"""type Job = Box<dyn FnOnce() + Send>;

/// Latest upload generation per Metal texture: an older decode finishing late
/// must not overwrite newer contents.
static GENS: std::sync::LazyLock<std::sync::Mutex<std::collections::HashMap<usize, u64>>> = std::sync::LazyLock::new(Default::default);

fn decode_pool() -> &'static std::sync::Mutex<std::sync::mpsc::Sender<Job>> {
    static POOL: std::sync::LazyLock<std::sync::Mutex<std::sync::mpsc::Sender<Job>>> = std::sync::LazyLock::new(|| {
        let (tx, rx) = std::sync::mpsc::channel::<Job>();
        let rx = std::sync::Arc::new(std::sync::Mutex::new(rx));
        for i in 0..2 {
            let rx = rx.clone();
            let _ = std::thread::Builder::new().name(format!("tex-decode-{i}")).spawn(move || {
                unsafe { libc::pthread_set_qos_class_self_np(libc::qos_class_t::QOS_CLASS_USER_INITIATED, 0) };
                loop {
                    let job = { rx.lock().unwrap().recv() };
                    match job {
                        Ok(job) => objc2::rc::autoreleasepool(|_| job()),
                        Err(_) => return,
                    }
                }
            });
        }
        std::sync::Mutex::new(tx)
    });
    &POOL
}

fn decode_async(tex: &Texture, k: gfx::bc::Kind, data: Vec<u8>, pieces: Vec<Piece>, skip: u32, full: (u32, u32)) {
    static SYNC: std::sync::LazyLock<bool> = std::sync::LazyLock::new(|| std::env::var_os("NX_SYNC_TEXTURES").is_some());
    if *SYNC {
        decode_and_stage(tex, k, data, pieces, skip, full);
        return;
    }
    let id = tex as *const Texture as *const u8 as usize;
    let generation = {
        let mut g = GENS.lock().unwrap();
        let e = g.entry(id).or_insert(0);
        *e += 1;
        *e
    };
    let t = Shared(unsafe { objc2::rc::Retained::retain(tex as *const Texture as *mut Texture) }.expect("texture"));
    let job: Job = Box::new(move || {
        let t = t; // capture the whole Shared (Send), not just its field
        if GENS.lock().unwrap().get(&id) != Some(&generation) {
            return;
        }
        decode_and_stage(&t.0, k, data, pieces, skip, full);
    });
    let _ = decode_pool().lock().unwrap().send(job);
}

fn tex_cache_dir() -> Option<&'static str> {
    static ON: std::sync::LazyLock<bool> = std::sync::LazyLock::new(|| {
        std::env::var_os("NX_NO_TEX_CACHE").is_none() && std::fs::create_dir_all("work/texcache").is_ok()
    });
    (*ON).then_some("work/texcache")
}

static TEX_CACHE_BYTES: std::sync::LazyLock<std::sync::atomic::AtomicU64> = std::sync::LazyLock::new(|| {
    let n = std::fs::read_dir("work/texcache").map(|r| r.flatten().filter_map(|e| e.metadata().ok()).map(|m| m.len()).sum()).unwrap_or(0);
    std::sync::atomic::AtomicU64::new(n)
});

fn tex_cache_limit() -> u64 {
    static L: std::sync::LazyLock<u64> = std::sync::LazyLock::new(|| std::env::var("NX_TEX_CACHE_MB").ok().and_then(|v| v.trim().parse().ok()).unwrap_or(1024u64) << 20);
    *L
}

fn tex_cache_key(k: gfx::bc::Kind, data: &[u8], pieces: &[Piece], skip: u32, full: (u32, u32)) -> u64 {
    let mut h = 0xcbf2_9ce4_8422_2325u64 ^ ((skip as u64) << 48) ^ ((full.0 as u64) << 24) ^ full.1 as u64;
    let mix = |h: &mut u64, v: u64| *h = (*h ^ v).wrapping_mul(0x100_0000_01b3).rotate_left(29);
    for b in format!("{:?}", std::mem::discriminant(&k)).bytes() {
        mix(&mut h, b as u64);
    }
    for p in pieces {
        mix(&mut h, p.offset as u64);
        mix(&mut h, p.row as u64);
        mix(&mut h, ((p.size.0 as u64) << 32) | p.size.1 as u64);
        mix(&mut h, ((p.sub.0 as u64) << 32) | p.sub.1 as u64);
    }
    let mut chunks = data.chunks_exact(8);
    for c in &mut chunks {
        mix(&mut h, u64::from_le_bytes(c.try_into().unwrap()));
    }
    for &b in chunks.remainder() {
        mix(&mut h, b as u64);
    }
    h
}

fn tex_cache_read(key: u64) -> Option<(Vec<u8>, Vec<Piece>)> {
    let dir = tex_cache_dir()?;
    let raw = std::fs::read(format!("{dir}/{key:016x}.bin")).ok()?;
    let u = |i: usize| raw.get(i..i + 8).map(|b| u64::from_le_bytes(b.try_into().unwrap()));
    let n = u(0)? as usize;
    let mut pieces = Vec::with_capacity(n);
    let mut at = 8;
    for _ in 0..n {
        let (o, r, im, a, b) = (u(at)?, u(at + 8)?, u(at + 16)?, u(at + 24)?, u(at + 32)?);
        pieces.push(Piece { offset: o as usize, row: r as usize, image: im as usize, size: ((a >> 32) as u32, a as u32, (b >> 40) as u32), sub: (((b >> 20) & 0xFFFFF) as u32, (b & 0xFFFFF) as u32) });
        at += 40;
    }
    Some((raw.get(at..)?.to_vec(), pieces))
}

fn tex_cache_write(key: u64, decoded: &[u8], pieces: &[Piece]) {
    let Some(dir) = tex_cache_dir() else { return };
    let total = (decoded.len() + 8 + pieces.len() * 40) as u64;
    if TEX_CACHE_BYTES.load(std::sync::atomic::Ordering::Relaxed) + total > tex_cache_limit() {
        return;
    }
    let mut out = Vec::with_capacity(total as usize);
    out.extend_from_slice(&(pieces.len() as u64).to_le_bytes());
    for p in pieces {
        for v in [p.offset as u64, p.row as u64, p.image as u64, ((p.size.0 as u64) << 32) | p.size.1 as u64, ((p.size.2 as u64) << 40) | ((p.sub.0 as u64 & 0xFFFFF) << 20) | (p.sub.1 as u64 & 0xFFFFF)] {
            out.extend_from_slice(&v.to_le_bytes());
        }
    }
    out.extend_from_slice(decoded);
    let (tmp, path) = (format!("{dir}/{key:016x}.tmp"), format!("{dir}/{key:016x}.bin"));
    if std::fs::write(&tmp, &out).is_ok() && std::fs::rename(&tmp, &path).is_ok() {
        TEX_CACHE_BYTES.fetch_add(total, std::sync::atomic::Ordering::Relaxed);
    }
}

fn decode_and_stage(tex: &Texture, k: gfx::bc::Kind, data: Vec<u8>, pieces: Vec<Piece>, skip: u32, full: (u32, u32)) {
    let key = tex_cache_dir().map(|_| tex_cache_key(k, &data, &pieces, skip, full));
    if let Some((decoded, pieces)) = key.and_then(tex_cache_read) {
        stage_with(tex, decoded.len(), &pieces, |dst| dst.copy_from_slice(&decoded));
        return;
    }
    let (decoded, pieces) = decode_pieces(k, data, pieces, skip, full);
    if let Some(key) = key {
        tex_cache_write(key, &decoded, &pieces);
    }
    stage_with(tex, decoded.len(), &pieces, |dst| dst.copy_from_slice(&decoded));
}

fn decode_pieces(k: gfx::bc::Kind, data: Vec<u8>, mut pieces: Vec<Piece>, skip: u32, full: (u32, u32)) -> (Vec<u8>, Vec<Piece>) {
""")
old = """        pieces.extend(extra);
    }
    stage_with(tex, decoded.len(), &pieces, |dst| dst.copy_from_slice(&decoded));
}"""
assert y.count(old) == 1
y = y.replace(old, """        pieces.extend(extra);
    }
    (decoded, pieces)
}""")
tx.write_text(y)
print("patched", tx, "(async texture decode, texture disk cache)")

# ---------------------------------------------------------------------------
# With NX_FRAMESKIP=N, show the drawn frames at an even pace (every N+1
# display refreshes) instead of "as soon as ready": steadier 30 fps.
gm = pathlib.Path("crates/nxhost/src/gfx/mod.rs")
g = gm.read_text()
old = "    let interval = PRESENT_INTERVAL.load(std::sync::atomic::Ordering::Relaxed);\n"
assert g.count(old) == 1
g = g.replace(old, """    static SKIP: std::sync::LazyLock<u32> = std::sync::LazyLock::new(|| std::env::var("NX_FRAMESKIP").ok().and_then(|v| v.trim().parse::<u32>().ok()).unwrap_or(0).min(4));
    let interval = PRESENT_INTERVAL.load(std::sync::atomic::Ordering::Relaxed).max(*SKIP + if *SKIP > 0 { 1 } else { 0 });
""")
gm.write_text(g)
print("patched", gm, "(even pacing with frameskip)")


# ---------------------------------------------------------------------------
# NX_TEX_16BIT=1: keep colour BC textures (BC1/2/3/7, decoded on the CPU) as
# 16-bit pixels instead of 32-bit: half the memory (iOS killed the app for
# memory on the character select). Less precise colours (banding).
# BC1 -> A1BGR5, BC2/3/7 -> ABGR4. NX_TEX_16BIT=2 uses the other bit order,
# in case the colours come out wrong with 1.
bcp = pathlib.Path("crates/nxhost/src/gfx/bc.rs")
b = bcp.read_text()
old = """        Kind::Bc6(_) => P::RGBA16Float,
        _ if srgb => P::RGBA8Unorm_sRGB,"""
assert b.count(old) == 1
b = b.replace(old, """        Kind::Bc6(_) => P::RGBA16Float,
        Kind::Bc1 if sixteen() > 0 => P::A1BGR5Unorm,
        _ if sixteen() > 0 => P::ABGR4Unorm,
        _ if srgb => P::RGBA8Unorm_sRGB,""")
b += """
/// NX_TEX_16BIT: 0 = off, 1 = 16-bit colour textures, 2 = same, other bit order.
pub fn sixteen() -> u32 {
    static S: std::sync::LazyLock<u32> = std::sync::LazyLock::new(|| std::env::var("NX_TEX_16BIT").ok().and_then(|v| v.trim().parse().ok()).unwrap_or(0).min(2));
    *S
}

/// Repack RGBA8 pixels (rows of `row` bytes, `w` pixels) into 16-bit pixels.
pub fn pack16(k: Kind, srgb: bool, src: &[u8], w: usize, h: usize, row: usize, out: &mut Vec<u8>) {
    static LIN: std::sync::LazyLock<[u8; 256]> = std::sync::LazyLock::new(|| {
        let mut t = [0u8; 256];
        for (i, v) in t.iter_mut().enumerate() {
            let c = i as f32 / 255.0;
            let l = if c <= 0.04045 { c / 12.92 } else { ((c + 0.055) / 1.055).powf(2.4) };
            *v = (l * 255.0 + 0.5) as u8;
        }
        t
    });
    let alt = sixteen() == 2;
    for y in 0..h {
        for x in 0..w {
            let i = y * row + x * 4;
            let Some(px) = src.get(i..i + 4) else {
                out.extend_from_slice(&[0, 0]);
                continue;
            };
            let (mut r, mut g, mut bl, a) = (px[0] as u16, px[1] as u16, px[2] as u16, px[3] as u16);
            if srgb {
                r = LIN[r as usize] as u16;
                g = LIN[g as usize] as u16;
                bl = LIN[bl as usize] as u16;
            }
            let v: u16 = match (k, alt) {
                (Kind::Bc1, false) => (a >> 7) | (bl >> 3) << 1 | (g >> 3) << 6 | (r >> 3) << 11,
                (Kind::Bc1, true) => (a >> 7) << 15 | (bl >> 3) << 10 | (g >> 3) << 5 | (r >> 3),
                (_, false) => (a >> 4) | (bl >> 4) << 4 | (g >> 4) << 8 | (r >> 4) << 12,
                (_, true) => (a >> 4) << 12 | (bl >> 4) << 8 | (g >> 4) << 4 | (r >> 4),
            };
            out.extend_from_slice(&v.to_le_bytes());
        }
    }
}
"""
bcp.write_text(b)

tx = pathlib.Path("crates/nxhost/src/nvn/draw/textures.rs")
y = tx.read_text()
# the sRGB flag rides in bit 16 of `skip`
old = "                    decode_async(tex, k, data, pieces, skip, (key.width, key.height));\n"
assert y.count(old) == 1
y = y.replace(old, "                    decode_async(tex, k, data, pieces, skip | srgb_bit(key.format), (key.width, key.height));\n")
old = "                Some(k) => decode_async(tex, k, src.to_vec(), pieces, 0, (key.width, key.height)),\n"
assert y.count(old) == 1
y = y.replace(old, "                Some(k) => decode_async(tex, k, src.to_vec(), pieces, srgb_bit(key.format), (key.width, key.height)),\n")
old = "fn decode_pieces(k: gfx::bc::Kind, data: Vec<u8>, mut pieces: Vec<Piece>, skip: u32, full: (u32, u32)) -> (Vec<u8>, Vec<Piece>) {\n"
assert y.count(old) == 1
y = y.replace(old, """fn srgb_bit(nvn: u32) -> u32 {
    if matches!(nvn, 69..=72 | 78) { 1 << 16 } else { 0 }
}

fn decode_pieces(k: gfx::bc::Kind, data: Vec<u8>, mut pieces: Vec<Piece>, skip: u32, full: (u32, u32)) -> (Vec<u8>, Vec<Piece>) {
    let (skip, srgb) = (skip & 0xFFFF, skip >> 16 != 0);
""")
old = "    if skip > 0 {\n        let mut extra = Vec::new();"
assert y.count(old) == 1
y = y.replace(old, """    if gfx::bc::sixteen() > 0 && matches!(k, gfx::bc::Kind::Bc1 | gfx::bc::Kind::Bc2 | gfx::bc::Kind::Bc3 | gfx::bc::Kind::Bc7) {
        let mut packed = Vec::with_capacity(decoded.len() / 2 + 64);
        for p in &mut pieces {
            let (w, h, d) = p.size;
            let offset = packed.len();
            for z in 0..d as usize {
                let start = p.offset + z * p.image;
                gfx::bc::pack16(k, srgb, decoded.get(start..).unwrap_or(&[]), w as usize, h as usize, p.row, &mut packed);
            }
            (p.offset, p.row, p.image) = (offset, w as usize * 2, w as usize * 2 * h as usize);
            packed.resize(packed.len().next_multiple_of(16), 0);
        }
        decoded = packed;
    }
    if skip > 0 {
        let mut extra = Vec::new();""")
# cache key must change with the 16-bit mode
old = "    let mut h = 0xcbf2_9ce4_8422_2325u64 ^ ((skip as u64) << 48) ^ ((full.0 as u64) << 24) ^ full.1 as u64;\n"
assert y.count(old) == 1
y = y.replace(old, old + "    h ^= (gfx::bc::sixteen() as u64) << 60;\n")
tx.write_text(y)
print("patched", bcp, tx, "(NX_TEX_16BIT)")
