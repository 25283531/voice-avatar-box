import bpy
o = bpy.data.objects.get("MainShell")
zs = [(o.matrix_world @ v.co).z for v in o.data.vertices]
ys = [(o.matrix_world @ v.co).y for v in o.data.vertices]
xs = [(o.matrix_world @ v.co).x for v in o.data.vertices]
print("shell z %.3f .. %.3f   y %.3f .. %.3f   x %.3f .. %.3f"
      % (min(zs), max(zs), min(ys), max(ys), min(xs), max(xs)))
# 找 z 最大的那批顶点在 y 上的位置
top = [ (o.matrix_world @ v.co) for v in o.data.vertices if (o.matrix_world @ v.co).z > max(zs) - 0.2 ]
print("top verts %d, y range %.2f .. %.2f" % (len(top), min(p.y for p in top), max(p.y for p in top)))
print("dimensions", [round(v,2) for v in o.dimensions])
p = bpy.data.objects.get("BottomPlate")
print("plate", [round(v,2) for v in p.dimensions], "z", round(min((p.matrix_world@v.co).z for v in p.data.vertices),2),
      round(max((p.matrix_world@v.co).z for v in p.data.vertices),2))
# 非流形检查
import bmesh
bm = bmesh.new(); bm.from_mesh(o.data)
nm = [e for e in bm.edges if not e.is_manifold]
print("shell non-manifold edges", len(nm), "verts", len(bm.verts), "faces", len(bm.faces))
bm.free()
