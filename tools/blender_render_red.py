"""补齐红版的侧视与正视（沿用当前场景与灯光）。"""
import bpy, os

sc = bpy.context.scene
OUT = r"G:\workbuddy\语音助手\deliverables\render"

RED = bpy.data.materials["BodyRed"]
for nm in ("Body",):   # 只刷机身；键帽由 KeyCapFIX 固定，刷了会和白机身一起消失
    o = bpy.data.objects.get(nm)
    if o:
        o.data.materials.clear()
        o.data.materials.append(RED)

tgt = bpy.data.objects.get("CamTarget")
cam = bpy.data.objects.get("Cam")
sc.camera = cam


def shoot(path, loc, ortho=None, lens=78, res=(1200, 1500), tz=70):
    tgt.location = (0, 0, tz)
    cam.location = loc
    if ortho:
        cam.data.type = 'ORTHO'
        cam.data.ortho_scale = ortho
    else:
        cam.data.type = 'PERSP'
        cam.data.lens = lens
    sc.render.resolution_x, sc.render.resolution_y = res
    sc.render.filepath = path
    bpy.ops.render.render(write_still=True)
    print("WROTE", os.path.basename(path))


shoot(os.path.join(OUT, "side_red.png"), (660, 0, 70), ortho=195, res=(1200, 1500))
shoot(os.path.join(OUT, "front_red.png"), (0, -680, 70), ortho=190, res=(1250, 1500))
print("DONE")
