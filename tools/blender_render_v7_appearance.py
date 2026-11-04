"""渲染陪伴机器人产品图。参数刻意保守，避免阻塞 Blender 主线程。"""
import bpy, math, os

sc = bpy.context.scene
sc.render.engine = 'BLENDER_EEVEE'
try:
    sc.eevee.taa_render_samples = 64
except Exception:
    pass
sc.view_settings.view_transform = 'AgX'
sc.view_settings.look = 'AgX - Punchy'
sc.view_settings.exposure = -0.08

OUT = r"G:\workbuddy\语音助手\deliverables\render"
os.makedirs(OUT, exist_ok=True)

w = bpy.data.worlds.get("World") or bpy.data.worlds.new("World")
sc.world = w
w.use_nodes = True
bg = next(n for n in w.node_tree.nodes if n.type == 'BACKGROUND')
# 世界环境做上下渐变。均匀灰环境会让保护层反射成一层灰雾，屏幕读不出来；
# 有梯度才有高光结构，才像棚拍。
_nt = w.node_tree
_tc = _nt.nodes.new('ShaderNodeTexCoord')
_sep = _nt.nodes.new('ShaderNodeSeparateXYZ')
_mr = _nt.nodes.new('ShaderNodeMapRange')
_mr.inputs['From Min'].default_value = -1.0
_mr.inputs['From Max'].default_value = 1.0
_ramp = _nt.nodes.new('ShaderNodeValToRGB')
_ramp.color_ramp.elements[0].position = 0.0
_ramp.color_ramp.elements[0].color = (0.22, 0.22, 0.22, 1)
_ramp.color_ramp.elements[1].position = 1.0
_ramp.color_ramp.elements[1].color = (0.98, 0.975, 0.965, 1)
_nt.links.new(_tc.outputs['Generated'], _sep.inputs[0])
_nt.links.new(_sep.outputs['Z'], _mr.inputs['Value'])
_nt.links.new(_mr.outputs['Result'], _ramp.inputs['Fac'])
_nt.links.new(_ramp.outputs['Color'], bg.inputs[0])
bg.inputs[1].default_value = 0.62

if "Floor" not in bpy.data.objects:
    bpy.ops.mesh.primitive_plane_add(size=4000, location=(0, 0, 0))
    fl = bpy.context.object
    fl.name = "Floor"
    m = bpy.data.materials.new("FloorMat")
    m.use_nodes = True
    b = next(n for n in m.node_tree.nodes if n.type == 'BSDF_PRINCIPLED')
    b.inputs['Base Color'].default_value = (0.60, 0.595, 0.585, 1)
    b.inputs['Roughness'].default_value = 0.55
    fl.data.materials.append(m)

for o in [o for o in bpy.data.objects if o.type == 'LIGHT']:
    bpy.data.objects.remove(o, do_unlink=True)


def sun(name, energy, rot, angle=8.0, color=(1.0, 0.97, 0.92)):
    d = bpy.data.lights.new(name, 'SUN')
    d.energy = energy
    d.angle = math.radians(angle)
    d.color = color
    o = bpy.data.objects.new(name, d)
    bpy.context.collection.objects.link(o)
    o.rotation_euler = rot


sun("Key", 3.55, (math.radians(56), 0, math.radians(34)), 5)
sun("Fill", 1.45, (math.radians(72), 0, math.radians(-64)), 9)
sun("Rim", 2.5, (math.radians(106), 0, math.radians(198)), 6)

# 注意：不要启用 FOG_GLOW 合成。Blender 5.4 Alpha 上该节点会让渲染永久阻塞
# （实测两次都卡死主线程，连 addon 的 ping 都不再响应）。发光靠材质自发光即可。
sc.use_nodes = False

tgt = bpy.data.objects.get("CamTarget")
if not tgt:
    tgt = bpy.data.objects.new("CamTarget", None)
    bpy.context.collection.objects.link(tgt)
cam = bpy.data.objects.get("Cam")
if not cam:
    cd = bpy.data.cameras.new("Cam")
    cam = bpy.data.objects.new("Cam", cd)
    bpy.context.collection.objects.link(cam)
    con = cam.constraints.new('TRACK_TO')
    con.target = tgt
    con.track_axis = 'TRACK_NEGATIVE_Z'
    con.up_axis = 'UP_Y'
sc.camera = cam


def shoot(path, loc, ortho=None, lens=88, res=(1300, 1600), tz=76):
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


WHITE = bpy.data.materials["BodyWhite"]
RED = bpy.data.materials["BodyRed"]


def _srgb(h):
    h = h.lstrip('#')
    c = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    return tuple((x / 12.92 if x <= 0.04045 else ((x + 0.055) / 1.055) ** 2.4) for x in c)


def set_band(hexc):
    """灯带颜色按 SKU 分开。白版用"柔和红"（之前定稿的呼吸色）；
    红版必须换琥珀——红机身上再打一圈红光，灯带就融进机身了，
    "亮着"和"没亮"分不出来，老人会以为设备没反应。"""
    for m in bpy.data.materials:
        if not m.name.startswith("GlowBed") or not m.use_nodes:
            continue
        bs = next((n for n in m.node_tree.nodes if n.type == 'BSDF_PRINCIPLED'), None)
        if bs:
            bs.inputs['Emission Color'].default_value = (*_srgb(hexc), 1)


def paint(m):
    # 注意：不要在这里给 Btn_* 赋机身色。白键帽压在白机身上会完全消失，
    # 老人根本看不出那里有键。键帽固定用中性灰（见 blender_build.py M_KEYCAP），
    # 琥珀确认键才是唯一的彩色，视觉层级才成立。
    o = bpy.data.objects.get("Body")
    if o:
        o.data.materials.clear()
        o.data.materials.append(m)
    kc = bpy.data.materials.get("KeyCapFIX")
    if kc:
        for nm in ("Btn_Standby", "Btn_VolDown", "Btn_VolUp"):
            k = bpy.data.objects.get(nm)
            if k:
                k.data.materials.clear()
                k.data.materials.append(kc)


set_band("#FF4A38")
paint(WHITE)
shoot(os.path.join(OUT, "hero_white.png"), (197, -369, 254), lens=76, tz=64)
shoot(os.path.join(OUT, "side_white.png"), (541, 0, 64), ortho=177, res=(1200, 1500), tz=64)
shoot(os.path.join(OUT, "front_white.png"), (0, -558, 64), ortho=152, res=(1250, 1500), tz=64)
set_band("#FF9E3C")
paint(RED)
shoot(os.path.join(OUT, "hero_red.png"), (197, -369, 254), lens=76, tz=64)
print("DONE")
