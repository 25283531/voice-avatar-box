"""渲染可打印壳体（v8.0）。参数刻意保守，避免阻塞 Blender 主线程。
注意：不要启用合成器（FOG_GLOW / Glare / BLOOM 在 5.4 Alpha 上都会卡死主线程）。
"""
import bpy, math, os

sc = bpy.context.scene
sc.render.engine = 'BLENDER_EEVEE'
try:
    sc.eevee.taa_render_samples = 64
except Exception:
    pass
sc.view_settings.view_transform = 'AgX'
sc.view_settings.look = 'AgX - Punchy'
sc.view_settings.exposure = -0.05

OUT = r"G:\workbuddy\语音助手\deliverables\render"
os.makedirs(OUT, exist_ok=True)

FLOOR_Z = -3.25

w = bpy.data.worlds.get("World") or bpy.data.worlds.new("World")
sc.world = w
w.use_nodes = True
bg = next(n for n in w.node_tree.nodes if n.type == 'BACKGROUND')
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
    bpy.ops.mesh.primitive_plane_add(size=4000, location=(0, 0, FLOOR_Z))
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


def shoot(path, loc, ortho=None, lens=76, res=(1300, 1600), tz=64):
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


plate = bpy.data.objects.get("BottomPlate")
plate_home = tuple(plate.location)

# ---- 合体：四视图 ----
shoot(os.path.join(OUT, "print_hero.png"), (197, -369, 254), lens=76, tz=64)
shoot(os.path.join(OUT, "print_side.png"), (541, 0, 64), ortho=177,
      res=(1200, 1500), tz=64)
shoot(os.path.join(OUT, "print_front.png"), (0, -558, 64), ortho=152,
      res=(1250, 1500), tz=64)

# ---- 背面：Type-C 口 ----
shoot(os.path.join(OUT, "print_back.png"), (-150, 430, 150), lens=85, tz=44)

# ---- 底面 / 爆炸：相机在地板以下，必须隐藏地板（否则拍到的是地板背面）----
floor = bpy.data.objects.get("Floor")
if floor:
    floor.hide_render = True

shoot(os.path.join(OUT, "print_bottom.png"), (95, -185, -330), lens=70, tz=6)

# 仰视才能同时看到底口里的凸台和底板上对应的顶柱；从上方看主壳会把它全挡住。
plate.location = (plate_home[0], plate_home[1], plate_home[2] - 58.0)
shoot(os.path.join(OUT, "print_explode.png"), (120, -260, -300), lens=60, tz=50)
plate.location = plate_home

if floor:
    floor.hide_render = False

print("DONE")
