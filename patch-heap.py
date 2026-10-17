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
