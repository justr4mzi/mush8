#include <metal_stdlib>
using namespace metal;
struct V { float4 p [[position]]; };
vertex V probe_vs(uint i [[vertex_id]]) { V v; v.p = float4(float(i), 0, 0, 1); return v; }
struct F { float4 c0 [[color(0)]]; uint4 c2 [[color(2)]]; };
fragment F probe_fs(V v [[stage_in]]) { F f; f.c0 = v.p; f.c2 = uint4(1); return f; }
