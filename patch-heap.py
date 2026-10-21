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


# ---------------------------------------------------------------------------
# NX_TEX_DROP_SHRINK=1 (with NX_TEX_DROP=1..3): the old, stronger variant. The
# Metal texture really is smaller: up to 16x less memory and less GPU work for
# big textures, but some objects can turn black (shaders expect the real size).
tx = pathlib.Path("crates/nxhost/src/nvn/draw/textures.rs")
y = tx.read_text()
old = """        if skip > 0 {
            DROPS.lock().unwrap().insert(key, skip);
        } else {
            DROPS.lock().unwrap().remove(&key);
        }"""
assert y.count(old) == 1
y = y.replace(old, """        let sh = if shrink_mode() { skip } else { 0 };
        if skip > 0 {
            DROPS.lock().unwrap().insert(key, skip | if sh > 0 { 0x100 } else { 0 });
        } else {
            DROPS.lock().unwrap().remove(&key);
        }
        let levels = levels - sh;""")
old = """            d.setWidth(key.width as usize);
            d.setHeight(height as usize);"""
assert y.count(old) == 1
y = y.replace(old, """            d.setWidth((key.width >> sh).max(1) as usize);
            d.setHeight((height >> sh).max(1) as usize);""")
old = "fn dropped(key: &TexKey, _tex: &Texture) -> u32 {\n"
assert y.count(old) == 1
y = y.replace(old, """fn shrink_mode() -> bool {
    static S: std::sync::LazyLock<bool> = std::sync::LazyLock::new(|| std::env::var_os("NX_TEX_DROP_SHRINK").is_some());
    *S
}

""" + old)
old = "    let skip = dropped(key, tex);\n"
assert y.count(old) == 1
y = y.replace(old, "    let raw = dropped(key, tex);\n    let (skip, shrunk) = (raw & 0xFF, raw & 0x100 != 0);\n")
old = """            let levels = levels.min(layout.level_offsets.len() as u32);
            let skip = if skip < levels { skip } else { 0 };"""
assert y.count(old) == 1
y = y.replace(old, """            let levels = (levels + if shrunk { skip } else { 0 }).min(layout.level_offsets.len() as u32);
            let skip = if skip < levels { skip } else { 0 };""")
old = "                    decode_async(tex, k, data, pieces, skip | srgb_bit(key.format), (key.width, key.height));\n"
assert y.count(old) == 1
y = y.replace(old, """                    if shrunk {
                        for p in &mut pieces {
                            p.sub.0 -= skip;
                        }
                    }
                    decode_async(tex, k, data, pieces, (if shrunk { 0 } else { skip }) | srgb_bit(key.format), (key.width, key.height));
""")
old = """        None => {
            let stride = u32_at(b, 0x5c);"""
assert y.count(old) == 1
y = y.replace(old, """        None => {
            if shrunk {
                return;
            }
            let stride = u32_at(b, 0x5c);""")
tx.write_text(y)

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
print("patched", tx, md, "(NX_TEX_DROP_SHRINK)")


# ---------------------------------------------------------------------------
# NX_AUTOSKIP=1: automatic frameskip. 60 fps while the phone keeps up; as soon
# as frames come late it shows 1 frame out of 2 (steady 30 fps) and goes back
# to 60 when drawing is cheap again. Use it INSTEAD of NX_FRAMESKIP.
gm = pathlib.Path("crates/nxhost/src/gfx/mod.rs")
g = gm.read_text()
old = "    let interval = PRESENT_INTERVAL.load(std::sync::atomic::Ordering::Relaxed).max(*SKIP + if *SKIP > 0 { 1 } else { 0 });\n"
assert g.count(old) == 1
g = g.replace(old, """    let auto = AUTO_INTERVAL.load(std::sync::atomic::Ordering::Relaxed);
    let interval = PRESENT_INTERVAL.load(std::sync::atomic::Ordering::Relaxed).max(if auto > 0 { auto } else { *SKIP + if *SKIP > 0 { 1 } else { 0 } });
""")
g = g.replace("pub fn set_present_interval(n: u32) {", """/// Set by the automatic frameskip (NX_AUTOSKIP): 1 = 60 fps, 2 = 30 fps, 0 = off.
pub static AUTO_INTERVAL: std::sync::atomic::AtomicU32 = std::sync::atomic::AtomicU32::new(0);

pub fn set_present_interval(n: u32) {""", 1)
gm.write_text(g)

md = pathlib.Path("crates/nxhost/src/nvn/draw/mod.rs")
z = md.read_text()
old = 'static N: LazyLock<u64> = LazyLock::new(|| std::env::var("NX_FRAMESKIP").ok().and_then(|v| v.trim().parse::<u64>().ok()).unwrap_or(0).min(4));'
assert z.count(old) == 2
z = z.replace(old, "static N: LazyLock<u64> = LazyLock::new(skip_n);")
old = 'static FRAMESKIP_ON: LazyLock<bool> = LazyLock::new(|| std::env::var("NX_FRAMESKIP").ok().and_then(|v| v.trim().parse::<u64>().ok()).unwrap_or(0) > 0);'
assert z.count(old) == 1
z = z.replace(old, r"""static FRAMESKIP_ON: LazyLock<bool> = LazyLock::new(|| skip_n() > 0);

static AUTO: LazyLock<bool> = LazyLock::new(|| std::env::var_os("NX_AUTOSKIP").is_some_and(|v| v != "0"));
/// Automatic frameskip: is the frame being recorded now a skipped one?
static AUTO_SKIP: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);

fn skip_n() -> u64 {
    if *AUTO {
        return 1;
    }
    std::env::var("NX_FRAMESKIP").ok().and_then(|v| v.trim().parse::<u64>().ok()).unwrap_or(0).min(4)
}

static RESUMED: Mutex<Option<std::time::Instant>> = Mutex::new(None);

struct AutoState {
    mode30: bool,
    last: Option<std::time::Instant>,
    up_at: Option<std::time::Instant>,
    slow: u32,
    good: u32,
    need: u32,
}

/// Called at the start of every present(): decides whether the NEXT frame is skipped.
/// 60 fps mode: frames spaced more than 21 ms apart are late; a few late frames
/// close together switch to 30 fps. 30 fps mode: the drawn frame's own work time
/// (from the end of the skipped frame to this present) is measured; after
/// `need` cheap frames in a row (under 12 ms) it tries 60 fps again. Each quick
/// fall back doubles `need` (up to ~60 s), so it does not flicker.
fn auto_decide(skipped: bool) {
    static ST: Mutex<AutoState> = Mutex::new(AutoState { mode30: false, last: None, up_at: None, slow: 0, good: 0, need: 120 });
    let now = std::time::Instant::now();
    let mut st = ST.lock().unwrap();
    if !st.mode30 {
        if let Some(l) = st.last {
            let dt = now - l;
            if dt > std::time::Duration::from_millis(21) && dt < std::time::Duration::from_millis(500) {
                st.slow += 4;
            } else {
                st.slow = st.slow.saturating_sub(1);
            }
        }
        if st.slow >= 12 {
            let quick = st.up_at.is_some_and(|u| now - u < std::time::Duration::from_secs(5));
            st.need = if quick { (st.need * 2).min(1800) } else { 120 };
            (st.mode30, st.slow, st.good) = (true, 0, 0);
            gfx::AUTO_INTERVAL.store(2, Relaxed);
            eprintln!("[autoskip] frames late: 30 fps");
        }
    } else if !skipped {
        if let Some(r) = *RESUMED.lock().unwrap() {
            if now - r < std::time::Duration::from_millis(12) {
                st.good += 1;
            } else {
                st.good = 0;
            }
        }
        if st.good >= st.need {
            (st.mode30, st.good, st.slow, st.up_at) = (false, 0, 0, Some(now));
            gfx::AUTO_INTERVAL.store(1, Relaxed);
            eprintln!("[autoskip] drawing is cheap again: 60 fps");
        }
    }
    st.last = Some(now);
    AUTO_SKIP.store(st.mode30 && !skipped, Relaxed);
}

""")
old = "fn skip_frame() -> bool {\n"
assert z.count(old) == 1
z = z.replace(old, old + "    if *AUTO {\n        return AUTO_SKIP.load(Relaxed);\n    }\n")
old = """    let skip = skip_frame();
    FRAME.fetch_add(1, Relaxed);
    if *FRAMESKIP_ON {
        pace_frame(skip);
    }
"""
assert z.count(old) == 1
z = z.replace(old, """    let skip = skip_frame();
    FRAME.fetch_add(1, Relaxed);
    if *AUTO {
        if gfx::AUTO_INTERVAL.load(Relaxed) == 0 {
            gfx::AUTO_INTERVAL.store(1, Relaxed);
        }
        auto_decide(skip);
    }
    if *FRAMESKIP_ON {
        pace_frame(skip);
    }
    if *AUTO && skip {
        *RESUMED.lock().unwrap() = Some(std::time::Instant::now());
    }
""")
md.write_text(z)
print("patched", gm, md, "(NX_AUTOSKIP)")

# ---------------------------------------------------------------------------
# Memory: start dropping unused textures earlier (600 MB left instead of 400:
# the last race log reached 445 MB left), and at the same time free the
# pipelines compiled at boot that the game has not asked for yet (they come
# back from the pipeline archive when needed).
tx = pathlib.Path("crates/nxhost/src/nvn/draw/textures.rs")
y = tx.read_text()
old = '.unwrap_or(400) << 20);'
assert y.count(old) == 1
y = y.replace(old, '.unwrap_or(400) << 20);')
old = "        let frame = self.frame;\n        let old: std::collections::HashSet<TexKey> = self\n"
assert y.count(old) == 1

tx.write_text(y)
st = pathlib.Path("crates/nxhost/src/nvn/draw/state.rs")
u = st.read_text()
old = "impl States {\n"
assert u.count(old) == 1
u = u.replace(old, """/// Low memory: forget the boot-compiled pipelines nobody has used yet.
pub fn drop_prewarmed() {
    let n = {
        let mut m = PREWARMED.lock().unwrap();
        let n = m.len();
        m.clear();
        n
    };
    if n > 0 {
        eprintln!("[mem] low memory: freed {n} unused prewarmed pipelines");
    }
}

""" + old)
st.write_text(u)
print("patched", tx, st, "(earlier memory trim, free unused prewarmed pipelines)")


# ---------------------------------------------------------------------------
# Low-memory trim: a texture uploaded but not looked up again yet had no
# "last used" frame and was dropped at once (it came back decoded again).
# Now it counts as used when the trim first sees it.
tx = pathlib.Path("crates/nxhost/src/nvn/draw/textures.rs")
y = tx.read_text()
old = "        let frame = self.frame;\n        let old: std::collections::HashSet<TexKey> = self\n"
assert y.count(old) == 1
y = y.replace(old, """        let frame = self.frame;
        for k in self.uploaded.keys() {
            self.last_used.entry(*k).or_insert(frame);
        }
        let old: std::collections::HashSet<TexKey> = self
""")
y = y.replace(".filter(|k| self.last_used.get(*k).map_or(true, |&f| f + 300 < frame))", ".filter(|k| self.last_used.get(*k).is_some_and(|&f| f + 300 < frame))")
tx.write_text(y)
print("patched", tx, "(trim keeps fresh textures)")

# ---------------------------------------------------------------------------
# Device pipeline archive, v2. The single growing archive often failed to
# save ("expecting 'fragment' stage in pipeline no. N") and pipelines compiled
# in one run were never found in the next one (0 archive hits). Now:
# - the descriptor is copied before it is handed to the archive thread;
# - each batch goes to its own small file in work/devarch/ (a bad pipeline
#   can no longer poison the whole archive: a failing batch is split in two
#   until the bad pipeline is isolated and left out);
# - after the first save, the file is read back and one pipeline is looked up
#   in it: "[gfx] archive self-test: hit/MISS" in the log tells if it works.
st = pathlib.Path("crates/nxhost/src/nvn/draw/state.rs")
u = st.read_text()
a = u.index("fn harvest(d: Retained<MTLRenderPipelineDescriptor>) {")
b = u.index("#[derive(Clone, PartialEq, Eq, Hash)]\nstruct Recorded")
u = u[:a] + r"""const DEVICE_ARCHIVE_DIR: &str = "work/devarch";

fn device_archive_files() -> Vec<String> {
    let mut v: Vec<String> = std::fs::read_dir(DEVICE_ARCHIVE_DIR)
        .map(|r| r.flatten().map(|e| e.file_name().to_string_lossy().into_owned()).filter(|n| n.ends_with(".metalar")).collect())
        .unwrap_or_default();
    v.sort();
    v.into_iter().map(|n| format!("{DEVICE_ARCHIVE_DIR}/{n}")).collect()
}

fn save_group(group: &[Shared<MTLRenderPipelineDescriptor>], path: &str) -> Result<(), String> {
    let a = Shared(open_archive(None).ok_or("could not create an archive")?);
    for d in group {
        if !add(&a, &d.0) {
            return Err("could not add the pipeline".into());
        }
    }
    let tmp = format!("{path}.tmp");
    a.0.serializeToURL_error(&NSURL::fileURLWithPath(&NSString::from_str(&tmp))).map_err(|e| e.localizedDescription().to_string())?;
    std::fs::rename(&tmp, path).map_err(|e| e.to_string())
}

fn archive_self_test(path: &str, d: &MTLRenderPipelineDescriptor) {
    let Some(a) = open_archive(Some(path)) else {
        eprintln!("[gfx] archive self-test: could not reopen {path}");
        return;
    };
    let copy: Retained<MTLRenderPipelineDescriptor> = unsafe { objc2::msg_send![d, copy] };
    copy.setBinaryArchives(Some(&NSArray::from_retained_slice(&[a])));
    let r = gfx::gpu().device.0.newRenderPipelineStateWithDescriptor_options_reflection_error(&copy, MTLPipelineOption::FailOnBinaryArchiveMiss, None);
    match r {
        Ok(_) => eprintln!("[gfx] archive self-test: hit (saved pipelines are found again)"),
        Err(e) => eprintln!("[gfx] archive self-test: MISS ({})", e.localizedDescription()),
    }
}

fn harvest(d: Retained<MTLRenderPipelineDescriptor>) {
    static TX: LazyLock<Option<Mutex<std::sync::mpsc::Sender<Shared<MTLRenderPipelineDescriptor>>>>> = LazyLock::new(|| {
        if !device_archive_on() {
            return None;
        }
        let _ = std::fs::create_dir_all(DEVICE_ARCHIVE_DIR);
        let files = device_archive_files();
        let bytes: u64 = files.iter().filter_map(|f| std::fs::metadata(f).ok()).map(|m| m.len()).sum();
        if files.len() >= 400 || bytes > 300 << 20 {
            eprintln!("[gfx] device pipeline archive is full ({} files, {} MB), not adding to it", files.len(), bytes >> 20);
            return None;
        }
        let (tx, rx) = std::sync::mpsc::channel::<Shared<MTLRenderPipelineDescriptor>>();
        std::thread::Builder::new()
            .name("pipeline-archive".into())
            .spawn(move || {
                unsafe { libc::pthread_set_qos_class_self_np(libc::qos_class_t::QOS_CLASS_UTILITY, 0) };
                let run = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map_or(0, |d| d.as_secs());
                let (mut files, mut saved, mut rejected, mut tested) = (files.len(), 0usize, 0usize, false);
                let mut batch: Vec<Shared<MTLRenderPipelineDescriptor>> = Vec::new();
                let mut seq = 0u32;
                loop {
                    let timed_out = match rx.recv_timeout(std::time::Duration::from_secs(3)) {
                        Ok(d) => {
                            batch.push(d);
                            false
                        }
                        Err(std::sync::mpsc::RecvTimeoutError::Timeout) => true,
                        Err(_) => return,
                    };
                    if batch.is_empty() || !(timed_out || batch.len() >= 32) {
                        continue;
                    }
                    objc2::rc::autoreleasepool(|_| {
                        let mut stack = vec![std::mem::take(&mut batch)];
                        while let Some(mut group) = stack.pop() {
                            seq += 1;
                            let path = format!("{DEVICE_ARCHIVE_DIR}/{run:x}-{seq:04}.metalar");
                            match save_group(&group, &path) {
                                Ok(()) => {
                                    files += 1;
                                    saved += group.len();
                                    if !tested {
                                        tested = true;
                                        archive_self_test(&path, &group[0].0);
                                    }
                                }
                                Err(e) if group.len() == 1 => {
                                    rejected += 1;
                                    if rejected <= 3 {
                                        let e: String = e.chars().take(300).collect();
                                        eprintln!("[gfx] pipeline left out of the device archive: {e}");
                                    }
                                }
                                Err(_) => {
                                    let half = group.split_off(group.len() / 2);
                                    stack.push(group);
                                    stack.push(half);
                                }
                            }
                        }
                    });
                    eprintln!("[gfx] device pipeline archive: {saved} pipelines saved this run ({files} files, {rejected} left out)");
                    if files >= 400 {
                        return;
                    }
                }
            })
            .ok()?;
        Some(Mutex::new(tx))
    });
    if let Some(tx) = &*TX {
        // a private copy: the caller's descriptor still points at the loaded archives
        let copy: Retained<MTLRenderPipelineDescriptor> = unsafe { objc2::msg_send![&*d, copy] };
        copy.setBinaryArchives(None);
        let _ = tx.lock().unwrap().send(Shared(copy));
    }
}

""" + u[b:]
old = """    if device_archive_on() && std::path::Path::new(DEVICE_ARCHIVE).exists() {
        paths.push(DEVICE_ARCHIVE.to_string());
    }"""
assert u.count(old) == 1
u = u.replace(old, old + """
    if device_archive_on() {
        paths.extend(device_archive_files());
    }""")
old = """    eprintln!("[gfx] {} pipeline archive(s) loaded ({})", archives.len(), paths.join(", "));"""
assert u.count(old) == 1
u = u.replace(old, """    eprintln!("[gfx] {} pipeline archive(s) loaded", archives.len());""")
st.write_text(u)
print("patched", st, "(device pipeline archive v2)")

# ---------------------------------------------------------------------------
# Launcher: "Show FPS" switch (config show_fps; on by default) and
# "Music from other apps" (mix_audio): game sound mixes with Spotify & co.
mn = pathlib.Path("crates/nxhost/src/main.rs")
m = mn.read_text()
old = '    if field("rumble").as_deref() == Some("false") {\n'
assert m.count(old) == 1
m = m.replace(old, """    if field("show_fps").as_deref() == Some("false") {
        unsafe { std::env::set_var("NX_HIDE_FPS", "1") };
    } else if std::env::var_os("NX_FPS").is_none() {
        unsafe { std::env::set_var("NX_FPS", "1") };
    }
""" + old)
mn.write_text(m)
io = pathlib.Path("crates/nxhost/src/gfx/ios.rs")
i = io.read_text()
old = "pub fn show_fps(text: String) {\n"
assert i.count(old) == 1
i = i.replace(old, old + "    if std::env::var_os(\"NX_HIDE_FPS\").is_some() {\n        return;\n    }\n")
io.write_text(i)
sw = pathlib.Path("ios/Launcher.swift")
w = sw.read_text()
reps = [
("import Combine\n", "import AVFoundation\nimport Combine\n"),
("    @Published var phonePads = false\n", "    @Published var phonePads = false\n    @Published var showFps = true\n    @Published var mixAudio = true\n"),
('        phonePads = json["phone_pads"] as? Bool ?? false\n', '        phonePads = json["phone_pads"] as? Bool ?? false\n        showFps = json["show_fps"] as? Bool ?? true\n        mixAudio = json["mix_audio"] as? Bool ?? true\n'),
('        json["phone_pads"] = phonePads\n', '        json["phone_pads"] = phonePads\n        json["show_fps"] = showFps\n        json["mix_audio"] = mixAudio\n'),
("""                Section {
                    Toggle("Prewarm pipelines", isOn: $s.prewarm)""", """                Section {
                    Toggle("Show FPS", isOn: $s.showFps)
                    Toggle("Music from other apps", isOn: $s.mixAudio)
                } footer: {
                    Text("Keep Spotify or other music playing while you play (game sound is mixed in)")
                }
                Section {
                    Toggle("Prewarm pipelines", isOn: $s.prewarm)"""),
("""        .onAppear {
            files.scan()""", """        .onAppear {
            applyAudioMix(s.mixAudio)
            files.scan()"""),
("""        .onChange(of: s.phonePads) {""", """        .onChange(of: s.mixAudio) { applyAudioMix(s.mixAudio) }
        .onChange(of: s.phonePads) {"""),
('let docsURL = ', """func applyAudioMix(_ mix: Bool) {
    let session = AVAudioSession.sharedInstance()
    try? session.setCategory(mix ? .ambient : .soloAmbient, mode: .default, options: mix ? [.mixWithOthers] : [])
    try? session.setActive(true)
}

let docsURL = """),
]
for a_, b_ in reps:
    assert w.count(a_) == 1, a_[:60]
    w = w.replace(a_, b_)
sw.write_text(w)
print("patched", mn, io, sw, "(Show FPS / Music from other apps)")

# ---------------------------------------------------------------------------
# iOS link fix: "compact unwind compressed function offset doesn't fit in 24
# bits". The embedded game code (64 MB above the app's code) was marked as
# instructions, so the linker put it in the unwind table next to the app's
# last functions; depending on how many functions the app has, both could
# land in one unwind page, too far apart. The segments stay executable
# (-segprot rx); only the section attribute goes.
br = pathlib.Path("crates/nxhost/build.rs")
r = br.read_text()
old = 'let flags = if prot == "rx" { "regular,pure_instructions" } else { "regular" };'
assert r.count(old) == 1
r = r.replace(old, 'let flags = "regular";')
br.write_text(r)
print("patched", br, "(no unwind entries for the embedded game code)")

# ---------------------------------------------------------------------------
# Device archive v2 must not compete with the game for the shader compiler:
# adding a pipeline to an archive compiles it again in MTLCompilerService.
# Before each pipeline the archive thread waits until the game is not waiting for a pipeline
# (DEMAND == 0), with a short pause between pipelines.
st = pathlib.Path("crates/nxhost/src/nvn/draw/state.rs")
u = st.read_text()
old = "fn save_group(group: &[Shared<MTLRenderPipelineDescriptor>], path: &str) -> Result<(), String> {\n    let a = Shared(open_archive(None).ok_or(\"could not create an archive\")?);\n    for d in group {\n"
assert u.count(old) == 1
u = u.replace(old, old + """        archive_wait_idle();
""")
old = "fn save_group("
u = u.replace(old, """static PREWARM_DONE: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);

/// Let the game and the boot prewarm use the shader compiler first.
fn archive_wait_idle() {
    use std::sync::atomic::Ordering::SeqCst;
    while DEMAND.load(SeqCst) > 0 {
        std::thread::sleep(std::time::Duration::from_millis(50));
    }
    std::thread::sleep(std::time::Duration::from_millis(20));
}

fn save_group(""", 1)
old = '            eprintln!("[gfx] prewarmed {}/{} recorded pipelines in {:.1} s ({} from the device archive)", built.into_inner(), list.len(), t0.elapsed().as_secs_f32(), PREWARM_HITS.load(Relaxed));\n'
assert u.count(old) == 1
u = u.replace(old, old + "            PREWARM_DONE.store(true, std::sync::atomic::Ordering::SeqCst);\n")
old = "                    let timed_out = match rx.recv_timeout(std::time::Duration::from_secs(3)) {"
assert u.count(old) == 1
u = u.replace(old, "                    let timed_out = match rx.recv_timeout(std::time::Duration::from_secs(5)) {")
st.write_text(u)
print("patched", st, "(device archive waits for the game and the prewarm)")

# Prewarm progress in the log every 200 pipelines (it can take minutes with a
# cold iOS shader cache, while it pauses for the game).
st = pathlib.Path("crates/nxhost/src/nvn/draw/state.rs")
u = st.read_text()
old = "                                    PREWARMED.lock().unwrap().insert(r.clone(), Shared(p));\n                                    built.fetch_add(1, Relaxed);\n"
assert u.count(old) == 1
u = u.replace(old, """                                    PREWARMED.lock().unwrap().insert(r.clone(), Shared(p));
                                    let n = built.fetch_add(1, Relaxed) + 1;
                                    if n % 200 == 0 {
                                        eprintln!("[gfx] prewarm: {n}/{} pipelines ({:.0} s)", list.len(), t0.elapsed().as_secs_f32());
                                    }
""")
st.write_text(u)
print("patched", st, "(prewarm progress)")

# Which build is running: first line of the log.
mn = pathlib.Path("crates/nxhost/src/main.rs")
m = mn.read_text()
old = '    eprintln!("[host] iOS: files in {}, bundle {}", docs.display(), bundle.display());\n'
assert m.count(old) == 1
m = m.replace(old, '    eprintln!("[host] build {}", option_env!("GITHUB_SHA").map_or("local", |s| &s[..s.len().min(7)]));\n' + old)
mn.write_text(m)
print("patched", mn, "(build id in the log)")

# ---------------------------------------------------------------------------
# NX_TEX_SHRINK_MIN=W: with NX_TEX_DROP_SHRINK, only textures at least W
# pixels wide (game size) are really shrunk; smaller reduced textures keep
# their full size (sharpest levels filled by enlargement). The item textures
# turn black when shrunk; the biggest textures (most of the memory) can still be.
tx = pathlib.Path("crates/nxhost/src/nvn/draw/textures.rs")
y = tx.read_text()
old = "        let sh = if shrink_mode() { skip } else { 0 };\n"
assert y.count(old) == 1
y = y.replace(old, """        static SHRINK_MIN: std::sync::LazyLock<u32> = std::sync::LazyLock::new(|| std::env::var("NX_TEX_SHRINK_MIN").ok().and_then(|v| v.trim().parse::<u32>().ok()).unwrap_or(0));
        let sh = if shrink_mode() && key.width.max(height) >= *SHRINK_MIN { skip } else { 0 };
""")
tx.write_text(y)
print("patched", tx, "(NX_TEX_SHRINK_MIN)")

# ---------------------------------------------------------------------------
# "Final" setup: the optimised settings are built in (no env.txt needed; an
# env.txt still overrides any of them), chosen in the launcher:
#   30 fps lock (frameskip)   -> NX_FRAMESKIP            (config "frameskip", on)
#   Textures: very low / low / full -> NX_TEX_DROP...     (config "texture_quality")
#   Unlocked save             -> work/save-unlocked, seeded from the save
#                                packed in the IPA        (config "unlocked_save")
# Always on unless env.txt says otherwise: NX_FPS=1 (log), NX_SYNC_TEXTURES,
# NX_NO_TEX_CACHE, NX_HASH_EVERY=2.
mn = pathlib.Path("crates/nxhost/src/main.rs")
m = mn.read_text()
old = """    if field("show_fps").as_deref() == Some("false") {
        unsafe { std::env::set_var("NX_HIDE_FPS", "1") };
    } else if std::env::var_os("NX_FPS").is_none() {
        unsafe { std::env::set_var("NX_FPS", "1") };
    }
"""
assert m.count(old) == 1
m = m.replace(old, """    if field("show_fps").as_deref() == Some("false") {
        unsafe { std::env::set_var("NX_HIDE_FPS", "1") };
    }
    let def = |k: &str, v: &str| {
        if std::env::var_os(k).is_none() {
            unsafe { std::env::set_var(k, v) };
        }
    };
    def("NX_FPS", "1");
    def("NX_SYNC_TEXTURES", "1");
    def("NX_NO_TEX_CACHE", "1");
    def("NX_HASH_EVERY", "2");
    def("NX_FRAMESKIP", if field("frameskip").as_deref() == Some("false") { "0" } else { "1" });
    if std::env::var_os("NX_TEX_DROP").is_none() {
        let q = field("texture_quality").unwrap_or_else(|| "verylow".into());
        if q != "normal" {
            for (k, v) in [("NX_TEX_DROP", "2"), ("NX_TEX_DROP_SHRINK", "1"), ("NX_TEX_DROP_MIN", "256")] {
                unsafe { std::env::set_var(k, v) };
            }
            if q == "low" {
                def("NX_TEX_SHRINK_MIN", "2048");
            }
        }
    }
    if field("unlocked_save").as_deref() == Some("true") {
        let bundle = std::env::current_exe().ok().and_then(|e| e.parent().map(PathBuf::from)).unwrap_or_default();
        let src = bundle.join("shaders/unlocked-save");
        let dst = PathBuf::from("work/save-unlocked");
        if !dst.exists() {
            if src.is_dir() {
                match seed_unlocked_save(&src, &dst) {
                    Ok(()) => eprintln!("[host] unlocked save copied to work/save-unlocked"),
                    Err(e) => eprintln!("[host] could not copy the unlocked save: {e}"),
                }
            } else {
                eprintln!("[host] this IPA has no unlocked save: using your own save");
            }
        }
        if dst.exists() {
            unsafe { std::env::set_var("NX_SAVE_DIR", "work/save-unlocked") };
            eprintln!("[host] using the unlocked save (your own save in work/save is untouched)");
        }
    }
""")
m += """

/// work/save-unlocked = a copy of the player's own save folders, with the
/// packed save files (e.g. userdata.dat, a dump of the game's save) put in the
/// folder that holds the same file name (type1-index0 if none does).
fn seed_unlocked_save(src: &std::path::Path, dst: &std::path::Path) -> std::io::Result<()> {
    let own = std::path::Path::new("work/save");
    if own.is_dir() {
        copy_dir(own, dst)?;
    }
    std::fs::create_dir_all(dst)?;
    let subdirs: Vec<PathBuf> = std::fs::read_dir(dst)?.flatten().map(|e| e.path()).filter(|p| p.is_dir()).collect();
    for e in std::fs::read_dir(src)? {
        let e = e?;
        let name = e.file_name();
        let target = if e.file_type()?.is_dir() && name.to_string_lossy().starts_with("type") {
            dst.to_path_buf()
        } else {
            subdirs.iter().find(|d| d.join(&name).exists()).cloned().unwrap_or_else(|| dst.join("type1-index0"))
        };
        std::fs::create_dir_all(&target)?;
        if e.file_type()?.is_dir() {
            copy_dir(&e.path(), &target.join(&name))?;
        } else {
            std::fs::copy(e.path(), target.join(&name))?;
        }
        eprintln!("[host] unlocked save: {} -> {}", name.to_string_lossy(), target.display());
    }
    Ok(())
}

fn copy_dir(src: &std::path::Path, dst: &std::path::Path) -> std::io::Result<()> {
    std::fs::create_dir_all(dst)?;
    for e in std::fs::read_dir(src)? {
        let e = e?;
        let to = dst.join(e.file_name());
        if e.file_type()?.is_dir() {
            copy_dir(&e.path(), &to)?;
        } else {
            std::fs::copy(e.path(), to)?;
        }
    }
    Ok(())
}
"""
old = "        save_dir: args.save.clone(),\n"
assert m.count(old) == 1
m = m.replace(old, "        save_dir: std::env::var_os(\"NX_SAVE_DIR\").map(PathBuf::from).unwrap_or_else(|| args.save.clone()),\n")
mn.write_text(m)

mk = pathlib.Path("make-ios.sh")
k = mk.read_text()
old = 'cp "$LIST" "$RES/shaders/pipelines.tsv"\n'
assert k.count(old) == 1
k = k.replace(old, old + 'if [ -d unlocked-save ]; then cp -R unlocked-save "$RES/shaders/unlocked-save"; echo "unlocked save packed in the IPA"; fi\n')
mk.write_text(k)

sw = pathlib.Path("ios/Launcher.swift")
w = sw.read_text()
reps = [
("    @Published var mixAudio = true\n", "    @Published var mixAudio = true\n    @Published var frameskip = true\n    @Published var textureQuality = \"verylow\"\n    @Published var unlockedSave = false\n"),
('        mixAudio = json["mix_audio"] as? Bool ?? true\n', '        mixAudio = json["mix_audio"] as? Bool ?? true\n        frameskip = json["frameskip"] as? Bool ?? true\n        textureQuality = json["texture_quality"] as? String ?? "verylow"\n        unlockedSave = json["unlocked_save"] as? Bool ?? false\n'),
('        json["mix_audio"] = mixAudio\n', '        json["mix_audio"] = mixAudio\n        json["frameskip"] = frameskip\n        json["texture_quality"] = textureQuality\n        json["unlocked_save"] = unlockedSave\n'),
("""                Section {
                    Toggle("Show FPS", isOn: $s.showFps)""", """                Section {
                    Toggle("30 fps lock (frameskip)", isOn: $s.frameskip)
                    Picker("Textures", selection: $s.textureQuality) {
                        Text("Very low (fastest)").tag("verylow")
                        Text("Low (items not black)").tag("low")
                        Text("Full (may run out of memory)").tag("normal")
                    }
                } header: {
                    Text("Performance")
                } footer: {
                    Text("Very low: least memory, some items look black. Low: items fixed, uses more memory. An env.txt in Documents overrides these.")
                }
                Section {
                    Toggle("Unlocked save", isOn: $s.unlockedSave)
                } footer: {
                    Text("Plays with the save packed in this IPA (everything unlocked). Your own save is kept apart and untouched.")
                }
                Section {
                    Toggle("Show FPS", isOn: $s.showFps)"""),
]
for a_, b_ in reps:
    assert w.count(a_) == 1, a_[:60]
    w = w.replace(a_, b_)
sw.write_text(w)
print("patched", mn, mk, sw, "(built-in settings, launcher performance options, unlocked save)")

# ---------------------------------------------------------------------------
# On-screen counters: two small rounded pills at the very top (above the
# -/+ buttons), "30 fps" and the memory iOS counts for the app ("2.4 GB").
# Launcher switches: Show FPS (NX_HIDE_FPS), Show memory (NX_HIDE_MEM).
gm = pathlib.Path("crates/nxhost/src/gfx/mod.rs")
g = gm.read_text()
old = "fn footprint() -> u64 {"
assert g.count(old) == 1
g = g.replace(old, "pub fn footprint() -> u64 {")
gm.write_text(g)
io = pathlib.Path("crates/nxhost/src/gfx/ios.rs")
i = io.read_text()
a = i.index("thread_local! {\n    static FPS_LABEL")
b = i.index("pub fn show_fps(text: String) {", a)
b = i.index("\n    });\n}\n", b) + len("\n    });\n}\n")
i = i[:a] + r'''thread_local! {
    static PILLS: RefCell<Option<(Retained<UILabel>, Retained<UILabel>)>> = const { RefCell::new(None) };
}

fn pill(mtm: MainThreadMarker, view: &UIView, x: f64) -> Retained<UILabel> {
    let label = UILabel::new(mtm);
    label.setFrame(CGRect::new(CGPoint::new(x, 2.0), CGSize::new(62.0, 16.0)));
    unsafe { label.setTextColor(Some(&UIColor::colorWithWhite_alpha(1.0, 0.92))) };
    label.setBackgroundColor(Some(&UIColor::colorWithWhite_alpha(0.0, 0.42)));
    unsafe { label.setFont(Some(&UIFont::boldSystemFontOfSize(11.0))) };
    label.setTextAlignment(NSTextAlignment::Center);
    label.setUserInteractionEnabled(false);
    label.setClipsToBounds(true);
    label.layer().setCornerRadius(8.0);
    label.setHidden(true);
    view.addSubview(&label);
    label
}

/// Updates the on-screen counters (called once per second when NX_FPS=1).
pub fn show_fps(text: String) {
    let hide_fps = std::env::var_os("NX_HIDE_FPS").is_some();
    let hide_mem = std::env::var_os("NX_HIDE_MEM").is_some();
    if hide_fps && hide_mem {
        return;
    }
    let mem = format!("{:.1} GB", super::footprint() as f64 / (1u64 << 30) as f64);
    dispatch2::DispatchQueue::main().exec_async(move || {
        let Some(mtm) = MainThreadMarker::new() else { return };
        PILLS.with(|cell| {
            let mut cell = cell.borrow_mut();
            if cell.is_none() {
                let Some(view) = root_view() else { return };
                let w = UICoordinateSpace::bounds(&*view).size.width;
                let both = !hide_fps && !hide_mem;
                let (xf, xm) = if both { (w / 2.0 - 66.0, w / 2.0 + 4.0) } else { (w / 2.0 - 31.0, w / 2.0 - 31.0) };
                *cell = Some((pill(mtm, &view, xf), pill(mtm, &view, xm)));
            }
            if let Some((f, m)) = cell.as_ref() {
                if !hide_fps {
                    f.setText(Some(&NSString::from_str(&text)));
                    f.setHidden(false);
                }
                if !hide_mem {
                    m.setText(Some(&NSString::from_str(&mem)));
                    m.setHidden(false);
                }
            }
        })
    });
}
''' + i[b:]
io.write_text(i)
print("patched", io, gm, "(fps + memory pills)")

# Background texture loading (launcher, off by default): while a texture is
# decoded it shows neutral grey instead of leftover memory (the purple), and
# at most ~32 MB of game data waits in the queue (beyond that, decode at once:
# the queue made iOS kill the game while a track loaded).
tx = pathlib.Path("crates/nxhost/src/nvn/draw/textures.rs")
y = tx.read_text()
old = """    let id = tex as *const Texture as *const u8 as usize;
    let generation = {"""
assert y.count(old) == 1
y = y.replace(old, """    if QUEUED.load(std::sync::atomic::Ordering::Relaxed) > 32 << 20 {
        decode_and_stage(tex, k, data, pieces, skip, full);
        return;
    }
    neutral_fill(tex);
    let bytes = data.len();
    QUEUED.fetch_add(bytes, std::sync::atomic::Ordering::Relaxed);
    let id = tex as *const Texture as *const u8 as usize;
    let generation = {""")
old = """        let t = t; // capture the whole Shared (Send), not just its field
        if GENS.lock().unwrap().get(&id) != Some(&generation) {
            return;
        }
        decode_and_stage(&t.0, k, data, pieces, skip, full);"""
assert y.count(old) == 1
y = y.replace(old, """        let t = t; // capture the whole Shared (Send), not just its field
        if GENS.lock().unwrap().get(&id) == Some(&generation) {
            decode_and_stage(&t.0, k, data, pieces, skip, full);
        }
        QUEUED.fetch_sub(bytes, std::sync::atomic::Ordering::Relaxed);""")
old = "fn decode_async("
y = y.replace(old, """static QUEUED: std::sync::atomic::AtomicUsize = std::sync::atomic::AtomicUsize::new(0);

/// Fill a new 8-bit colour texture with opaque grey on the GPU, so it does not
/// show leftover memory until its decoded contents arrive.
fn neutral_fill(tex: &Texture) {
    if !matches!(tex.pixelFormat(), MTLPixelFormat::RGBA8Unorm | MTLPixelFormat::RGBA8Unorm_sRGB) {
        return;
    }
    if !matches!(tex.textureType(), MTLTextureType::Type2D | MTLTextureType::Type2DArray) {
        return;
    }
    const GREY_BYTES: usize = 16 << 20;
    static GREY: std::sync::LazyLock<Option<Shared<gfx::Buffer>>> = std::sync::LazyLock::new(|| {
        let b = gfx::gpu().device.0.newBufferWithLength_options(GREY_BYTES, MTLResourceOptions::StorageModeShared)?;
        let px = unsafe { std::slice::from_raw_parts_mut(b.contents().as_ptr() as *mut u32, GREY_BYTES / 4) };
        px.fill(u32::from_le_bytes([0x80, 0x80, 0x80, 0xff]));
        Some(Shared(b))
    });
    let Some(grey) = &*GREY else { return };
    let Some(cb) = gfx::gpu().queue.0.commandBuffer() else { return };
    let Some(enc) = cb.blitCommandEncoder() else { return };
    for level in 0..tex.mipmapLevelCount() {
        let (w, h) = ((tex.width() >> level).max(1), (tex.height() >> level).max(1));
        if w * 4 * h > GREY_BYTES {
            continue;
        }
        for slice in 0..tex.arrayLength() {
            unsafe {
                enc.copyFromBuffer_sourceOffset_sourceBytesPerRow_sourceBytesPerImage_sourceSize_toTexture_destinationSlice_destinationLevel_destinationOrigin(
                    &grey.0,
                    0,
                    w * 4,
                    w * 4 * h,
                    MTLSize { width: w, height: h, depth: 1 },
                    tex,
                    slice,
                    level,
                    MTLOrigin { x: 0, y: 0, z: 0 },
                )
            };
        }
    }
    enc.endEncoding();
    cb.commit();
}

fn decode_async(""", 1)
tx.write_text(y)
print("patched", tx, "(background textures: grey placeholder, bounded queue)")

mn = pathlib.Path("crates/nxhost/src/main.rs")
m = mn.read_text()
old = '    def("NX_SYNC_TEXTURES", "1");\n'
assert m.count(old) == 1
m = m.replace(old, '''    if field("async_textures").as_deref() != Some("true") {
        def("NX_SYNC_TEXTURES", "1");
    }
    if field("show_memory").as_deref() == Some("false") {
        unsafe { std::env::set_var("NX_HIDE_MEM", "1") };
    }
''')
mn.write_text(m)

# Launcher: switches + Tools (share the log, clean up, clear shader cache)
sw = pathlib.Path("ios/Launcher.swift")
w = sw.read_text()
reps = [
("    @Published var unlockedSave = false\n", "    @Published var unlockedSave = false\n    @Published var asyncTextures = false\n    @Published var showMemory = true\n"),
('        unlockedSave = json["unlocked_save"] as? Bool ?? false\n', '        unlockedSave = json["unlocked_save"] as? Bool ?? false\n        asyncTextures = json["async_textures"] as? Bool ?? false\n        showMemory = json["show_memory"] as? Bool ?? true\n'),
('        json["unlocked_save"] = unlockedSave\n', '        json["unlocked_save"] = unlockedSave\n        json["async_textures"] = asyncTextures\n        json["show_memory"] = showMemory\n'),
("""                        Text("Full (may run out of memory)").tag("normal")
                    }""", """                        Text("Full (may run out of memory)").tag("normal")
                    }
                    Toggle("Background texture loading", isOn: $s.asyncTextures)"""),
("""                    Text("Very low: least memory, some items look black. Low: items fixed, uses more memory. An env.txt in Documents overrides these.")""",
 """                    Text("Very low: least memory, some items look black. Low: items fixed, uses more memory. Background texture loading (experimental): fewer small hitches, new objects are grey for a moment. An env.txt in Documents overrides these.")"""),
("""                    Toggle("Show FPS", isOn: $s.showFps)""", """                    Toggle("Show FPS", isOn: $s.showFps)
                    Toggle("Show memory", isOn: $s.showMemory)"""),
("    @StateObject private var s = Settings()\n", "    @StateObject private var s = Settings()\n    @State private var toolMessage = \"\"\n    @State private var confirmClear = false\n"),
("                if files.ready { gameFilesSection }\n", """                Section {
                    ShareLink(item: docsURL.appendingPathComponent("work/nxhost.log")) {
                        Label("Share the log", systemImage: "square.and.arrow.up")
                    }
                    ShareLink(item: docsURL.appendingPathComponent("work/nxhost.prev.log")) {
                        Label("Share the previous log", systemImage: "square.and.arrow.up")
                    }
                    Button("Clean up unneeded files") { toolMessage = cleanWork() }
                    Button("Clear the shader cache", role: .destructive) { confirmClear = true }
                } header: {
                    Text("Tools")
                } footer: {
                    Text(toolMessage.isEmpty ? "Clear the shader cache after an iOS update: the next start compiles everything again." : toolMessage)
                }
                .confirmationDialog("Clear the shader cache? The next start will be slow while everything compiles again.", isPresented: $confirmClear, titleVisibility: .visible) {
                    Button("Clear", role: .destructive) { toolMessage = clearShaderCache() }
                }
                if files.ready { gameFilesSection }
"""),
("let docsURL = ", '''func fileSize(_ url: URL) -> Int64 {
    let fm = FileManager.default
    var isDir: ObjCBool = false
    guard fm.fileExists(atPath: url.path, isDirectory: &isDir) else { return 0 }
    if !isDir.boolValue { return (try? fm.attributesOfItem(atPath: url.path)[.size] as? Int64) ?? 0 }
    let items = (try? fm.subpathsOfDirectory(atPath: url.path)) ?? []
    return items.reduce(0) { $0 + ((try? fm.attributesOfItem(atPath: url.appendingPathComponent($1).path)[.size] as? Int64) ?? 0) }
}

func removeAll(_ urls: [URL]) -> String {
    var freed: Int64 = 0, count = 0
    for u in urls {
        let n = fileSize(u)
        if (try? FileManager.default.removeItem(at: u)) != nil { freed += n; count += 1 }
    }
    return "Removed \\(count) item(s), \\(ByteCountFormatter.string(fromByteCount: freed, countStyle: .file)) freed"
}

/// Leftovers: random-named .metallib files, the old texture cache, the old pipeline archive, temp files.
func cleanWork() -> String {
    let work = docsURL.appendingPathComponent("work")
    let names = (try? FileManager.default.contentsOfDirectory(atPath: work.path)) ?? []
    var junk = names.filter { $0.hasSuffix(".metallib") || $0.hasSuffix(".tmp") || $0.hasSuffix(".probe") || $0 == "texcache" || $0 == "device.metalar" || $0 == "msl_rt" || $0 == "msl_fail" }.map { work.appendingPathComponent($0) }
    let dev = work.appendingPathComponent("devarch")
    junk += ((try? FileManager.default.contentsOfDirectory(atPath: dev.path)) ?? []).filter { $0.hasSuffix(".tmp") }.map { dev.appendingPathComponent($0) }
    return removeAll(junk)
}

/// The app's pipeline archive and iOS's own compiled-shader cache for the app.
func clearShaderCache() -> String {
    let work = docsURL.appendingPathComponent("work")
    var urls = [work.appendingPathComponent("devarch"), work.appendingPathComponent("device.metalar")]
    if let caches = FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask).first {
        urls += ((try? FileManager.default.contentsOfDirectory(atPath: caches.path)) ?? []).map { caches.appendingPathComponent($0) }
    }
    return removeAll(urls)
}

let docsURL = '''),
]
for a_, b_ in reps:
    assert w.count(a_) == 1, a_[:60]
    w = w.replace(a_, b_)
sw.write_text(w)
print("patched", mn, sw, "(launcher: background textures, show memory, tools)")

# ---------------------------------------------------------------------------
# "Full shader prewarm" (launcher, off by default): before the game starts,
# compile every recorded pipeline with all CPU cores, with a progress message
# on screen; no compiling during races afterwards (NX_PREWARM_FULL=1).
io = pathlib.Path("crates/nxhost/src/gfx/ios.rs")
i = io.read_text()
old = "fn set_status(text: &str) {"
assert i.count(old) == 1
i = i.replace(old, "pub fn set_status(text: &str) {")
io.write_text(i)
gm = pathlib.Path("crates/nxhost/src/gfx/mod.rs")
g = gm.read_text()
old = "pub use ios::{run_app, set_touch_controls, show_fps, wait_foreground};"
assert g.count(old) == 1
g = g.replace(old, "pub use ios::{run_app, set_status, set_touch_controls, show_fps, wait_foreground};")
gm.write_text(g)
st = pathlib.Path("crates/nxhost/src/nvn/draw/state.rs")
u = st.read_text()
old = "            let (next, built) = (AtomicUsize::new(0), AtomicUsize::new(0));\n            std::thread::scope(|s| {\n                for _ in 0..2 {\n"
assert u.count(old) == 1
u = u.replace(old, """            let (next, built) = (AtomicUsize::new(0), AtomicUsize::new(0));
            let full = std::env::var_os("NX_PREWARM_FULL").is_some();
            PREWARM_TOTAL.store(list.len(), Relaxed);
            let workers = if full { std::thread::available_parallelism().map_or(4, |n| n.get()).clamp(2, 6) } else { 2 };
            std::thread::scope(|s| {
                for _ in 0..workers {
""")
old = "                                    let n = built.fetch_add(1, Relaxed) + 1;\n"
assert u.count(old) == 1
u = u.replace(old, old + "                                    PREWARM_BUILT.store(n, Relaxed);\n")
old = "pub fn prewarm() {"
u = u.replace(old, """static PREWARM_TOTAL: std::sync::atomic::AtomicUsize = std::sync::atomic::AtomicUsize::new(0);
static PREWARM_BUILT: std::sync::atomic::AtomicUsize = std::sync::atomic::AtomicUsize::new(0);

/// NX_PREWARM_FULL: hold the game until the prewarm is done, with progress on screen.
pub fn prewarm_wait() {
    use std::sync::atomic::Ordering::{Relaxed, SeqCst};
    let t0 = std::time::Instant::now();
    while !PREWARM_DONE.load(SeqCst) {
        let (n, total) = (PREWARM_BUILT.load(Relaxed), PREWARM_TOTAL.load(Relaxed).max(1));
        #[cfg(target_os = "ios")]
        crate::gfx::set_status(&format!("Preparing shaders… {}%\\n\\n{n} / {total}\\nOnly slow the first time", n * 100 / total));
        std::thread::sleep(std::time::Duration::from_millis(300));
    }
    #[cfg(target_os = "ios")]
    crate::gfx::set_status("");
    eprintln!("[gfx] full prewarm: game held for {:.0} s", t0.elapsed().as_secs_f32());
}

pub fn prewarm() {""", 1)
st.write_text(u)
dm = pathlib.Path("crates/nxhost/src/nvn/draw/mod.rs")
d = dm.read_text()
old = "pub use state::prewarm;"
assert d.count(old) == 1
d = d.replace(old, "pub use state::{prewarm, prewarm_wait};")
dm.write_text(d)
nm = pathlib.Path("crates/nxhost/src/nvn/mod.rs")
n_ = nm.read_text()
old = "pub use draw::{precompile, prewarm};"
assert n_.count(old) == 1
n_ = n_.replace(old, "pub use draw::{precompile, prewarm, prewarm_wait};")
nm.write_text(n_)
mn = pathlib.Path("crates/nxhost/src/main.rs")
m = mn.read_text()
old = "    if std::env::var_os(\"NX_PREWARM\").is_some() {\n        nvn::prewarm();\n    }\n"
assert m.count(old) == 1
m = m.replace(old, "    if std::env::var_os(\"NX_PREWARM\").is_some() || std::env::var_os(\"NX_PREWARM_FULL\").is_some() {\n        nvn::prewarm();\n        if std::env::var_os(\"NX_PREWARM_FULL\").is_some() {\n            nvn::prewarm_wait();\n        }\n    }\n")
old = '    if field("async_textures").as_deref() != Some("true") {\n'
assert m.count(old) == 1
m = m.replace(old, '''    if field("full_prewarm").as_deref() == Some("true") {
        def("NX_PREWARM_FULL", "1");
    }
    if let Some(v) = field("game_volume").and_then(|v| v.parse::<f32>().ok()) {
        def("NX_VOLUME", &format!("{}", v.clamp(0.0, 1.0)));
    }
''' + old)
mn.write_text(m)

# Game volume (launcher slider, NX_VOLUME 0..1): scales the game's sound only,
# handy with music from other apps.
ou = pathlib.Path("crates/nxhost/src/audio/output.rs")
o = ou.read_text()
old = "    fn fill(&mut self, buf: &mut [i16]) {\n        (self.source)(buf);\n"
assert o.count(old) == 1
o = o.replace(old, """    fn fill(&mut self, buf: &mut [i16]) {
        (self.source)(buf);
        static VOL: std::sync::LazyLock<f32> = std::sync::LazyLock::new(|| std::env::var("NX_VOLUME").ok().and_then(|v| v.trim().parse::<f32>().ok()).unwrap_or(1.0).clamp(0.0, 1.0));
        if *VOL < 0.999 {
            for s in buf.iter_mut() {
                *s = (*s as f32 * *VOL) as i16;
            }
        }
""")
ou.write_text(o)

sw = pathlib.Path("ios/Launcher.swift")
w = sw.read_text()
reps = [
("    @Published var showMemory = true\n", "    @Published var showMemory = true\n    @Published var fullPrewarm = false\n    @Published var gameVolume = 1.0\n"),
('        showMemory = json["show_memory"] as? Bool ?? true\n', '        showMemory = json["show_memory"] as? Bool ?? true\n        fullPrewarm = json["full_prewarm"] as? Bool ?? false\n        gameVolume = json["game_volume"] as? Double ?? 1.0\n'),
('        json["show_memory"] = showMemory\n', '        json["show_memory"] = showMemory\n        json["full_prewarm"] = fullPrewarm\n        json["game_volume"] = gameVolume\n'),
("""                    Toggle("Music from other apps", isOn: $s.mixAudio)""", """                    Toggle("Music from other apps", isOn: $s.mixAudio)
                    HStack {
                        Image(systemName: "speaker.fill").foregroundStyle(.secondary)
                        Slider(value: $s.gameVolume, in: 0...1)
                        Text("\\(Int(s.gameVolume * 100))%").monospacedDigit().foregroundStyle(.secondary).frame(width: 44, alignment: .trailing)
                    }"""),
("""                    Toggle("Prewarm pipelines", isOn: $s.prewarm)""", """                    Toggle("Prewarm pipelines", isOn: $s.prewarm)
                    Toggle("Full prewarm before playing (experimental)", isOn: $s.fullPrewarm)"""),
("""                    Text("Builds the game's graphics pipelines in the background at startup")""", """                    Text("Builds the game's graphics pipelines in the background at startup. Full prewarm: waits until all of them are ready (slow the first time, then no compiling during races).")"""),
]
for a_, b_ in reps:
    assert w.count(a_) == 1, a_[:60]
    w = w.replace(a_, b_)
sw.write_text(w)
print("patched", st, mn, ou, sw, "(full prewarm, game volume)")

# ---------------------------------------------------------------------------
# Launcher: "Full prewarm on first launch" (auto, when the app has no saved
# pipeline archive yet) and "Quiet mode" (no log, no watchdog, no memory log:
# a little less CPU; turn it off to send a log). Both off by default.
mn = pathlib.Path("crates/nxhost/src/main.rs")
m = mn.read_text()
old = '    if field("full_prewarm").as_deref() == Some("true") {\n        def("NX_PREWARM_FULL", "1");\n    }\n'
assert m.count(old) == 1
m = m.replace(old, '''    let first_launch = std::fs::read_dir("work/devarch").map_or(true, |r| r.flatten().all(|e| !e.file_name().to_string_lossy().ends_with(".metalar")));
    if field("full_prewarm").as_deref() == Some("true") || (field("auto_full_prewarm").as_deref() == Some("true") && first_launch) {
        def("NX_PREWARM_FULL", "1");
        if first_launch {
            eprintln!("[host] first launch: full shader prewarm before playing");
        }
    }
    if field("quiet").as_deref() == Some("true") {
        eprintln!("[host] quiet mode: no more log from here (turn it off in the launcher to get a log)");
        def("NX_NO_WATCHDOG", "1");
        def("NX_NO_MEMLOG", "1");
        if let Ok(null) = std::fs::OpenOptions::new().write(true).open("/dev/null") {
            use std::os::fd::AsRawFd;
            unsafe { libc::dup2(null.as_raw_fd(), 2) };
        }
    }
''')
mn.write_text(m)
sw = pathlib.Path("ios/Launcher.swift")
w = sw.read_text()
reps = [
("    @Published var fullPrewarm = false\n", "    @Published var fullPrewarm = false\n    @Published var autoFullPrewarm = false\n    @Published var quiet = false\n"),
('        fullPrewarm = json["full_prewarm"] as? Bool ?? false\n', '        fullPrewarm = json["full_prewarm"] as? Bool ?? false\n        autoFullPrewarm = json["auto_full_prewarm"] as? Bool ?? false\n        quiet = json["quiet"] as? Bool ?? false\n'),
('        json["full_prewarm"] = fullPrewarm\n', '        json["full_prewarm"] = fullPrewarm\n        json["auto_full_prewarm"] = autoFullPrewarm\n        json["quiet"] = quiet\n'),
("""                    Toggle("Full prewarm before playing (experimental)", isOn: $s.fullPrewarm)""", """                    Toggle("Full prewarm before playing (experimental)", isOn: $s.fullPrewarm)
                    Toggle("Full prewarm on first launch only", isOn: $s.autoFullPrewarm)"""),
("""                    Button("Clean up unneeded files") { toolMessage = cleanWork() }""", """                    Toggle("Quiet mode (no log)", isOn: $s.quiet)
                    Button("Clean up unneeded files") { toolMessage = cleanWork() }"""),
]
for a_, b_ in reps:
    assert w.count(a_) == 1, a_[:60]
    w = w.replace(a_, b_)
sw.write_text(w)
print("patched", mn, sw, "(first-launch full prewarm, quiet mode)")

# ---------------------------------------------------------------------------
# Precompiled GPU binaries for the A16 (iPhone 15, 14 Pro: applegpu_g15p) and
# the A15 (iPhone 14, 13: applegpu_g14p), built on the GitHub Mac with the
# newest Xcode there, aimed at that Xcode's iOS SDK. Shipped in the app;
# the phone loads the file for its own GPU only when the launcher switch
# "Precompiled shaders (experimental)" is on (NX_PIPELINE_ARCHIVE). Off by
# default: an archive built with the iOS 26 tools crashed the iOS 27 driver once.
tt = pathlib.Path("tt-a16.sh")
tt.write_text(r'''#!/bin/sh
CACHE="$1"
LIST="$CACHE/pipelines.all.tsv"
[ -f "$LIST" ] && [ -f "$CACHE/index.tsv" ] || { echo "[tt] no shader cache yet"; exit 0; }
X="$(for x in /Applications/Xcode*.app; do echo "$(DEVELOPER_DIR=$x/Contents/Developer xcrun -sdk iphoneos --show-sdk-version 2>/dev/null) $x"; done | sort -V | tail -1 | cut -d' ' -f2-)"
[ -n "$X" ] && export DEVELOPER_DIR="$X/Contents/Developer"
SDK="$(xcrun -sdk iphoneos --show-sdk-version)"
echo "[tt] using $X (iOS SDK $SDK)"
STAMP="$(cat "$LIST" "$CACHE/index.tsv" "$CACHE/guest.tsv" a16/gen.py | shasum | cut -d' ' -f1)-$SDK"
rm -f "$CACHE/pipelines-a16.metalar" "$CACHE/pipelines-a16.metalar.stamp"
SCRIPT="$CACHE/tt.mtlp-json"
for ARCH in applegpu_g15p applegpu_g14p; do
  OUT="$CACHE/pipelines-$ARCH.metalar"
  if [ -f "$OUT" ] && [ "$(cat "$OUT.stamp" 2>/dev/null)" = "$STAMP" ]; then echo "[tt] $ARCH up to date"; ls -lh "$OUT"; continue; fi
  rm -f "$OUT" "$OUT.stamp"
  [ -f "$SCRIPT" ] || python3 a16/gen.py "$CACHE" "$LIST" a16/metal-enums.json "$SCRIPT" || exit 1
  date
  ok=0
  for TGT in "ios$SDK" ios27.0 ios26.0; do
    echo "[tt] $ARCH, target $TGT"
    if perl -e 'alarm shift; exec @ARGV' 2700 xcrun -sdk iphoneos metal-tt -target "air64-apple-$TGT" -arch $ARCH -compress-resources "$SCRIPT" -o "$OUT.tmp"; then ok=1; break; fi
    rm -f "$OUT.tmp"
  done
  date
  [ $ok = 1 ] || { echo "[tt] $ARCH failed"; continue; }
  mv "$OUT.tmp" "$OUT"
  echo "$STAMP" > "$OUT.stamp"
  xcrun metal-lipo -archs "$OUT"
  ls -lh "$OUT"
done
rm -f "$SCRIPT"
echo "[tt] done"
''')
mk = pathlib.Path("make-ios.sh")
k = mk.read_text()
old = 'cp "$LIST" "$RES/shaders/pipelines.tsv"\n'
assert k.count(old) == 1
k = k.replace(old, old + 'for f in "$CACHE"/pipelines-applegpu_*.metalar; do [ -f "$f" ] && cp "$f" "$RES/shaders/" && echo "shipping $(basename "$f")"; done\n')
k = k.replace('if [ -f "$CACHE/pipelines-a16.metalar" ]; then cp "$CACHE/pipelines-a16.metalar" "$RES/shaders/"; fi\n', '')
mk.write_text(k)

st = pathlib.Path("crates/nxhost/src/nvn/draw/state.rs")
u = st.read_text()
old = '.filter(|n| n.starts_with("pipelines") && n.ends_with(".metalar"))'
assert u.count(old) == 1
u = u.replace(old, '.filter(|n| *n == format!("pipelines-{}.metalar", gfx::gpu().device.0.architecture().name()))')
old = '    eprintln!("[gfx] {} pipeline archive(s) loaded", archives.len());'
assert u.count(old) == 1
u = u.replace(old, '    eprintln!("[gfx] {} pipeline archive(s) loaded{}", archives.len(), if std::env::var_os("NX_PIPELINE_ARCHIVE").is_some() { format!(" (precompiled shaders for {} on)", gfx::gpu().device.0.architecture().name()) } else { String::new() });')
st.write_text(u)

mn = pathlib.Path("crates/nxhost/src/main.rs")
m = mn.read_text()
old = '    if field("quiet").as_deref() == Some("true") {\n'
assert m.count(old) == 1
m = m.replace(old, '    if field("precompiled_shaders").as_deref() == Some("true") {\n        def("NX_PIPELINE_ARCHIVE", "1");\n    }\n' + old)
mn.write_text(m)

sw = pathlib.Path("ios/Launcher.swift")
w = sw.read_text()
reps = [
("    @Published var quiet = false\n", "    @Published var quiet = false\n    @Published var precompiled = false\n"),
('        quiet = json["quiet"] as? Bool ?? false\n', '        quiet = json["quiet"] as? Bool ?? false\n        precompiled = json["precompiled_shaders"] as? Bool ?? false\n'),
('        json["quiet"] = quiet\n', '        json["quiet"] = quiet\n        json["precompiled_shaders"] = precompiled\n'),
("""                    Toggle("Full prewarm on first launch only", isOn: $s.autoFullPrewarm)""", """                    Toggle("Full prewarm on first launch only", isOn: $s.autoFullPrewarm)
                    Toggle("Precompiled shaders (experimental)", isOn: $s.precompiled)"""),
("""Full prewarm: waits until all of them are ready (slow the first time, then no compiling during races).")""", """Full prewarm: waits until all of them are ready (slow the first time, then no compiling during races). Precompiled shaders: ready-made GPU code for the A15/A16 packed in the app; if the game crashes at start, turn it off.")"""),
]
for a_, b_ in reps:
    assert w.count(a_) == 1, a_[:60]
    w = w.replace(a_, b_)
sw.write_text(w)
print("patched", tt, mk, st, mn, sw, "(precompiled shaders for A15 and A16)")

# ---------------------------------------------------------------------------
# Texture upload staging limit. Each texture upload copies its pixels into a
# fresh shared buffer that lives until the GPU has copied it; loading a track
# uploads hundreds of textures in one go, and those buffers pile up (a likely
# reason for the game being killed while a race loads). At most ~96 MB of
# staging buffers stay in flight: beyond that, wait for the oldest uploads.
tx = pathlib.Path("crates/nxhost/src/nvn/draw/textures.rs")
y = tx.read_text()
old = """    enc.endEncoding();
    cb.commit();
}

pub fn compare_fn(nvn: u32) -> MTLCompareFunction {"""
assert y.count(old) == 1
y = y.replace(old, """    enc.endEncoding();
    cb.commit();
    throttle_staging(cb, total);
}

static STAGING: std::sync::Mutex<std::collections::VecDeque<(Shared<gfx::CommandBuffer>, usize)>> = std::sync::Mutex::new(std::collections::VecDeque::new());

fn throttle_staging(cb: objc2::rc::Retained<gfx::CommandBuffer>, bytes: usize) {
    const LIMIT: usize = 96 << 20;
    let mut q = STAGING.lock().unwrap();
    q.retain(|(c, _)| !matches!(c.0.status(), MTLCommandBufferStatus::Completed | MTLCommandBufferStatus::Error));
    q.push_back((Shared(cb), bytes));
    let mut total: usize = q.iter().map(|(_, b)| *b).sum();
    let mut waited = false;
    while total > LIMIT && q.len() > 1 {
        let (c, b) = q.pop_front().unwrap();
        c.0.waitUntilCompleted();
        total -= b;
        waited = true;
    }
    drop(q);
    static LOGGED: std::sync::atomic::AtomicUsize = std::sync::atomic::AtomicUsize::new(0);
    if waited && LOGGED.fetch_add(1, std::sync::atomic::Ordering::Relaxed) < 5 {
        eprintln!("[gfx] texture uploads: waited for the GPU (over 96 MB of upload buffers in flight)");
    }
}

pub fn compare_fn(nvn: u32) -> MTLCompareFunction {""")
tx.write_text(y)

# Memory log: also every time the app grows or shrinks by 150 MB within a
# second (shows what happens right before iOS closes the app).
gm = pathlib.Path("crates/nxhost/src/gfx/mod.rs")
g = gm.read_text()
old = """        loop {
            std::thread::sleep(std::time::Duration::from_secs(10));
            let (used, left) = (footprint(), available_memory());"""
assert g.count(old) == 1
g = g.replace(old, """        let mut last_used = 0u64;
        let mut tick = 0u32;
        loop {
            std::thread::sleep(std::time::Duration::from_secs(1));
            tick += 1;
            let now = footprint();
            if tick % 10 != 0 {
                if now.abs_diff(last_used) > 150 << 20 && last_used != 0 {
                    eprintln!("[mem] {:.0} s: jump to {} MB (was {} MB), iOS still allows {} MB", t0.elapsed().as_secs_f32(), now >> 20, last_used >> 20, available_memory() >> 20);
                    last_used = now;
                }
                continue;
            }
            last_used = now;
            let (used, left) = (now, available_memory());""")
gm.write_text(g)
print("patched", tx, gm, "(upload staging limit, memory jump log)")

# NX_SKIP_SHADERS=hash,hash: never draw with these vertex/fragment shaders
# (to test whether one shader brings the game down).
st = pathlib.Path("crates/nxhost/src/nvn/draw/state.rs")
u = st.read_text()
old = "        let mut key = PipeKey::new();\n        for w in [vs_key.0, vs_key.1 as u64, fs_key,"
assert u.count(old) == 1
u = u.replace(old, """        static SKIP: LazyLock<Vec<u64>> = LazyLock::new(|| std::env::var("NX_SKIP_SHADERS").unwrap_or_default().split(',').filter_map(|s| u64::from_str_radix(s.trim(), 16).ok()).collect());
        if !SKIP.is_empty() && (SKIP.contains(&vs_key.0) || SKIP.contains(&fs_key)) {
            return Err("shader skipped (NX_SKIP_SHADERS)");
        }
""" + old)
st.write_text(u)
print("patched", st, "(NX_SKIP_SHADERS)")

# ---------------------------------------------------------------------------
# Memory: pipelines the prewarm found in the device archive are not kept
# (the game gets them back from the archive in about a millisecond when it
# needs them); only freshly compiled ones stay in memory. The last race used
# 2.97 of the ~3.07 GB iOS allows, races that need a bit more were killed.
st = pathlib.Path("crates/nxhost/src/nvn/draw/state.rs")
u = st.read_text()
old = "                                    PREWARMED.lock().unwrap().insert(r.clone(), Shared(p));\n"
assert u.count(old) == 1
u = u.replace(old, "                                    if !from_archive {\n                                        PREWARMED.lock().unwrap().insert(r.clone(), Shared(p));\n                                    }\n")
st.write_text(u)

# Launcher Textures: "Ultra low" = NX_TEX_DROP=3 (8x smaller sharpest size)
mn = pathlib.Path("crates/nxhost/src/main.rs")
m = mn.read_text()
old = '            for (k, v) in [("NX_TEX_DROP", "2"), ("NX_TEX_DROP_SHRINK", "1"), ("NX_TEX_DROP_MIN", "256")] {'
assert m.count(old) == 1
m = m.replace(old, '            for (k, v) in [("NX_TEX_DROP", if q == "ultralow" { "3" } else { "2" }), ("NX_TEX_DROP_SHRINK", "1"), ("NX_TEX_DROP_MIN", if q == "ultralow" { "128" } else { "256" })] {')
mn.write_text(m)
sw = pathlib.Path("ios/Launcher.swift")
w = sw.read_text()
old = '                        Text("Very low (fastest)").tag("verylow")\n'
assert w.count(old) == 1
w = w.replace(old, '                        Text("Ultra low (least memory)").tag("ultralow")\n' + old)
sw.write_text(w)
print("patched", st, mn, sw, "(prewarm keeps only fresh pipelines, Ultra low textures)")

# ---------------------------------------------------------------------------
# Precompiled A15/A16 shader files removed (700 MB in the app for little gain;
# the device archive in work/devarch does the job): no metal-tt step, nothing
# shipped, no launcher switch.
mk = pathlib.Path("make-ios.sh")
k = mk.read_text()
k = k.replace('for f in "$CACHE"/pipelines-applegpu_*.metalar; do [ -f "$f" ] && cp "$f" "$RES/shaders/" && echo "shipping $(basename "$f")"; done\n', '')
k = k.replace('sh ./tt-a16.sh "$CACHE" > work/a16.log 2>&1 || echo "A16 pipeline archive failed, building without it"\ntail -5 work/a16.log || true\n\n', '')
assert "tt-a16" not in k and "pipelines-applegpu" not in k
mk.write_text(k)
sw = pathlib.Path("ios/Launcher.swift")
w = sw.read_text()
old = '\n                    Toggle("Precompiled shaders (experimental)", isOn: $s.precompiled)'
assert w.count(old) == 1
w = w.replace(old, '')
w = w.replace(" Precompiled shaders: ready-made GPU code for the A15/A16 packed in the app; if the game crashes at start, turn it off.", "")
w = w.replace("Background texture loading (experimental): fewer small hitches, new objects are grey for a moment.", "Background texture loading (experimental): fewer small hitches, but can run out of memory while a race loads.")
sw.write_text(w)
print("patched", mk, sw, "(precompiled shader files removed)")

# ---------------------------------------------------------------------------
# Defaults from the tests: Ultra low textures, full prewarm before playing,
# low-memory trim from 600 MB left.
mn = pathlib.Path("crates/nxhost/src/main.rs")
m = mn.read_text()
old = 'let q = field("texture_quality").unwrap_or_else(|| "verylow".into());'
assert m.count(old) == 1
m = m.replace(old, 'let q = field("texture_quality").unwrap_or_else(|| "ultralow".into());')
old = 'if field("full_prewarm").as_deref() == Some("true") ||'
assert m.count(old) == 1
m = m.replace(old, 'if field("full_prewarm").as_deref() != Some("false") ||')
old = '    def("NX_HASH_EVERY", "2");\n'
assert m.count(old) == 1
m = m.replace(old, old + '    def("NX_TRIM_MB", "900");\n')
mn.write_text(m)
sw = pathlib.Path("ios/Launcher.swift")
w = sw.read_text()
for a_, b_ in [('    @Published var textureQuality = "verylow"\n', '    @Published var textureQuality = "ultralow"\n'),
               ('        textureQuality = json["texture_quality"] as? String ?? "verylow"\n', '        textureQuality = json["texture_quality"] as? String ?? "ultralow"\n'),
               ('    @Published var fullPrewarm = false\n', '    @Published var fullPrewarm = true\n'),
               ('        fullPrewarm = json["full_prewarm"] as? Bool ?? false\n', '        fullPrewarm = json["full_prewarm"] as? Bool ?? true\n')]:
    assert w.count(a_) == 1, a_
    w = w.replace(a_, b_)
sw.write_text(w)
print("patched", mn, sw, "(defaults: Ultra low, full prewarm, trim 600)")

# ---------------------------------------------------------------------------
# Device pipeline archive: forget it by itself when iOS or the GPU changes.
# A pipeline archive only works on the iOS build and GPU it was made on; after
# an iOS update (or a work/ folder copied from another iPhone) the old files
# are never hit and fill the 400-file cap, so nothing new gets saved.
# work/devarch/os.txt remembers "<iOS build> <GPU architecture>".
st = pathlib.Path("crates/nxhost/src/nvn/draw/state.rs")
u = st.read_text()
old = """fn device_archive_files() -> Vec<String> {
"""
assert u.count(old) == 1
u = u.replace(old, """fn os_build() -> String {
    let mut buf = [0u8; 64];
    let mut len = buf.len();
    let r = unsafe { libc::sysctlbyname(c"kern.osversion".as_ptr(), buf.as_mut_ptr().cast(), &mut len, std::ptr::null_mut(), 0) };
    if r != 0 {
        return String::new();
    }
    String::from_utf8_lossy(&buf[..len]).trim_end_matches('\\0').trim().to_string()
}

fn check_device_archive_os() {
    let id = format!("{} {}", os_build(), gfx::gpu().device.0.architecture().name());
    let stamp = format!("{DEVICE_ARCHIVE_DIR}/os.txt");
    let old = std::fs::read_to_string(&stamp).map(|s| s.trim().to_string()).ok();
    if old.as_deref().is_some_and(|o| o != id) {
        let mut n = 0;
        if let Ok(r) = std::fs::read_dir(DEVICE_ARCHIVE_DIR) {
            for e in r.flatten() {
                if e.file_name().to_string_lossy().contains(".metalar") && std::fs::remove_file(e.path()).is_ok() {
                    n += 1;
                }
            }
        }
        eprintln!("[gfx] iOS or GPU changed ({} -> {id}): device pipeline archive cleared ({n} files)", old.as_deref().unwrap_or_default());
    }
    if old.as_deref() != Some(id.as_str()) {
        let _ = std::fs::create_dir_all(DEVICE_ARCHIVE_DIR);
        let _ = std::fs::write(&stamp, &id);
    }
}

fn device_archive_files() -> Vec<String> {
    static CHECK: std::sync::Once = std::sync::Once::new();
    CHECK.call_once(check_device_archive_os);
""")

# ---------------------------------------------------------------------------
# Retry a pipeline whose build failed: the shader compiler service on iOS
# sometimes dies mid-build ("XPC_ERROR_CONNECTION_INTERRUPTED") and the
# pipeline then stayed black for the whole session. Two more tries, 1 s then 3 s
# later (failures are rare, so the wait costs nothing normally).
old = """        Err(e) => {
            eprintln!("[gfx] pipeline {name} failed: {}", e.localizedDescription());
            None
        }
    }
}
"""
assert u.count(old) == 1
u = u.replace(old, """        Err(e) => {
            eprintln!("[gfx] pipeline {name} failed: {}", e.localizedDescription());
            for (i, wait) in [1u64, 3].into_iter().enumerate() {
                std::thread::sleep(std::time::Duration::from_secs(wait));
                match dev.newRenderPipelineStateWithDescriptor_error(&d) {
                    Ok(p) => {
                        eprintln!("[gfx] pipeline {name}: retry {} worked", i + 1);
                        harvest(d.clone());
                        return Some(Shared(p));
                    }
                    Err(e) => eprintln!("[gfx] pipeline {name}: retry {} failed: {}", i + 1, e.localizedDescription()),
                }
            }
            None
        }
    }
}
""")
st.write_text(u)
print("patched", st, "(devarch cleared on iOS/GPU change, retry failed pipelines)")

# ---------------------------------------------------------------------------
# NX_ASYNC_GFX=1 (launcher: "Graphics on a separate thread", experimental):
# translating the game's NVN command buffers to Metal (textures, pipelines,
# draws) ran on the game's render thread, so game logic + translation added up
# (~18 + ~10 ms a frame in races). Now QueueSubmitCommands only hands the
# recorded commands to a "gfx-submit" thread and returns, like a real GPU
# queue; the game goes on with the next frame while the previous one is
# translated. Order is kept: fences, presents and freed memory pools go
# through the same queue, SyncWait waits for its fence to be reached,
# QueueFinish / texel reads and writes wait for everything queued. The game
# can be at most one presented frame ahead.
md = pathlib.Path("crates/nxhost/src/nvn/draw/mod.rs")
z = md.read_text()

def rep(old, new, count=1):
    global z
    assert z.count(old) == count, old[:80]
    z = z.replace(old, new)

# command buffers referenced by a queued submit are kept alive with it
rep("cmd::recording(*h)", "lookup(*h)", 4)
rep("pub fn submit(handles: &[u64]) {\n", """thread_local! {
    static PINNED: std::cell::RefCell<HashMap<u64, std::sync::Arc<Vec<Cmd>>>> = Default::default();
    static JOB_FRAME: std::cell::Cell<Option<u64>> = const { std::cell::Cell::new(None) };
}

fn lookup(h: u64) -> Option<std::sync::Arc<Vec<Cmd>>> {
    PINNED.with(|p| p.borrow().get(&h).cloned()).or_else(|| cmd::recording(h))
}

/// The frame a command buffer belongs to (on the graphics thread: the frame
/// that was current when it was submitted).
fn cur_frame() -> u64 {
    JOB_FRAME.with(|f| f.get()).unwrap_or_else(|| FRAME.load(Relaxed))
}

fn pin_tree(h: u64, depth: usize, out: &mut HashMap<u64, std::sync::Arc<Vec<Cmd>>>) {
    if out.contains_key(&h) {
        return;
    }
    let Some(c) = cmd::recording(h) else { return };
    out.insert(h, c.clone());
    if depth < 8 {
        for x in c.iter() {
            if let Cmd::Call(hs) = x {
                for &s in hs {
                    pin_tree(s, depth + 1, out);
                }
            }
        }
    }
}

mod async_gfx {
    use super::*;
    use std::collections::VecDeque;
    use std::sync::Condvar;
    use std::sync::atomic::{AtomicU64, Ordering::SeqCst};

    pub enum Job {
        Submit { handles: Vec<u64>, pinned: HashMap<u64, std::sync::Arc<Vec<Cmd>>>, frame: u64 },
        Fence(u64),
        Present(TexBuilder),
        Forget(u64, u64),
    }

    static QUEUE: Mutex<VecDeque<(u64, Job)>> = Mutex::new(VecDeque::new());
    static WORK: Condvar = Condvar::new();
    static DONE_LOCK: Mutex<()> = Mutex::new(());
    static DONE_CV: Condvar = Condvar::new();
    static ENQ: AtomicU64 = AtomicU64::new(0);
    static DONE: AtomicU64 = AtomicU64::new(0);
    static LAST_PRESENT: AtomicU64 = AtomicU64::new(0);
    static WAIT_NS: AtomicU64 = AtomicU64::new(0);
    pub static FENCES: LazyLock<Mutex<HashMap<u64, u64>>> = LazyLock::new(Default::default);

    pub fn on() -> bool {
        static ON: LazyLock<bool> = LazyLock::new(|| std::env::var_os("NX_ASYNC_GFX").is_some_and(|v| v != "0"));
        *ON
    }

    pub fn take_wait_ms() -> f64 {
        gfx::prof::take_ms(&WAIT_NS)
    }

    pub fn push(job: Job) -> u64 {
        start();
        let present = matches!(job, Job::Present(_));
        let seq = {
            let mut q = QUEUE.lock().unwrap();
            let seq = ENQ.fetch_add(1, SeqCst) + 1;
            q.push_back((seq, job));
            seq
        };
        WORK.notify_one();
        if present {
            // at most one presented frame ahead of the screen
            wait_for(LAST_PRESENT.swap(seq, SeqCst));
        } else if seq > DONE.load(SeqCst) + 512 {
            wait_for(seq - 256);
        }
        seq
    }

    pub fn wait_for(seq: u64) {
        if DONE.load(SeqCst) >= seq {
            return;
        }
        let t0 = std::time::Instant::now();
        crate::kernel::blocking(|| {
            let mut g = DONE_LOCK.lock().unwrap();
            while DONE.load(SeqCst) < seq {
                g = DONE_CV.wait(g).unwrap();
            }
        });
        WAIT_NS.fetch_add(t0.elapsed().as_nanos() as u64, Relaxed);
    }

    /// Wait until everything queued so far has been handed to Metal.
    pub fn drain() {
        if on() {
            wait_for(ENQ.load(SeqCst));
        }
    }

    pub fn submit(handles: &[u64]) {
        let mut pinned = HashMap::new();
        for &h in handles {
            pin_tree(h, 0, &mut pinned);
        }
        push(Job::Submit { handles: handles.to_vec(), pinned, frame: FRAME.load(Relaxed) });
    }

    fn start() {
        static ONCE: std::sync::Once = std::sync::Once::new();
        ONCE.call_once(|| {
            std::thread::Builder::new()
                .name("gfx-submit".into())
                .stack_size(8 << 20)
                .spawn(|| {
                    unsafe { libc::pthread_set_qos_class_self_np(libc::qos_class_t::QOS_CLASS_USER_INTERACTIVE, 0) };
                    eprintln!("[gfx] graphics on a separate thread (NX_ASYNC_GFX)");
                    loop {
                        let (seq, job) = {
                            let mut q = QUEUE.lock().unwrap();
                            loop {
                                if let Some(j) = q.pop_front() {
                                    break j;
                                }
                                q = WORK.wait(q).unwrap();
                            }
                        };
                        match job {
                            Job::Submit { handles, pinned, frame } => {
                                PINNED.with(|p| *p.borrow_mut() = pinned);
                                JOB_FRAME.with(|f| f.set(Some(frame)));
                                submit_now(&handles);
                                PINNED.with(|p| p.borrow_mut().clear());
                            }
                            Job::Fence(s) => fence_now(s),
                            Job::Present(b) => present_now(b),
                            Job::Forget(a, s) => DEAD.lock().unwrap().push((a, a + s)),
                        }
                        {
                            let _g = DONE_LOCK.lock().unwrap();
                            DONE.store(seq, SeqCst);
                        }
                        DONE_CV.notify_all();
                    }
                })
                .expect("graphics thread");
        });
    }
}

pub fn submit(handles: &[u64]) {
    if async_gfx::on() {
        async_gfx::submit(handles);
    } else {
        submit_now(handles);
    }
}

fn submit_now(handles: &[u64]) {
""")

rep("""pub fn forget_memory(addr: u64, size: u64) {
    if size > 0 {
""", """pub fn forget_memory(addr: u64, size: u64) {
    if size > 0 && async_gfx::on() {
        async_gfx::push(async_gfx::Job::Forget(addr, size));
        return;
    }
    if size > 0 {
""")
rep("""pub fn fence_sync(sync: u64) {
    let mut e""", """pub fn fence_sync(sync: u64) {
    if async_gfx::on() {
        let mut f = async_gfx::FENCES.lock().unwrap();
        let seq = async_gfx::push(async_gfx::Job::Fence(sync));
        f.insert(sync, seq);
        return;
    }
    fence_now(sync);
}

fn fence_now(sync: u64) {
    let mut e""")
rep("""pub fn finish() {
""", """pub fn finish() {
    async_gfx::drain();
""")
rep("""pub fn sync_wait(sync: u64) -> u64 {
""", """pub fn sync_wait(sync: u64) -> u64 {
    if async_gfx::on() {
        let seq = async_gfx::FENCES.lock().unwrap().remove(&sync);
        if let Some(seq) = seq {
            async_gfx::wait_for(seq);
        }
    }
""")
rep("""pub fn write_texels(mut b: TexBuilder, view: u64, region: [i32; 6], data: *const u8, strides: Option<(usize, usize)>) {
""", """pub fn write_texels(mut b: TexBuilder, view: u64, region: [i32; 6], data: *const u8, strides: Option<(usize, usize)>) {
    async_gfx::drain();
""")
rep("""    objc2::rc::autoreleasepool(|_| {
        let b = super::objects::texture(texture).map(|t| t.builder);
        let Some(b) = b else { return };
        let t = {""", """    let b = super::objects::texture(texture).map(|t| t.builder);
    let Some(b) = b else { return };
    if async_gfx::on() {
        async_gfx::push(async_gfx::Job::Present(b));
    } else {
        present_now(b);
    }
}

fn present_now(b: TexBuilder) {
    objc2::rc::autoreleasepool(|_| {
        let t = {""")
# frameskip decisions use the frame the commands were recorded in
rep("    *N > 0 && FRAME.load(Relaxed) % (*N + 1) != 0\n", "    *N > 0 && cur_frame() % (*N + 1) != 0\n")
rep("    let frame = FRAME.load(Relaxed);\n    let mut last = LAST.lock().unwrap();\n", "    let frame = cur_frame();\n    let mut last = LAST.lock().unwrap();\n")
# profile line: with the graphics thread, "game" is the time the game thread
# did not spend waiting for it
rep("""            ((secs - submit - wait) / frames).max(0.0),""", """            ((if async_gfx::on() { secs - gfx_waited } else { secs - submit - wait }) / frames).max(0.0),""")
rep("""        let gpu = gpu_ms();
        let draws = gfx::prof::DRAWS.swap(0, Relaxed);""", """        let gpu = gpu_ms();
        let gfx_waited = async_gfx::take_wait_ms();
        let draws = gfx::prof::DRAWS.swap(0, Relaxed);""")
rep("""            skipped
        );
""", """            skipped
        );
        if async_gfx::on() {
            eprintln!("[prof] graphics thread: busy {:.1} ms per frame, game thread waited for it {:.1} ms per frame", submit / frames, gfx_waited / frames);
        }
""")
md.write_text(z)

mn = pathlib.Path("crates/nxhost/src/main.rs")
m = mn.read_text()
old = '    if field("async_textures").as_deref() != Some("true") {\n'
assert m.count(old) == 1
m = m.replace(old, '''    if field("async_gfx").as_deref() == Some("true") {
        def("NX_ASYNC_GFX", "1");
    }
''' + old)
mn.write_text(m)

sw = pathlib.Path("ios/Launcher.swift")
w = sw.read_text()
reps = [
("    @Published var asyncTextures = false\n", "    @Published var asyncTextures = false\n    @Published var asyncGfx = false\n"),
('        asyncTextures = json["async_textures"] as? Bool ?? false\n', '        asyncTextures = json["async_textures"] as? Bool ?? false\n        asyncGfx = json["async_gfx"] as? Bool ?? false\n'),
('        json["async_textures"] = asyncTextures\n', '        json["async_textures"] = asyncTextures\n        json["async_gfx"] = asyncGfx\n'),
('                    Toggle("Background texture loading", isOn: $s.asyncTextures)\n', '                    Toggle("Background texture loading", isOn: $s.asyncTextures)\n                    Toggle("Graphics on a separate thread", isOn: $s.asyncGfx)\n'),
("Background texture loading (experimental): fewer small hitches, but can run out of memory while a race loads.", "Background texture loading (experimental): fewer small hitches, but can run out of memory while a race loads. Graphics on a separate thread (experimental): the game and the graphics run on two CPU cores at once, more FPS."),
]
for a_, b_ in reps:
    assert w.count(a_) == 1, a_[:60]
    w = w.replace(a_, b_)
sw.write_text(w)
print("patched", md, mn, sw, "(NX_ASYNC_GFX: graphics on a separate thread)")

# ---------------------------------------------------------------------------
# NX_TRACE_ROMFS=1 (debug): log each game file the first time it is read
# ("[romfs] 12.3 s /Course/...") so a scripted run shows which track,
# character or menu got loaded.
fsr = pathlib.Path("crates/nxhost/src/services/fs.rs")
f_ = fsr.read_text()
old = """                let dst = unsafe { std::slice::from_raw_parts_mut(b.addr as *mut u8, size as usize) };
                if let Err(e) = crate::kernel::blocking(|| self.s.read_at(off, dst)) {"""
assert f_.count(old) == 1
f_ = f_.replace(old, """                if self.name == "romfs" || self.name == "add-on content" {
                    trace_romfs(self.name, &self.s, off);
                }
""" + old)
old = "struct Storage {\n"
assert f_.count(old) == 1
f_ = f_.replace(old, """fn trace_romfs(name: &'static str, s: &DynStorage, off: u64) {
    static ON: std::sync::LazyLock<bool> = std::sync::LazyLock::new(|| std::env::var_os("NX_TRACE_ROMFS").is_some());
    if !*ON {
        return;
    }
    static T0: std::sync::LazyLock<std::time::Instant> = std::sync::LazyLock::new(std::time::Instant::now);
    type Index = (Vec<(u64, u64, String)>, std::collections::HashSet<usize>);
    static FILES: Mutex<Option<std::collections::HashMap<(&'static str, u64), Index>>> = Mutex::new(None);
    let mut all = FILES.lock().unwrap();
    let (files, seen) = all.get_or_insert_with(Default::default).entry((name, s.size())).or_insert_with(|| {
        let mut v: Vec<(u64, u64, String)> = nxfs::romfs::RomFs::open(s.clone()).map(|r| r.files().into_iter().map(|f| (f.offset, f.size, f.path)).collect()).unwrap_or_default();
        v.sort();
        eprintln!("[romfs] {name}: {} files", v.len());
        (v, Default::default())
    });
    let i = files.partition_point(|f| f.0 <= off);
    if i == 0 || off >= files[i - 1].0 + files[i - 1].1.max(1) {
        return;
    }
    if seen.insert(i - 1) {
        let tag = if name == "romfs" { "" } else { "dlc:" };
        eprintln!("[romfs] {:.1} s {tag}{}", T0.elapsed().as_secs_f32(), files[i - 1].2);
    }
}

""" + old)
fsr.write_text(f_)
print("patched", fsr, "(NX_TRACE_ROMFS)")

# ---------------------------------------------------------------------------
# Movies: a guest thread waiting for the next video frame blocked inside its
# emulated CPU core (it still does, for 250 ms at most). When the iOS video decoder stalls (seen after "1 player":
# AVAssetReader copyNextSampleBuffer never returns), the core stayed taken
# forever and the threads scheduled on it never ran again: the game froze on
# the menu while still drawing. Now after
# 250 ms without a new frame the previous one is shown again (a black frame
# the first time), so the game never waits on the decoder.
vd = pathlib.Path("crates/nxhost/src/movie/video.rs")
v = vd.read_text()
old = """        let Ok(mut frame) = self.worker.frames.recv() else { return false };
        std::mem::swap(out, &mut frame);
        let _ = self.worker.recycle.send(frame);
        true
"""
assert v.count(old) == 1
v = v.replace(old, """        use std::sync::mpsc::RecvTimeoutError;
        // no kernel::blocking here: decoder_call holds the movie objects lock, and a
        // thread giving up its core while holding it deadlocked the menus (other
        // threads waiting for that lock keep their cores). 250 ms at most, then the
        // previous frame.
        let r = self.worker.frames.recv_timeout(std::time::Duration::from_millis(250));
        let mut frame = match r {
            Ok(f) => f,
            Err(RecvTimeoutError::Disconnected) => return false,
            Err(RecvTimeoutError::Timeout) => {
                static WARNED: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);
                if !WARNED.swap(true, std::sync::atomic::Ordering::Relaxed) {
                    eprintln!("[movie] video decoder is late: showing the previous frame instead of waiting");
                }
                let size = self.layout.0 * self.layout.1 * 3 / 2;
                if out.len() != size {
                    out.clear();
                    out.resize(size, 0);
                    out[self.layout.0 * self.layout.1..].fill(128);
                }
                return true;
            }
        };
        std::mem::swap(out, &mut frame);
        let _ = self.worker.recycle.send(frame);
        true
""")
old = "    ready.recv().ok()?.then_some(Worker { frames, recycle })\n"
assert v.count(old) == 1
v = v.replace(old, "    ready.recv_timeout(std::time::Duration::from_secs(5)).ok()?.then_some(Worker { frames, recycle })\n")
vd.write_text(v)
print("patched", vd, "(video frames: never hold the core, never wait on a stalled decoder)")

# ---------------------------------------------------------------------------
# Memory while a race loads: the low-memory trim runs every 30 frames, but a
# race loads its textures in one or two very long frames (2+ s), so memory went
# from 1.9 to 2.95 GB before any trim and iOS killed the app. Now every 8 new
# textures the remaining memory is checked; under NX_TRIM_EMERGENCY_MB
# (default 300) the textures not used in the current or previous frame (the
# menu's) are dropped at once. They come back if needed.
tx = pathlib.Path("crates/nxhost/src/nvn/draw/textures.rs")
y = tx.read_text()
old = "        let tex = self.create(b, key, format, render_target)?;\n"
assert y.count(old) == 1
y = y.replace(old, "        self.emergency_trim();\n" + old)
old = "    pub fn trim(&mut self) {\n"
assert y.count(old) == 1
y = y.replace(old, """    fn emergency_trim(&mut self) {
        use std::sync::atomic::{AtomicU32, Ordering::Relaxed};
        static N: AtomicU32 = AtomicU32::new(0);
        static LIMIT: std::sync::LazyLock<u64> = std::sync::LazyLock::new(|| std::env::var("NX_TRIM_EMERGENCY_MB").ok().and_then(|v| v.trim().parse::<u64>().ok()).unwrap_or(300) << 20);
        if N.fetch_add(1, Relaxed) % 8 != 0 || *LIMIT == 0 {
            return;
        }
        let left = gfx::available_memory();
        if left == 0 || left > *LIMIT {
            return;
        }
        let frame = self.frame;
        let old: std::collections::HashSet<TexKey> = self
            .uploaded
            .keys()
            .filter(|k| self.last_used.get(*k).is_none_or(|&f| f + 1 < frame))
            .copied()
            .collect();
        if old.is_empty() {
            return;
        }
        self.evict(&|k| old.contains(k));
        self.last_used.retain(|k, _| !old.contains(k));
        eprintln!("[mem] memory almost full while loading ({} MB left): dropped {} textures not used right now", left >> 20, old.len());
    }

""" + old)
tx.write_text(y)
print("patched", tx, "(emergency trim while loading)")

# Launcher: "Reset settings to default" (keeps the Online server address)
sw = pathlib.Path("ios/Launcher.swift")
w = sw.read_text()
old = "    func save() throws {\n"
assert w.count(old) == 1
w = w.replace(old, """    func resetToDefaults() {
        network = "off"; prewarm = true; rumble = true; backgroundCompile = true; motion = false
        handheld = true; phonePads = false; showFps = true; mixAudio = true; frameskip = true
        textureQuality = "ultralow"; unlockedSave = false; asyncTextures = false; asyncGfx = false
        showMemory = true; fullPrewarm = true; autoFullPrewarm = false; quiet = false; precompiled = false
        gameVolume = 1.0
    }

""" + old)
old = """                    Button("Clear the shader cache", role: .destructive) { confirmClear = true }
"""
assert w.count(old) == 1
w = w.replace(old, old + """                    Button("Reset settings to default") { confirmReset = true }
""")
old = "    @State private var confirmClear = false\n"
assert w.count(old) == 1
w = w.replace(old, old + "    @State private var confirmReset = false\n")
old = """                .confirmationDialog("Clear the shader cache? The next start will be slow while everything compiles again.", isPresented: $confirmClear, titleVisibility: .visible) {
                    Button("Clear", role: .destructive) { toolMessage = clearShaderCache() }
                }
"""
assert w.count(old) == 1
w = w.replace(old, old + """                .confirmationDialog("Put every setting back to its default?", isPresented: $confirmReset, titleVisibility: .visible) {
                    Button("Reset", role: .destructive) { s.resetToDefaults(); toolMessage = "Settings reset to default." }
                }
""")
sw.write_text(w)
print("patched", sw, "(reset settings to default)")

# ---------------------------------------------------------------------------
# Race loading, more margin: the load spike (2.24 -> 2.98 GB in one second,
# ~200 MB of it upload buffers) killed the app even with the recommended
# settings. When less than 600 MB is left, at most 24 MB of texture upload
# buffers stay in flight (instead of 96 MB), and the emergency trim starts at
# 400 MB left instead of 300.
tx = pathlib.Path("crates/nxhost/src/nvn/draw/textures.rs")
y = tx.read_text()
old = """    const LIMIT: usize = 96 << 20;
    let mut q = STAGING.lock().unwrap();"""
assert y.count(old) == 1
y = y.replace(old, """    let left = gfx::available_memory();
    let LIMIT: usize = if left != 0 && left < 600 << 20 { 24 << 20 } else { 96 << 20 };
    let mut q = STAGING.lock().unwrap();""")
y = y.replace('eprintln!("[gfx] texture uploads: waited for the GPU (over 96 MB of upload buffers in flight)");',
              'eprintln!("[gfx] texture uploads: waited for the GPU (over {} MB of upload buffers in flight)", LIMIT >> 20);')
old = '.unwrap_or(300) << 20);\n        if N.fetch_add(1, Relaxed) % 8 != 0'
assert y.count(old) == 1
y = y.replace(old, '.unwrap_or(400) << 20);\n        if N.fetch_add(1, Relaxed) % 8 != 0')
tx.write_text(y)
print("patched", tx, "(smaller upload window and earlier emergency trim when memory is low)")

# ---------------------------------------------------------------------------
# Black objects at the start of every race: the prewarm did not keep the
# pipelines it found in the device archive (to save memory), so the race asked
# for them again, ~130 at once while it loads, and getting them back from the
# archive took 0.5 to 10 s each (132 "building took" lines, 0 archive misses).
# Now the prewarm keeps every pipeline it builds (NX_KEEP_PREWARMED=0 for the
# old way); the log says what it costs. If memory gets critical while a race
# loads, the emergency trim lets go of the ones not used yet.
st = pathlib.Path("crates/nxhost/src/nvn/draw/state.rs")
u = st.read_text()
old = """                                    if !from_archive {
                                        PREWARMED.lock().unwrap().insert(r.clone(), Shared(p));
                                    }
"""
assert u.count(old) == 1
u = u.replace(old, """                                    static KEEP: LazyLock<bool> = LazyLock::new(|| std::env::var_os("NX_KEEP_PREWARMED").is_none_or(|v| v != "0"));
                                    if *KEEP || !from_archive {
                                        PREWARMED.lock().unwrap().insert(r.clone(), Shared(p));
                                    }
""")
old = """            eprintln!("[gfx] prewarmed {}/{} recorded pipelines in {:.1} s ({} from the device archive)", built.into_inner(), list.len(), t0.elapsed().as_secs_f32(), PREWARM_HITS.load(Relaxed));
"""
assert u.count(old) == 1
u = u.replace(old, old + """            eprintln!("[gfx] prewarm: {} pipelines kept ready in memory, app now uses {} MB", PREWARMED.lock().unwrap().len(), gfx::footprint() >> 20);
""")
st.write_text(u)
tx = pathlib.Path("crates/nxhost/src/nvn/draw/textures.rs")
y = tx.read_text()
old = """        let frame = self.frame;
        let old: std::collections::HashSet<TexKey> = self
            .uploaded
            .keys()
            .filter(|k| self.last_used.get(*k).is_none_or(|&f| f + 1 < frame))"""
assert y.count(old) == 1
y = y.replace(old, """        super::state::drop_prewarmed();
""" + old)
tx.write_text(y)
print("patched", st, tx, "(keep prewarmed pipelines; emergency trim frees them)")

# The separate graphics thread is on by default (launcher switch to turn it off)
mn = pathlib.Path("crates/nxhost/src/main.rs")
m = mn.read_text()
old = '    if field("async_gfx").as_deref() == Some("true") {\n'
assert m.count(old) == 1
m = m.replace(old, '    if field("async_gfx").as_deref() != Some("false") {\n')
mn.write_text(m)
sw = pathlib.Path("ios/Launcher.swift")
w = sw.read_text()
for a_, b_ in [("    @Published var asyncGfx = false\n", "    @Published var asyncGfx = true\n"),
               ('        asyncGfx = json["async_gfx"] as? Bool ?? false\n', '        asyncGfx = json["async_gfx"] as? Bool ?? true\n'),
               ("asyncTextures = false; asyncGfx = false", "asyncTextures = false; asyncGfx = true")]:
    assert w.count(a_) == 1, a_
    w = w.replace(a_, b_)
sw.write_text(w)
print("patched", mn, sw, "(separate graphics thread on by default)")

# ---------------------------------------------------------------------------
# Memory report every ~10 s in the log, to see what fills the 3 GB:
# "[mem] textures: N loaded (X MB), M render targets (Y MB), P pipelines kept"
tx = pathlib.Path("crates/nxhost/src/nvn/draw/textures.rs")
y = tx.read_text()
old = "    pub fn trim(&mut self) {\n"
assert y.count(old) == 1
y = y.replace(old, old + """        if self.frame % 600 == 0 {
            let (mut n, mut bytes, mut rn, mut rbytes) = (0usize, 0usize, 0usize, 0usize);
            let mut seen = std::collections::HashSet::new();
            for (k, t) in &self.map {
                if !seen.insert(objc2::rc::Retained::as_ptr(&t.0) as usize) {
                    continue;
                }
                let size = objc2_metal::MTLResource::allocatedSize(&*t.0);
                if self.uploaded.contains_key(k) {
                    n += 1;
                    bytes += size;
                } else {
                    rn += 1;
                    rbytes += size;
                }
            }
            eprintln!("[mem] textures: {n} loaded ({} MB), {rn} render targets ({} MB), {} pipelines kept ready", bytes >> 20, rbytes >> 20, super::state::prewarmed_count());
        }
""")
tx.write_text(y)
st = pathlib.Path("crates/nxhost/src/nvn/draw/state.rs")
u = st.read_text()
old = "pub fn drop_prewarmed() {\n"
assert u.count(old) == 1
u = u.replace(old, "pub fn prewarmed_count() -> usize {\n    PREWARMED.lock().unwrap().len()\n}\n\n" + old)
st.write_text(u)
print("patched", tx, st, "(memory report)")

# ---------------------------------------------------------------------------
# Where does the menu's ~2.1 GB go? Log the app's memory before/after the
# pipeline archives are opened and when the prewarm starts.
st = pathlib.Path("crates/nxhost/src/nvn/draw/state.rs")
u = st.read_text()
old = "    let archives: Vec<Retained<Archive>> = paths.iter().filter_map(|p| open_archive(Some(p))).collect();\n"
assert u.count(old) == 1
u = u.replace(old, """    let before = gfx::footprint() >> 20;
""" + old + """    eprintln!("[mem] pipeline archives: {} files, app {} MB -> {} MB", archives.len(), before, gfx::footprint() >> 20);
""")
old = """            let t0 = std::time::Instant::now();
            let list = read_list();
"""
assert u.count(old) == 1
u = u.replace(old, old + """            eprintln!("[mem] prewarm starts: app uses {} MB", gfx::footprint() >> 20);
""")
st.write_text(u)
print("patched", st, "(memory at archive load and prewarm start)")

# ---------------------------------------------------------------------------
# Detailed memory every 10 s, to know what fills the ~3 GB:
# "[mem] detail: game RAM X MB, Metal Y MB, resident Z, compressed C, ..."
#  - game RAM: pages of the Switch address space really in memory (mincore)
#  - Metal: everything the GPU driver allocated (textures, buffers, pipelines)
#  - task_vm_info: resident / internal / external / compressed / purgeable / footprint
gm = pathlib.Path("crates/nxhost/src/gfx/mod.rs")
g = gm.read_text()
old = """            eprintln!("[mem] {:.0} s: app uses {} MB, iOS still allows {} MB (lowest so far {} MB)", t0.elapsed().as_secs_f32(), used >> 20, left >> 20, low >> 20);
"""
assert g.count(old) == 1
g = g.replace(old, old + """            mem_detail();
""")
old = "/// Every 10 s: memory used by the app and memory iOS still allows (NX_NO_MEMLOG=1 to turn off).\n"
assert g.count(old) == 1
g = g.replace(old, r'''/// Where the memory goes (see start_memlog).
pub fn mem_detail() {
    unsafe extern "C" {
        static mach_task_self_: u32;
        fn task_info(task: u32, flavor: i32, info: *mut u64, count: *mut u32) -> i32;
    }
    const TASK_VM_INFO: i32 = 22;
    let mut vm = [0u64; 64];
    let mut count = (vm.len() * 2) as u32;
    let ok = unsafe { task_info(mach_task_self_, TASK_VM_INFO, vm.as_mut_ptr(), &mut count) } == 0;
    // guest (Switch) address space pages that are really in memory
    let game = crate::kernel::KERNEL.aspace.get().map_or(0u64, |a| {
        let len = a.layout.total() as usize;
        let page = 16384usize;
        let mut v = vec![0u8; len / page + 1];
        let r = unsafe { libc::mincore(a.base as *mut libc::c_void, len, v.as_mut_ptr() as *mut _) };
        if r == 0 { v.iter().filter(|&&b| b & 1 != 0).count() as u64 * page as u64 } else { 0 }
    });
    let metal = GPU.get().map_or(0, |g| g.device.0.currentAllocatedSize() as u64);
    let mb = |b: u64| b >> 20;
    if ok {
        // task_vm_info: [2] resident, [6] internal, [8] external, [10] reusable,
        // [13] purgeable volatile resident, [15] compressed, [18] phys_footprint
        eprintln!(
            "[mem] detail: game RAM {} MB, Metal {} MB | resident {} MB, internal {} MB, external {} MB, compressed {} MB, purgeable {} MB, reusable {} MB, footprint {} MB",
            mb(game), mb(metal), mb(vm[2]), mb(vm[6]), mb(vm[8]), mb(vm[15]), mb(vm[13]), mb(vm[10]), mb(vm[18])
        );
    } else {
        eprintln!("[mem] detail: game RAM {} MB, Metal {} MB", mb(game), mb(metal));
    }
}

''' + old)
gm.write_text(g)
print("patched", gm, "(detailed memory report)")

# Memory at boot stages: after the game modules are loaded
mn = pathlib.Path("crates/nxhost/src/main.rs")
m = mn.read_text()
old = '    let mods = loader::load(&dir, &["main", "subsdk0", "sdk"], &hle::table())?;\n'
assert m.count(old) == 1
m = m.replace(old, old + '    eprintln!("[mem] game modules loaded: app uses {} MB", gfx::footprint() >> 20);\n    gfx::mem_detail();\n')
mn.write_text(m)
print("patched", mn, "(memory after loading the game modules)")

# ---------------------------------------------------------------------------
# Thermal state and Low Power Mode in the memory detail line (FPS drops when
# the phone throttles), and a "Share the pipeline list" button in Tools.
gm = pathlib.Path("crates/nxhost/src/gfx/mod.rs")
g = gm.read_text()
old = '''    let metal = GPU.get().map_or(0, |g| g.device.0.currentAllocatedSize() as u64);
'''
assert g.count(old) == 1
g = g.replace(old, old + '''    let pi = objc2_foundation::NSProcessInfo::processInfo();
    let heat = ["normal", "fair", "serious (slowed down)", "critical (slowed down)"].get(pi.thermalState().0 as usize).copied().unwrap_or("?");
    #[cfg(target_os = "ios")]
    let low_power = pi.isLowPowerModeEnabled();
    #[cfg(not(target_os = "ios"))]
    let low_power = false;
    eprintln!("[mem] phone: heat {heat}{}", if low_power { ", LOW POWER MODE ON (slower CPU)" } else { "" });
''')
gm.write_text(g)
sw = pathlib.Path("ios/Launcher.swift")
w = sw.read_text()
old = '''                    ShareLink(item: docsURL.appendingPathComponent("work/nxhost.prev.log")) {
                        Label("Share the previous log", systemImage: "square.and.arrow.up")
                    }
'''
assert w.count(old) == 1
w = w.replace(old, old + '''                    ShareLink(item: docsURL.appendingPathComponent("work/pipelines.tsv")) {
                        Label("Share the pipeline list", systemImage: "square.and.arrow.up")
                    }
''')
sw.write_text(w)
print("patched", gm, sw, "(heat / low power in the log, share pipeline list)")

# ---------------------------------------------------------------------------
# Purgeable textures (NX_PURGEABLE=0 to turn off): a decoded texture unused for
# ~2 s (120 frames) is marked "volatile". iOS does not count volatile memory
# against the app's limit and may take it back when memory runs short, instead
# of killing the app. When the game uses the texture again it is made
# non-volatile; if iOS took it, it is decoded again from the game's memory.
# Only textures with their own storage (not views, not textures over the game's
# memory, not render targets) are concerned.
tx = pathlib.Path("crates/nxhost/src/nvn/draw/textures.rs")
y = tx.read_text()
old = "#[derive(Default)]\npub struct Textures {\n"
assert y.count(old) == 1
y = y.replace(old, """static VOLATILE: std::sync::Mutex<Option<std::collections::HashSet<TexKey>>> = std::sync::Mutex::new(None);
static PURGED: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);

fn purgeable_on() -> bool {
    static ON: std::sync::LazyLock<bool> = std::sync::LazyLock::new(|| std::env::var_os("NX_PURGEABLE").is_none_or(|v| v != "0"));
    *ON
}

""" + old)
old = """        self.last_used.retain(|k, _| !gone(k));
    }
"""
assert y.count(old) == 1
y = y.replace(old, """        self.last_used.retain(|k, _| !gone(k));
        if let Some(v) = VOLATILE.lock().unwrap().as_mut() {
            v.retain(|k| !gone(k));
        }
    }

    /// Mark decoded textures unused for ~2 s as purgeable (see purgeable_on).
    fn make_volatile(&mut self) {
        if !purgeable_on() {
            return;
        }
        let frame = self.frame;
        let mut vol = VOLATILE.lock().unwrap();
        let vol = vol.get_or_insert_with(Default::default);
        let mut n = 0;
        for k in self.uploaded.keys() {
            if vol.contains(k) || self.last_used.get(k).is_none_or(|&f| f + 120 > frame) || self.aliases.contains_key(k) {
                continue;
            }
            let Some(t) = self.map.get(k) else { continue };
            #[allow(unused_unsafe)]
            let own = unsafe { t.0.buffer().is_none() && t.0.parentTexture().is_none() };
            if !own || self.views.keys().any(|(vk, _, _)| vk == k) {
                continue;
            }
            objc2_metal::MTLResource::setPurgeableState(&*t.0, MTLPurgeableState::Volatile);
            vol.insert(*k);
            n += 1;
        }
        let _ = n;
    }
""")
old = """        if let Some(t) = self.map.get(&key) {
            let t = t.clone();
            let frame = self.frame;
            self.last_used.insert(key, frame);
"""
assert y.count(old) == 1
y = y.replace(old, """        if purgeable_on() && VOLATILE.lock().unwrap().as_mut().is_some_and(|v| v.remove(&key)) {
            if let Some(t) = self.map.get(&key) {
                let was = objc2_metal::MTLResource::setPurgeableState(&*t.0, MTLPurgeableState::NonVolatile);
                if was == MTLPurgeableState::Empty {
                    // iOS took it back: decode it again below
                    self.evict(&|k| *k == key);
                    PURGED.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
                }
            }
        }
""" + old)
old = """            eprintln!("[mem] textures: {n} loaded ({} MB), {rn} render targets ({} MB), {} pipelines kept ready", bytes >> 20, rbytes >> 20, super::state::prewarmed_count());
"""
assert y.count(old) == 1
y = y.replace(old, """            eprintln!("[mem] textures: {n} loaded ({} MB), {rn} render targets ({} MB), {} pipelines kept ready, {} purgeable, {} taken back by iOS", bytes >> 20, rbytes >> 20, super::state::prewarmed_count(), VOLATILE.lock().unwrap().as_ref().map_or(0, |v| v.len()), PURGED.load(std::sync::atomic::Ordering::Relaxed));
""")
old = "    pub fn trim(&mut self) {\n"
assert y.count(old) == 1
y = y.replace(old, old + "        self.make_volatile();\n")
tx.write_text(y)
print("patched", tx, "(purgeable textures)")
# a new texture over the same memory reuses an existing one (views, block
# copies): make every purgeable texture at that address safe first
tx = pathlib.Path("crates/nxhost/src/nvn/draw/textures.rs")
y = tx.read_text()
old = "        let base_key = TexKey { format: 0, ..key };\n"
assert y.count(old) == 1
y = y.replace(old, "        self.restore_addr(key.addr);\n" + old)
old = "    /// Mark decoded textures unused for ~2 s as purgeable (see purgeable_on).\n"
assert y.count(old) == 1
y = y.replace(old, """    /// Make the purgeable textures over `addr` normal again; if iOS took one back,
    /// forget everything at that address so it is all created again.
    fn restore_addr(&mut self, addr: u64) {
        if !purgeable_on() {
            return;
        }
        let keys: Vec<TexKey> = VOLATILE.lock().unwrap().as_mut().map_or(Vec::new(), |v| {
            let ks: Vec<TexKey> = v.iter().filter(|k| k.addr == addr).copied().collect();
            for k in &ks {
                v.remove(k);
            }
            ks
        });
        let mut purged = false;
        for k in keys {
            if let Some(t) = self.map.get(&k) {
                purged |= objc2_metal::MTLResource::setPurgeableState(&*t.0, MTLPurgeableState::NonVolatile) == MTLPurgeableState::Empty;
            }
        }
        if purged {
            self.evict(&|k| k.addr == addr);
            PURGED.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
        }
    }

""" + old)
tx.write_text(y)
print("patched", tx, "(purgeable textures: shared storage)")

# ---------------------------------------------------------------------------
# NX_AUTOINPUT: SLEFT / SRIGHT push the left stick (steering), for the Mac runs.
ip = pathlib.Path("crates/nxhost/src/input.rs")
s_ = ip.read_text()
old = '("LEFT", LEFT), ("UP", UP), ("RIGHT", RIGHT), ("DOWN", DOWN),'
assert s_.count(old) == 1
s_ = s_.replace(old, old + ' ("SLEFT", 1 << 40), ("SRIGHT", 1 << 41),')
old = "                let mut acc = Acc { buttons: scripted(i), ..Default::default() };\n"
assert s_.count(old) == 1
s_ = s_.replace(old, """                let script = scripted(i);
                let mut acc = Acc { buttons: script & 0xFFFF_FFFF, ..Default::default() };
                if script & 1 << 40 != 0 {
                    acc.lx = -1.0;
                }
                if script & 1 << 41 != 0 {
                    acc.lx = 1.0;
                }
""")
ip.write_text(s_)
print("patched", ip, "(scripted stick)")

# How much do the kept pipelines weigh? footprint before/after freeing them
st = pathlib.Path("crates/nxhost/src/nvn/draw/state.rs")
u = st.read_text()
old = """pub fn drop_prewarmed() {
    let n = {"""
assert u.count(old) == 1
u = u.replace(old, """pub fn drop_prewarmed() {
    let before = gfx::footprint() >> 20;
    let n = {""")
old = 'eprintln!("[mem] low memory: freed {n} unused prewarmed pipelines");'
assert u.count(old) == 1
u = u.replace(old, 'eprintln!("[mem] low memory: freed {n} unused prewarmed pipelines (app {} MB -> {} MB)", before, gfx::footprint() >> 20);')
st.write_text(u)
print("patched", st, "(pipeline memory measure)")

# NX_AUTOINPUT=@file reads the script from a file (long scripts are over the
# environment size limit), and the lookup is a binary search (a 6 h script has
# ~90 000 entries; the old loop went through all of them at every input poll).
ip = pathlib.Path("crates/nxhost/src/input.rs")
s_ = ip.read_text()
old = '''        std::env::var("NX_AUTOINPUT").unwrap_or_default().split(',').filter_map(|e| {'''
assert s_.count(old) == 1
s_ = s_.replace(old, '''        let raw = std::env::var("NX_AUTOINPUT").unwrap_or_default();
        let raw = match raw.strip_prefix('@') {
            Some(path) => std::fs::read_to_string(path.trim()).unwrap_or_default(),
            None => raw,
        };
        let mut v: Vec<(f32, u64, usize)> = raw.split(',').filter_map(|e| {''')
old = '''            Some((t.parse().ok()?, names.iter().find(|(n, _)| n.eq_ignore_ascii_case(b))?.1, player))
        }).collect()
    });'''
assert s_.count(old) == 1
s_ = s_.replace(old, '''            Some((t.parse().ok()?, names.iter().find(|(n, _)| n.eq_ignore_ascii_case(b))?.1, player))
        }).collect();
        v.sort_by(|a, b| a.0.total_cmp(&b.0));
        v
    });''')
old = '''    SCRIPT.iter().filter(|(at, _, p)| *p == player + 1 && t >= *at && t < at + 0.15).fold(0, |m, (_, b, _)| m | b)'''
assert s_.count(old) == 1
s_ = s_.replace(old, '''    let lo = SCRIPT.partition_point(|(at, _, _)| *at <= t - 0.15);
    SCRIPT[lo..].iter().take_while(|(at, _, _)| *at <= t).filter(|(at, _, p)| *p == player + 1 && t < at + 0.15).fold(0, |m, (_, b, _)| m | b)''')
ip.write_text(s_)
print("patched", ip, "(script from a file, binary search)")

# ---------------------------------------------------------------------------
# A render pass that is dropped while still open (an early exit somewhere in
# the replay) made Metal abort the whole app ("Command encoder released
# without endEncoding", seen on a macOS 14 Mac). End it on drop instead.
dm = pathlib.Path("crates/nxhost/src/nvn/draw/mod.rs")
s_ = dm.read_text()
for a_, b_ in [("    fixed: Option<EncState>,\n}\n", "    fixed: Option<EncState>,\n    ended: bool,\n}\n\nimpl Drop for Pass {\n    fn drop(&mut self) {\n        if !self.ended {\n            self.enc.endEncoding();\n        }\n    }\n}\n"),
               ("fresh, keys, fixed: None });", "fresh, keys, fixed: None, ended: false });"),
               ("        if let Some(p) = self.pass.take() {\n            p.enc.endEncoding();\n", "        if let Some(mut p) = self.pass.take() {\n            p.enc.endEncoding();\n            p.ended = true;\n")]:
    assert s_.count(a_) == 1, a_
    s_ = s_.replace(a_, b_)
dm.write_text(s_)
print("patched", dm, "(open render pass ended on drop)")

# ---------------------------------------------------------------------------
# Memory report: how much the BC textures cost once decoded on the CPU (iPhone
# GPUs have no BC support, so they are stored 4-8x bigger than on the Switch).
tx = pathlib.Path("crates/nxhost/src/nvn/draw/textures.rs")
y = tx.read_text()
old = """                let size = objc2_metal::MTLResource::allocatedSize(&*t.0);
                if self.uploaded.contains_key(k) {"""
assert y.count(old) == 1
y = y.replace(old, """                let size = objc2_metal::MTLResource::allocatedSize(&*t.0);
                if gfx::bc::soft(k.format).is_some() {
                    bc_n += 1;
                    bc_bytes += size;
                    // compressed size on the Switch: BC1/BC4 half a byte per pixel, the others one
                    let (dec, comp) = match k.format { 65 | 66 | 69 | 70 => if gfx::bc::astc_bc1() { (2, 1) } else { (8, 1) }, 73 | 74 => (2, 1), 75 | 76 => (2, 1), 79 | 80 => (8, 1), _ => (4, 1) };
                    bc_native += size * comp / dec;
                    bc_by[(k.format - 65) as usize] += size;
                }
                if self.uploaded.contains_key(k) {""")
old = "            let (mut n, mut bytes, mut rn, mut rbytes) = (0usize, 0usize, 0usize, 0usize);\n"
assert y.count(old) == 1
y = y.replace(old, old + "            let (mut bc_n, mut bc_bytes, mut bc_native) = (0usize, 0usize, 0usize);\n            let mut bc_by = [0usize; 16];\n")
old = "            eprintln!(\"[mem] textures: {n} loaded"
assert y.count(old) == 1
y = y.replace(old, """            if bc_n > 0 {
                eprintln!("[mem] BC textures decoded on the CPU: {bc_n}, {} MB (would be {} MB compressed)", bc_bytes >> 20, bc_native >> 20);
                let names = ["BC1", "BC1", "BC2", "BC3", "BC1 srgb", "BC1 srgb", "BC2 srgb", "BC3 srgb", "BC4", "BC4s", "BC5", "BC5s", "BC7", "BC7 srgb", "BC6s", "BC6"];
                let parts: Vec<String> = (0..16).filter(|&i| bc_by[i] >> 20 > 0).map(|i| format!("{} {} MB", names[i], bc_by[i] >> 20)).collect();
                eprintln!("[mem] BC by format (fmt 65+i): {}", parts.join(", "));
            }
""" + old)
tx.write_text(y)
print("patched", tx, "(BC texture cost in the memory report)")

# ---------------------------------------------------------------------------
# iPhone GPUs cannot read BC textures, so they were decoded to RGBA: a BC1
# texture took 8x its Switch size (most of the RAM jump when a race loads).
# BC1 is now rewritten block by block as ASTC 4x4, which iPhones read natively:
# 1 byte per pixel instead of 4, same colors (within 2/255; blocks mixing
# transparent pixels with several colors get their colors averaged).
# NX_BC1_ASTC=0 goes back to RGBA.
bcp = pathlib.Path("crates/nxhost/src/gfx/bc.rs")
s_ = bcp.read_text()
old = "pub fn format(nvn: u32) -> Option<MTLPixelFormat> {\n"
assert s_.count(old) == 1
s_ = s_.replace(old, r"""pub fn astc_bc1() -> bool {
    static S: LazyLock<bool> = LazyLock::new(|| {
        let on = enabled()
            && std::env::var("NX_BC1_ASTC").map_or(true, |v| v.trim() != "0")
            && sixteen() == 0
            // the mip-drop mode that enlarges a smaller level works on RGBA pixels; the
            // shrink mode (launcher default) just uploads fewer levels and is fine
            && (std::env::var("NX_TEX_DROP").map_or(true, |v| v.trim() == "0") || std::env::var_os("NX_TEX_DROP_SHRINK").is_some())
            && super::gpu().device.0.supportsFamily(objc2_metal::MTLGPUFamily::Apple2);
        if on {
            eprintln!("[gfx] BC1 textures stored as ASTC 4x4 (4x less memory than decoded)");
        }
        on
    });
    *S
}

/// Rows of the decoded data for an image `h` pixels high (block rows for ASTC).
pub fn out_rows(k: Kind, h: usize) -> usize {
    if k == Kind::Bc1 && astc_bc1() { h.div_ceil(4) } else { h }
}

fn bc1_to_astc(src: &[u8], row_bytes: usize, w: usize, h: usize, images: usize) -> (Vec<u8>, usize) {
    static TRITS: LazyLock<[u8; 243]> = LazyLock::new(trit_table);
    let (bw, bh) = (w.div_ceil(4), h.div_ceil(4));
    let mut out = vec![0u8; bw * bh * 16 * images];
    for img in 0..images {
        for by in 0..bh {
            for bx in 0..bw {
                let o = (img * bh + by) * row_bytes + bx * 8;
                let Some(b) = src.get(o..o + 8) else { continue };
                let d = ((img * bh + by) * bw + bx) * 16;
                out[d..d + 16].copy_from_slice(&bc1_block_astc(b, &TRITS));
            }
        }
    }
    (out, bw * 16)
}

""" + 'fn trit_table() -> [u8; 243] {\n    let mut t = [0u8; 243];\n    let mut seen = [false; 243];\n    for b in 0..256u32 {\n        let bit = |i: u32| (b >> i) & 1;\n        let bits = |hi: u32, lo: u32| (b >> lo) & ((1 << (hi - lo + 1)) - 1);\n        let (c, t4, t3);\n        if bits(4, 2) == 7 {\n            c = (bits(7, 5) << 2) | bits(1, 0);\n            t4 = 2;\n            t3 = 2;\n        } else {\n            c = bits(4, 0);\n            if bits(6, 5) == 3 {\n                t4 = 2;\n                t3 = bit(7);\n            } else {\n                t4 = bit(7);\n                t3 = bits(6, 5);\n            }\n        }\n        let cb = |i: u32| (c >> i) & 1;\n        let (t2, t1, t0);\n        if c & 3 == 3 {\n            t2 = 2;\n            t1 = cb(4);\n            t0 = (cb(3) << 1) | (cb(2) & !cb(3) & 1);\n        } else if (c >> 2) & 3 == 3 {\n            t2 = 2;\n            t1 = 2;\n            t0 = c & 3;\n        } else {\n            t2 = cb(4);\n            t1 = (c >> 2) & 3;\n            t0 = (cb(1) << 1) | (cb(0) & !cb(1) & 1);\n        }\n        let i = (t0 + 3 * t1 + 9 * t2 + 27 * t3 + 81 * t4) as usize;\n        if !seen[i] {\n            seen[i] = true;\n            t[i] = b as u8;\n        }\n    }\n    t\n}\n\n/// One BC1 block as one ASTC 4x4 block: the same colors (within 2/255), except\n/// blocks mixing transparent pixels with several colors (their colors are averaged).\npub fn bc1_block_astc(b: &[u8], trits: &[u8; 243]) -> [u8; 16] {\n    let c0 = u16::from_le_bytes([b[0], b[1]]);\n    let c1 = u16::from_le_bytes([b[2], b[3]]);\n    let idx = u32::from_le_bytes([b[4], b[5], b[6], b[7]]);\n    let x = |c: u16| -> [u32; 3] {\n        let (r, g, bl) = ((c >> 11) as u32 & 31, (c >> 5) as u32 & 63, c as u32 & 31);\n        [r << 3 | r >> 2, g << 2 | g >> 4, bl << 3 | bl >> 2]\n    };\n    let (mut e0, mut e1) = (x(c0), x(c1));\n    let ix = |i: usize| (idx >> (2 * i)) & 3;\n    let mut bits: u128 = 0;\n    let mut ws: u128 = 0;\n    let put = |bits: &mut u128, pos: u32, v: u32, n: u32| *bits |= ((v & ((1u32 << n) - 1)) as u128) << pos;\n    let transparent = c0 <= c1 && (0..16).any(|i| ix(i) == 3);\n    if transparent {\n        // RGBA: transparent black to the (average) opaque color\n        let pal = [e0, e1, [(e0[0] + e1[0]) / 2, (e0[1] + e1[1]) / 2, (e0[2] + e1[2]) / 2]];\n        let (mut sum, mut n) = ([0u32; 3], 0u32);\n        for i in 0..16 {\n            let k = ix(i) as usize;\n            if k != 3 {\n                for c in 0..3 {\n                    sum[c] += pal[k][c];\n                }\n                n += 1;\n            }\n        }\n        let m = if n == 0 { [0; 3] } else { sum.map(|s| (s + n / 2) / n) };\n        put(&mut bits, 0, 66, 11);\n        put(&mut bits, 13, 12, 4);\n        for (i, v) in [0, m[0], 0, m[1], 0, m[2], 0, 255].into_iter().enumerate() {\n            put(&mut bits, 17 + 8 * i as u32, v, 8);\n        }\n        for i in 0..16 {\n            put(&mut ws, 2 * i as u32, if ix(i) == 3 { 0 } else { 3 }, 2);\n        }\n        return (bits | ws.reverse_bits()).to_le_bytes();\n    }\n    let four = c0 > c1;\n    let mut w = [0u32; 16];\n    for (i, w) in w.iter_mut().enumerate() {\n        *w = if four { [0, 3, 1, 2][ix(i) as usize] } else { [0, 2, 1, 0][ix(i) as usize] };\n    }\n    let top = if four { 3 } else { 2 };\n    if e1.iter().sum::<u32>() < e0.iter().sum::<u32>() {\n        std::mem::swap(&mut e0, &mut e1);\n        w = w.map(|v| top - v);\n    }\n    put(&mut bits, 0, if four { 66 } else { 81 }, 11);\n    put(&mut bits, 13, 8, 4);\n    for (i, v) in [e0[0], e1[0], e0[1], e1[1], e0[2], e1[2]].into_iter().enumerate() {\n        put(&mut bits, 17 + 8 * i as u32, v, 8);\n    }\n    if four {\n        for i in 0..16 {\n            put(&mut ws, 2 * i as u32, w[i], 2);\n        }\n    } else {\n        for g in 0..4 {\n            let t = |k: usize| w.get(g * 5 + k).copied().unwrap_or(0) as usize;\n            let v = trits[t(0) + 3 * t(1) + 9 * t(2) + 27 * t(3) + 81 * t(4)] as u32;\n            put(&mut ws, 8 * g as u32, v, 8);\n        }\n        ws &= (1u128 << 26) - 1;\n    }\n    (bits | ws.reverse_bits()).to_le_bytes()\n}\n' + "\n" + old)
old = "        _ if srgb => P::RGBA8Unorm_sRGB,\n"
assert s_.count(old) == 1
s_ = s_.replace(old, "        Kind::Bc1 if astc_bc1() => if srgb { P::ASTC_4x4_sRGB } else { P::ASTC_4x4_LDR },\n" + old)
old = "    let px = pixel_bytes(k);\n    let mut out = vec![0u8; w * h * px * images];\n"
assert s_.count(old) == 1
s_ = s_.replace(old, "    if k == Kind::Bc1 && astc_bc1() {\n        return bc1_to_astc(src, row_bytes, w, h, images);\n    }\n" + old)
bcp.write_text(s_)
tx = pathlib.Path("crates/nxhost/src/nvn/draw/textures.rs")
y = tx.read_text()
old = "        (p.offset, p.row, p.image) = (offset, bpr, bpr * h as usize);\n"
assert y.count(old) == 1
y = y.replace(old, "        (p.offset, p.row, p.image) = (offset, bpr, bpr * gfx::bc::out_rows(k, h as usize));\n")
tx.write_text(y)
old = "    h ^= (gfx::bc::sixteen() as u64) << 60;\n"
assert y.count(old) == 1
y = y.replace(old, old + "    h ^= (gfx::bc::astc_bc1() as u64) << 59; // cached RGBA and ASTC data differ\n")
tx.write_text(y)
dm = pathlib.Path("crates/nxhost/src/nvn/draw/mod.rs")
s_ = dm.read_text()
old = "            Some(px) => (px.as_ptr(), px.len() / d.max(1) / h, px.len() / d.max(1)),\n"
assert s_.count(old) == 1
s_ = s_.replace(old, "            Some(px) => (px.as_ptr(), px.len() / d.max(1) / gfx::bc::soft(key.format).map_or(h, |k| gfx::bc::out_rows(k, h)), px.len() / d.max(1)),\n")
dm.write_text(s_)
print("patched", bcp, "(BC1 stored as ASTC 4x4)")

# ---------------------------------------------------------------------------
# Memory report, part 2: the textures the main list doesn't show (views,
# aliases, textures dropped from the list but still held elsewhere), the
# scratch buffers for draw data, the block-copy buffer and the texture upload
# buffers in flight. Metal grew by ~430 MB when a race loaded on the iPhone
# while the main list only showed ~110 MB: this says where the rest is.
gm = pathlib.Path("crates/nxhost/src/gfx/mod.rs")
s_ = gm.read_text()
old = "/// Where the memory goes (see start_memlog).\n"
assert s_.count(old) == 1
s_ = s_.replace(old, """/// Bytes held by our own Metal objects: [draw scratch buffers, block-copy buffer,
/// upload buffers in flight, every texture held, textures outside the main list].
pub static OURS: [std::sync::atomic::AtomicU64; 5] = [const { std::sync::atomic::AtomicU64::new(0) }; 5];

""" + old)
old = """    } else {
        eprintln!("[mem] detail: game RAM {} MB, Metal {} MB", mb(game), mb(metal));
    }
"""
assert s_.count(old) == 1
s_ = s_.replace(old, old + """    let o = |i: usize| OURS[i].load(std::sync::atomic::Ordering::Relaxed) >> 20;
    let heap = heap().map_or(0, |h| h.buffer.0.length() as u64);
    eprintln!(
        "[mem] Metal besides the game RAM: {} MB = textures {} MB (of which {} MB outside the main list), draw scratch {} MB, block copies {} MB, uploads in flight {} MB, other {} MB",
        mb(metal.saturating_sub(heap)), o(3), o(4), o(0), o(1), o(2),
        mb(metal.saturating_sub(heap)).saturating_sub(o(3) + o(0) + o(1) + o(2))
    );
""")
gm.write_text(s_)
st = pathlib.Path("crates/nxhost/src/nvn/draw/state.rs")
s_ = st.read_text()
old = "                Shared(dev.newBufferWithLength_options(size.max(CHUNK), MTLResourceOptions::StorageModeShared).expect(\"scratch buffer\"))\n"
assert s_.count(old) == 1
s_ = s_.replace(old, "                gfx::OURS[0].fetch_add(size.max(CHUNK) as u64, std::sync::atomic::Ordering::Relaxed);\n" + old)
st.write_text(s_)
tx = pathlib.Path("crates/nxhost/src/nvn/draw/textures.rs")
y = tx.read_text()
old = "        *scratch = Some(Shared(b));\n"
assert y.count(old) == 1
y = y.replace(old, "        gfx::OURS[1].store(b.length() as u64, std::sync::atomic::Ordering::Relaxed);\n" + old)
old = "    let mut total: usize = q.iter().map(|(_, b)| *b).sum();\n"
assert y.count(old) == 1
y = y.replace(old, old + "    gfx::OURS[2].store(total as u64, std::sync::atomic::Ordering::Relaxed);\n")
old = "            eprintln!(\"[mem] textures: {n} loaded"
assert y.count(old) == 1
y = y.replace(old, """            {
                // every texture still held anywhere, counted once (views count as their parent)
                fn root(t: &Texture) -> objc2::rc::Retained<objc2::runtime::ProtocolObject<dyn MTLTexture>> {
                    let mut r: objc2::rc::Retained<objc2::runtime::ProtocolObject<dyn MTLTexture>> = unsafe { objc2::rc::Retained::retain(t as *const _ as *mut _) }.unwrap();
                    while let Some(p) = r.parentTexture() {
                        r = p;
                    }
                    r
                }
                let mut all = std::collections::HashMap::new();
                let main: std::collections::HashSet<usize> = self.map.values().map(|t| objc2::rc::Retained::as_ptr(&root(&t.0)) as usize).collect();
                let held = self.map.values().map(|t| &t.0).chain(self.storage.values().map(|(_, t)| &t.0)).chain(self.views.values().map(|t| &t.0)).chain(self.block_views.iter().map(|(_, t)| &t.0));
                for t in held {
                    let r = root(t);
                    all.entry(objc2::rc::Retained::as_ptr(&r) as usize).or_insert_with(|| objc2_metal::MTLResource::allocatedSize(&*r));
                }
                let total: usize = all.values().sum();
                let outside: usize = all.iter().filter(|(p, _)| !main.contains(p)).map(|(_, s)| *s).sum();
                gfx::OURS[3].store(total as u64, std::sync::atomic::Ordering::Relaxed);
                gfx::OURS[4].store(outside as u64, std::sync::atomic::Ordering::Relaxed);
            }
""" + old)
tx.write_text(y)
print("patched", gm, "(memory report: where Metal memory goes)")

# ---------------------------------------------------------------------------
# The emergency trim no longer throws away the prewarmed pipelines: on the
# iPhone it freed nothing measurable ("app 2273 MB -> 2273 MB") but every
# pipeline of the race then had to be rebuilt from the archive, 20 to 25 s
# each under memory pressure: objects stayed black until the 3rd lap.
# Only when iOS is about to kill the app (less than 150 MB left) are they freed.
tx = pathlib.Path("crates/nxhost/src/nvn/draw/textures.rs")
y = tx.read_text()
old = """        super::state::drop_prewarmed();
        let frame = self.frame;
"""
assert y.count(old) == 1
y = y.replace(old, """        if left < 150 << 20 {
            super::state::drop_prewarmed();
        }
        let frame = self.frame;
""")
tx.write_text(y)
print("patched", tx, "(emergency trim keeps the prewarmed pipelines)")

# ---------------------------------------------------------------------------
# Purgeable textures, part 2: every texture decoded from the game's data (BC,
# ASTC...) that hasn't been drawn for ~2 s is marked purgeable, not only the
# few the game can rewrite. iOS doesn't count purgeable memory against the app
# and takes it back first when memory runs out, instead of killing the app;
# the texture is decoded again if it's needed after that. Textures the game
# ever renders into are never marked (their content can't be rebuilt).
tx = pathlib.Path("crates/nxhost/src/nvn/draw/textures.rs")
y = tx.read_text()
old = "fn purgeable_on() -> bool {\n"
assert y.count(old) == 1
y = y.replace(old, """/// Textures decoded from the game's data, and textures the game rendered into.
static SAMPLED: std::sync::Mutex<Option<std::collections::HashSet<TexKey>>> = std::sync::Mutex::new(None);
static RT_USED: std::sync::Mutex<Option<std::collections::HashSet<TexKey>>> = std::sync::Mutex::new(None);

""" + old)
old = """        if !render_target && !depth_format {
            upload(b, &key, &tex.0, levels);
        }
"""
assert y.count(old) == 1
y = y.replace(old, """        if !render_target && !depth_format {
            upload(b, &key, &tex.0, levels);
            SAMPLED.lock().unwrap().get_or_insert_with(Default::default).insert(key);
        }
""")
old = """        if purgeable_on() && VOLATILE.lock().unwrap().as_mut().is_some_and(|v| v.remove(&key)) {"""
assert y.count(old) == 1
y = y.replace(old, """        if render_target && purgeable_on() {
            RT_USED.lock().unwrap().get_or_insert_with(Default::default).insert(key);
        }
""" + old)
old = """        let _ = n;
    }
"""
assert y.count(old) == 1
y = y.replace(old, """        // off by default: on the iPhone, iOS took back 1458 textures in one race and
        // decoding them again cost CPU (lower FPS) and memory (old copies still held)
        static ALL: std::sync::LazyLock<bool> = std::sync::LazyLock::new(|| std::env::var_os("NX_PURGEABLE_ALL").is_some_and(|v| v != "0"));
        let sampled = SAMPLED.lock().unwrap();
        let rt = RT_USED.lock().unwrap();
        if let Some(sampled) = sampled.as_ref().filter(|_| *ALL) {
            for k in sampled {
                if vol.contains(k) || rt.as_ref().is_some_and(|r| r.contains(k)) || self.aliases.contains_key(k) {
                    continue;
                }
                if self.last_used.get(k).is_none_or(|&f| f + 120 > frame) {
                    continue;
                }
                let Some(t) = self.map.get(k) else { continue };
                #[allow(unused_unsafe)]
                let own = unsafe { t.0.buffer().is_none() && t.0.parentTexture().is_none() };
                if !own {
                    continue;
                }
                objc2_metal::MTLResource::setPurgeableState(&*t.0, MTLPurgeableState::Volatile);
                vol.insert(*k);
                n += 1;
            }
        }
        let _ = n;
    }
""")
old = """    pub fn evict(&mut self, gone: &dyn Fn(&TexKey) -> bool) {
"""
assert y.count(old) == 1
y = y.replace(old, old + """        if let Some(s) = SAMPLED.lock().unwrap().as_mut() {
            s.retain(|k| !gone(k));
        }
        if let Some(s) = RT_USED.lock().unwrap().as_mut() {
            s.retain(|k| !gone(k));
        }
""")
tx.write_text(y)
print("patched", tx, "(every decoded texture can be purgeable)")

# ---------------------------------------------------------------------------
# Draw data scratch buffers: 4 MB pieces instead of 16 MB (less half-empty
# space per frame in flight), at most 4 spare pieces kept, big one-off pieces
# not kept, and none kept when memory is low. Measured on the iPhone: 96 MB.
st = pathlib.Path("crates/nxhost/src/nvn/draw/state.rs")
s_ = st.read_text()
old = "const CHUNK: usize = 16 << 20;\n"
assert s_.count(old) == 1
s_ = s_.replace(old, "const CHUNK: usize = 4 << 20;\n")
old = """                let (_, bufs) = self.in_flight.swap_remove(i);
                self.free.extend(bufs);
"""
assert s_.count(old) == 1
s_ = s_.replace(old, """                let (_, bufs) = self.in_flight.swap_remove(i);
                self.free.extend(bufs);
                let low = { let l = gfx::available_memory(); l != 0 && l < 600 << 20 };
                let keep = if low { 0 } else { 4 };
                let mut dropped = 0usize;
                self.free.retain(|b| b.0.length() <= CHUNK || { dropped += b.0.length(); false });
                while self.free.len() > keep {
                    dropped += self.free.pop().map_or(0, |b| b.0.length());
                }
                if dropped > 0 {
                    gfx::OURS[0].fetch_sub(dropped as u64, std::sync::atomic::Ordering::Relaxed);
                }
""")
st.write_text(s_)
print("patched", st, "(smaller draw scratch buffers, spares released)")

# ---------------------------------------------------------------------------
# "[game] track: <name>" in the log each time a track loads (on by default,
# NX_TRACE_TRACKS=0 to turn off), so a log tells which track used how much memory.
fsr = pathlib.Path("crates/nxhost/src/services/fs.rs")
f_ = fsr.read_text()
old = """    static ON: std::sync::LazyLock<bool> = std::sync::LazyLock::new(|| std::env::var_os("NX_TRACE_ROMFS").is_some());
    if !*ON {
        return;
    }
"""
assert f_.count(old) == 1
f_ = f_.replace(old, """    static ON: std::sync::LazyLock<bool> = std::sync::LazyLock::new(|| std::env::var_os("NX_TRACE_ROMFS").is_some());
    static TRACKS: std::sync::LazyLock<bool> = std::sync::LazyLock::new(|| std::env::var("NX_TRACE_TRACKS").map_or(true, |v| v.trim() != "0"));
    if !*ON && !*TRACKS {
        return;
    }
""")
old = """    if seen.insert(i - 1) {
        let tag = if name == "romfs" { "" } else { "dlc:" };
        eprintln!("[romfs] {:.1} s {tag}{}", T0.elapsed().as_secs_f32(), files[i - 1].2);
    }
"""
assert f_.count(old) == 1
f_ = f_.replace(old, """    if seen.insert(i - 1) {
        let tag = if name == "romfs" { "" } else { "dlc:" };
        let path = &files[i - 1].2;
        if *ON {
            eprintln!("[romfs] {:.1} s {tag}{}", T0.elapsed().as_secs_f32(), path);
        }
        if let Some(track) = path.strip_prefix("/Course/").filter(|p| p.contains("/course_model")).and_then(|p| p.split('/').next()) {
            eprintln!("[game] track: {track}{} (app uses {} MB)", if tag.is_empty() { "" } else { " (Booster Course Pass)" }, crate::gfx::footprint() >> 20);
        }
    }
""")
fsr.write_text(f_)
print("patched", fsr, "(track names in the log)")

# ---------------------------------------------------------------------------
# Profile of the game thread: time spent in the graphics API calls we translate
# (per call, the 6 slowest every 10 s) and time waiting for GPU fences, so the
# log says what the game thread does when a frame takes 40 ms instead of 16.
nm = pathlib.Path("crates/nxhost/src/nvn/mod.rs")
s_ = nm.read_text()
for a_, b_ in [
    ("struct Proc {\n    name: String,\n    calls: AtomicU64,\n", "struct Proc {\n    name: String,\n    calls: AtomicU64,\n    ns: AtomicU64,\n"),
    ("Proc { name: name.to_string(), calls: AtomicU64::new(0), host: host_impl(name) }", "Proc { name: name.to_string(), calls: AtomicU64::new(0), ns: AtomicU64::new(0), host: host_impl(name) }"),
    ("    if let Some(f) = p.host {\n        return f(a);\n    }\n",
     "    if let Some(f) = p.host {\n        let t = std::time::Instant::now();\n        let r = f(a);\n        let ns = t.elapsed().as_nanos() as u64;\n        p.ns.fetch_add(ns, Relaxed);\n        crate::gfx::prof::NVN_NS.fetch_add(ns, Relaxed);\n        return r;\n    }\n"),
    ("fn log_limit() -> u64 {\n", """/// The graphics API calls that took the most time since the last call (ms, calls).
pub fn top_calls(n: usize) -> String {
    let procs = PROCS.lock().unwrap();
    let mut v: Vec<(u64, u64, &str)> = procs.iter().map(|p| (p.ns.swap(0, Relaxed), p.calls.load(Relaxed), p.name.as_str())).filter(|x| x.0 > 0).collect();
    v.sort_by(|a, b| b.0.cmp(&a.0));
    v.iter().take(n).map(|(ns, _, name)| format!("{} {:.0} ms", name.trim_start_matches("nvn"), *ns as f64 / 1e6)).collect::<Vec<_>>().join(", ")
}

fn log_limit() -> u64 {
"""),
]:
    assert s_.count(a_) == 1, a_[:50]
    s_ = s_.replace(a_, b_)
nm.write_text(s_)
gm = pathlib.Path("crates/nxhost/src/gfx/mod.rs")
s_ = gm.read_text()
old = "    pub static PIPE_NS: AtomicU64 = AtomicU64::new(0);\n"
assert s_.count(old) == 1
s_ = s_.replace(old, old + "    pub static NVN_NS: AtomicU64 = AtomicU64::new(0);\n    pub static FENCE_NS: AtomicU64 = AtomicU64::new(0);\n")
gm.write_text(s_)
cm = pathlib.Path("crates/nxhost/src/nvn/cmd.rs")
s_ = cm.read_text()
old = "pub fn sync_wait(a: &Args) -> u64 {\n"
assert s_.count(old) == 1
s_ = s_.replace(old, old + "    let _t = crate::gfx::prof::Timer::new(&crate::gfx::prof::FENCE_NS);\n")
cm.write_text(s_)
dm = pathlib.Path("crates/nxhost/src/nvn/draw/mod.rs")
s_ = dm.read_text()
old = """        if async_gfx::on() {
            eprintln!("[prof] graphics thread: busy {:.1} ms per frame, game thread waited for it {:.1} ms per frame", submit / frames, gfx_waited / frames);
        }
"""
assert s_.count(old) == 1
s_ = s_.replace(old, old + """        {
            let nvn = gfx::prof::take_ms(&gfx::prof::NVN_NS);
            let fence = gfx::prof::take_ms(&gfx::prof::FENCE_NS);
            eprintln!("[prof] game thread: graphics calls {:.1} ms per frame (of which waiting for GPU fences {:.1} ms)", nvn / frames, fence / frames);
            static TICK: std::sync::atomic::AtomicU32 = std::sync::atomic::AtomicU32::new(0);
            if TICK.fetch_add(1, Relaxed) % 10 == 9 {
                eprintln!("[prof] slowest graphics calls (10 s): {}", super::top_calls(6));
            }
        }
""")
dm.write_text(s_)
print("patched", nm, cm, dm, "(game thread profile)")

# ---------------------------------------------------------------------------
# The thread that presents frames (the game's render thread) gets the same
# "interactive" priority as our graphics thread, so iOS keeps it on a fast core
# instead of letting other game threads or our helpers take its place
# (NX_STOCK_QOS=1 keeps the old priorities).
cm = pathlib.Path("crates/nxhost/src/nvn/cmd.rs")
s_ = cm.read_text()
old = "pub fn present(a: &Args) -> u64 {\n"
assert s_.count(old) == 1
s_ = s_.replace(old, old + """    thread_local!(static RAISED: std::cell::Cell<bool> = const { std::cell::Cell::new(false) });
    if !RAISED.get() && std::env::var_os("NX_STOCK_QOS").is_none() {
        RAISED.set(true);
        unsafe { libc::pthread_set_qos_class_self_np(libc::qos_class_t::QOS_CLASS_USER_INTERACTIVE, 0) };
        eprintln!("[host] the game's render thread now has interactive priority");
    }
""")
cm.write_text(s_)
print("patched", cm, "(render thread priority)")
