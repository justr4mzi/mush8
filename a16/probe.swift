// Builds one render pipeline with every feature Mush8 uses (gapped color
// attachments, blending, write masks, depth+stencil), adds it to a binary
// archive and serializes it, so `metal-source` shows the exact JSON spelling
// that metal-tt expects.
import Metal
import Foundation

let src = """
#include <metal_stdlib>
using namespace metal;
struct V { float4 p [[position]]; };
vertex V probe_vs(uint i [[vertex_id]]) { V v; v.p = float4(float(i), 0, 0, 1); return v; }
struct F { float4 c0 [[color(0)]]; uint4 c2 [[color(2)]]; };
fragment F probe_fs(V v [[stage_in]]) { F f; f.c0 = v.p; f.c2 = uint4(1); return f; }
"""
let dev = MTLCreateSystemDefaultDevice()!
let lib = try! dev.makeLibrary(source: src, options: nil)
let d = MTLRenderPipelineDescriptor()
d.vertexFunction = lib.makeFunction(name: "probe_vs")
d.fragmentFunction = lib.makeFunction(name: "probe_fs")
let a0 = d.colorAttachments[0]!
a0.pixelFormat = .bgra8Unorm_srgb
a0.writeMask = [.red, .alpha]
a0.isBlendingEnabled = true
a0.rgbBlendOperation = .max
a0.alphaBlendOperation = .reverseSubtract
a0.sourceRGBBlendFactor = .sourceAlpha
a0.destinationRGBBlendFactor = .oneMinusSourceAlpha
a0.sourceAlphaBlendFactor = .blendColor
a0.destinationAlphaBlendFactor = .zero
let a2 = d.colorAttachments[2]!
a2.pixelFormat = .rgba8Uint
a2.writeMask = .all
d.depthAttachmentPixelFormat = .depth32Float_stencil8
d.stencilAttachmentPixelFormat = .depth32Float_stencil8
let ad = MTLBinaryArchiveDescriptor()
let ar = try! dev.makeBinaryArchive(descriptor: ad)
try! ar.addRenderPipelineFunctions(descriptor: d)
let out = URL(fileURLWithPath: CommandLine.arguments[1])
try! ar.serialize(to: out)
print("probe archive written to \(out.path)")
