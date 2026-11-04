"""陪伴机器人 · 可打印壳体 v8.0（最终 3D 打印件）

与 v7.0 外观模型的差别：
  1. 侧轮廓「斜面 ↔ 前脸」的钝角改成 R30 相切圆弧（原来是折线 + 5.5 倒角）
  2. 背部底端开 Type-C 口（口部沉台 + 贯穿孔）
  3. 去掉按键 / 屏幕 / 保护层 / 灯带 / 屏幕 UI —— 只留壳体，但保留它们的位置开孔
  4. 变成真壳体：壁厚 3.2mm，Solidify 挖空
  5. 底部打开：主壳 + 底板（止口 + 4 颗 M3 自攻），分模线在底面，正面侧面完全看不见

输出：MainShell / BottomPlate 两个对象，各导出一份 STL。
通过 Blender MCP 插件 socket 在运行中的 Blender 里执行。
"""
import bpy, bmesh, math
from mathutils import Vector

for o in list(bpy.data.objects):
    bpy.data.objects.remove(o, do_unlink=True)
for coll in (bpy.data.meshes, bpy.data.materials, bpy.data.curves):
    for x in list(coll):
        if x.users == 0:
            coll.remove(x)

sc = bpy.context.scene
sc.unit_settings.system = 'METRIC'
sc.unit_settings.scale_length = 0.001
sc.unit_settings.length_unit = 'MILLIMETERS'

# ==================== 主参数 ====================
W, H, D = 118.0, 127.0, 76.0
FRONT_V, TILT, BEV = 38.0, math.radians(18.0), 5.5
WALL = 3.2                     # 打印壁厚（3.2 = 8 圈 0.4 喷嘴）
R_TRANS = 45.0                 # 斜面↔前脸过渡圆弧半径（弧长约 8.5mm）
HW, HD = W / 2, D / 2
RISE = H - FRONT_V
SETBACK = RISE * math.tan(TILT)
PLAT = D - SETBACK
ang = TILT
slope = math.hypot(SETBACK, RISE)
P0 = (-HD, FRONT_V)
P1 = (-HD + SETBACK, H)
ROT = (-ang, 0.0, 0.0)

# 过渡圆弧：与前脸、斜面两条直线相切。
# 两线夹角 18°（几乎平行），切线长 = R / tan((180-18)/2) = R / tan81° ≈ R/6.31。
# 所以 R 再大，切点也只往前脸/斜面方向走几毫米 —— 是一条很短的「缓弯」，不是大圆角。
TL = R_TRANS / math.tan(math.radians(81.0))
T1 = (-HD, FRONT_V - TL)
T2 = (-HD + TL * math.sin(ang), FRONT_V + TL * math.cos(ang))
CX, CZ = T1[0] + R_TRANS, T1[1]
ARC_N = 14

print("W%.0f H%.0f D%.0f | setback %.2f platform %.2f | W/H %.2f"
      % (W, H, D, SETBACK, PLAT, W / H))
print("圆弧 R%.1f 切线长 %.2f 切点1(%.2f,%.2f) 切点2(%.2f,%.2f) 圆心(%.2f,%.2f)"
      % (R_TRANS, TL, T1[0], T1[1], T2[0], T2[1], CX, CZ))
print("前脸直段 %.2f -> %.2f（圆弧吃掉 %.2f）" % (FRONT_V, T1[1], TL))


def on_slant(u, v, w=0.0):
    n = Vector((0.0, -math.cos(ang), math.sin(ang)))
    b = Vector((0.0, P0[0] + math.sin(ang) * v, P0[1] + math.cos(ang) * v))
    return Vector((u, 0, 0)) + b + n * w


# ==================== 材质 ====================
def srgb(h):
    h = h.lstrip('#')
    c = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    return tuple((x / 12.92 if x <= 0.04045 else ((x + 0.055) / 1.055) ** 2.4) for x in c)


def mat(name, hexc, rough=0.5):
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    b = next(n for n in m.node_tree.nodes if n.type == 'BSDF_PRINCIPLED')
    r, g, bl = srgb(hexc)
    b.inputs['Base Color'].default_value = (r, g, bl, 1)
    b.inputs['Roughness'].default_value = rough
    return m


M_SHELL = mat("ShellWhite", "#EFE9DF", 0.62)
M_PLATE = mat("PlateWhite", "#E6E0D6", 0.66)
M_MASK = mat("LightMask", "#141210", 0.9)      # 屏幕后遮光片（非打印件，只为渲染）


# ==================== 通用几何 ====================
def rrect_pts(w, h, r, seg=10, roll=0.0):
    pts = []
    for (cx, cz, a0) in ((w / 2 - r, h / 2 - r, 0), (-w / 2 + r, h / 2 - r, 90),
                         (-w / 2 + r, -h / 2 + r, 180), (w / 2 - r, -h / 2 + r, 270)):
        for i in range(seg + 1):
            a = math.radians(a0 + 90.0 * i / seg)
            pts.append((cx + r * math.cos(a), cz + r * math.sin(a)))
    if roll:
        cr, sr = math.cos(roll), math.sin(roll)
        pts = [(x * cr - z * sr, x * sr + z * cr) for (x, z) in pts]
    return pts


def rrect_prism(name, w, h, r, t, material=None, seg=10, roll=0.0):
    """圆角矩形棱柱：X 宽 / Z 高 / Y 厚，中心在原点"""
    pts = rrect_pts(w, h, r, seg, roll)
    n = len(pts)
    verts = [(x, -t / 2, z) for (x, z) in pts] + [(x, t / 2, z) for (x, z) in pts]
    faces = [tuple(range(n)), tuple(range(n, 2 * n))[::-1]]
    for i in range(n):
        j = (i + 1) % n
        faces.append((i, j, n + j, n + i))
    me = bpy.data.meshes.new(name + "Mesh")
    me.from_pydata(verts, [], faces)
    me.update()
    bm = bmesh.new()
    bm.from_mesh(me)
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    bm.to_mesh(me)
    bm.free()
    ob = bpy.data.objects.new(name, me)
    bpy.context.collection.objects.link(ob)
    if material:
        ob.data.materials.append(material)
    return ob


def boolean_cut(target, cutter, label=""):
    bpy.context.view_layer.objects.active = target
    md = target.modifiers.new("cut", 'BOOLEAN')
    md.operation = 'DIFFERENCE'
    md.object = cutter
    try:
        md.solver = 'EXACT'
    except Exception:
        pass
    bpy.ops.object.modifier_apply(modifier=md.name)
    bpy.data.objects.remove(cutter, do_unlink=True)
    if label:
        print("  cut %-16s polys %d" % (label, len(target.data.polygons)))


# ==================== 1. 外壳 ====================
prof = [(-HD, 0.0), T1]
for i in range(1, ARC_N):
    a = math.radians(180.0 - 18.0 * i / ARC_N)
    prof.append((CX + R_TRANS * math.cos(a), CZ + R_TRANS * math.sin(a)))
prof.append(T2)
prof.append(P1)
prof.append((HD, H))
prof.append((HD, 0.0))

verts, faces = [], []
n = len(prof)
for x in (-HW, HW):
    for (y, z) in prof:
        verts.append((x, y, z))
faces.append(tuple(range(n)))
faces.append(tuple(range(2 * n - 1, n - 1, -1)))
for i in range(n):
    j = (i + 1) % n
    faces.append((i, j, n + j, n + i))

me = bpy.data.meshes.new("ShellMesh")
me.from_pydata(verts, [], faces)
me.update()
shell = bpy.data.objects.new("MainShell", me)
bpy.context.collection.objects.link(shell)
bm = bmesh.new()
bm.from_mesh(me)
bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
bm.to_mesh(me)
bm.free()

bpy.context.view_layer.objects.active = shell
shell.select_set(True)
bv = shell.modifiers.new("B", 'BEVEL')
bv.width = BEV
bv.segments = 10
bv.limit_method = 'ANGLE'
bv.angle_limit = math.radians(28)
bpy.ops.object.modifier_apply(modifier=bv.name)
print("倒角后 polys", len(shell.data.polygons))

sv = shell.modifiers.new("S", 'SOLIDIFY')
sv.thickness = WALL
sv.offset = -1.0
sv.use_even_offset = True
sv.use_quality_normals = True
bpy.ops.object.modifier_apply(modifier=sv.name)
shell.data.materials.append(M_SHELL)
print("Solidify %.1f 后 polys %d" % (WALL, len(shell.data.polygons)))

# ==================== 2. 底口（开合面）====================
# 底口不能超过「底面平直区」。底面 118x76，倒角 5.5 之后平直区只剩 107x65，
# 底口一旦超过它，就把底面边缘切穿了 —— 实测剩下一圈 0.58mm 高的圆弧薄边，
# 底板没地方坐，壳体底部也变成刀口。所以底口取 106x64（比平直区小 1）。
FLAT_W, FLAT_D = W - 2 * BEV, D - 2 * BEV
BOT_W, BOT_D = FLAT_W - 1.0, FLAT_D - 1.0
c = rrect_prism("cutBottom", BOT_W, BOT_D, BEV - WALL + 0.15, 8.0)
c.rotation_euler = (math.radians(90), 0, 0)
c.location = (0, 0, -1.0)
boolean_cut(shell, c, "bottom opening")
print("底口 %.1f x %.1f | 底面平直区 %.1f x %.1f | 内腔 %.1f x %.1f"
      % (BOT_W, BOT_D, FLAT_W, FLAT_D, W - 2 * WALL, D - 2 * WALL))

# ==================== 3. 前脸：保护层凹槽 / 屏幕台阶通孔 ====================
SCR_W, SCR_H = 84.54, 56.36          # 4.0" 3:2 显示区
SLAB_W, SLAB_H = 90.5, 62.4          # 屏模组外形（含 3.2 黑边）
PAN_W, PAN_H = 108.6, 80.0           # 保护层外形
POCK_PAN = 1.4                       # 保护层凹槽深（壁厚 3.2，留 1.8 实壁）
SCR_FRONT = -2.45                    # 屏前表面沉深
SCR_BACK = -5.45                     # 屏模组背面（3.0 厚）
v_c = slope / 2.0

c = rrect_prism("cutPanel", PAN_W + 0.4, PAN_H + 0.4, 10.0, POCK_PAN + 4.0)
c.rotation_euler = ROT
c.location = on_slant(0, v_c, -POCK_PAN + (POCK_PAN + 4.0) / 2.0)
boolean_cut(shell, c, "cover pocket")

c = rrect_prism("cutScrFront", SCR_W + 0.2, SCR_H + 0.2, 4.0, 3.0 - SCR_FRONT)
c.rotation_euler = ROT
c.location = on_slant(0, v_c, (3.0 + SCR_FRONT) / 2.0)
boolean_cut(shell, c, "screen front")

# 后台阶：比屏模组大 1.0；上表面比前孔底面高 0.15，避开共面
c = rrect_prism("cutScrBack", SLAB_W + 1.0, SLAB_H + 1.0, 4.0,
                (SCR_FRONT + 0.15) - SCR_BACK)
c.rotation_euler = ROT
c.location = on_slant(0, v_c, ((SCR_FRONT + 0.15) + SCR_BACK) / 2.0)
boolean_cut(shell, c, "screen back")
print("前脸: 凹槽 %.1f | 屏前 %.2f 屏背 %.2f | 台阶面 %.2f（通孔）"
      % (POCK_PAN, SCR_FRONT, SCR_BACK, SCR_FRONT))

mask = rrect_prism("ScreenMask", SLAB_W - 0.4, SLAB_H - 0.4, 3.0, 0.4, M_MASK, seg=8)
mask.rotation_euler = ROT
mask.location = on_slant(0, v_c, SCR_BACK + 0.3)

# ==================== 4. 顶部按键孔 ====================
fy0, fy1 = P1[0] + BEV, HD - BEV
bcy = (fy0 + fy1) / 2.0
KEY_GAP = 0.3


def X(mm):
    return mm - HW


for nm, cx, kw, kh in [("Standby", 15.0, 14.0, 17.0),
                       ("VolDown", 45.5, 14.0, 17.0),
                       ("VolUp", 62.5, 14.0, 17.0)]:
    c = rrect_prism("cutKey" + nm, kw + KEY_GAP, kh + KEY_GAP, 2.0, 12.0)
    c.rotation_euler = (math.radians(90), 0, 0)
    c.location = (X(cx), bcy, H)
    boolean_cut(shell, c, "key " + nm)

bpy.ops.mesh.primitive_cylinder_add(radius=12.0 + KEY_GAP / 2, depth=12.0,
                                    vertices=72, location=(X(98.0), bcy, H))
ck = bpy.context.object
ck.name = "cutConfirm"
boolean_cut(shell, ck, "key confirm")
print("按键孔: 三键 14.3x17.3 + 确认 Ø24.3 | 平台 y %.2f..%.2f" % (fy0, fy1))

# ==================== 5. 网罩孔（105 个 Ø2.7 贯穿）====================
GR_W, GR_H = 100.0, 22.0
GR_CZ = 18.0                    # 居中偏下：上沿留 1.8 不撞过渡圆弧
R_HOLE, SEG_H = 1.35, 12
nx, nz = 21, 5
Y0, Y1 = -HD - 2.0, -HD + WALL + 1.0

HV, HF = [], []


def add_hole(cx, cz, r, y0, y1, seg=SEG_H):
    b = len(HV)
    for y in (y0, y1):
        for k in range(seg):
            a = 2 * math.pi * k / seg
            HV.append((cx + r * math.cos(a), y, cz + r * math.sin(a)))
    HF.append(tuple(range(b, b + seg)))
    HF.append(tuple(range(b + seg, b + 2 * seg))[::-1])
    for k in range(seg):
        k2 = (k + 1) % seg
        HF.append((b + k, b + k2, b + seg + k2, b + seg + k))


for ix in range(nx):
    for iz in range(nz):
        cx = -(GR_W - 8) / 2.0 + ix * (GR_W - 8) / (nx - 1)
        cz = GR_CZ - (GR_H - 4) / 2.0 + iz * (GR_H - 4) / (nz - 1)
        add_hole(cx, cz, R_HOLE, Y0, Y1)

gm = bpy.data.meshes.new("GrilleHoleMesh")
gm.from_pydata(HV, [], HF)
gm.update()
bm = bmesh.new()
bm.from_mesh(gm)
bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
bm.to_mesh(gm)
bm.free()
gobj = bpy.data.objects.new("cutGrille", gm)
bpy.context.collection.objects.link(gobj)
boolean_cut(shell, gobj, "grille %dx%d" % (nx, nz))
print("网罩 %d 孔 Ø%.1f"
      % (nx * nz, R_HOLE * 2))

# ==================== 6. Type-C 口（背部靠底端）====================
USBC_Z = 12.0
USBC_W, USBC_H = 9.2, 3.4
USBC_REC, USBC_RECH = 10.8, 5.0
c = rrect_prism("cutUsbIn", USBC_W, USBC_H, USBC_H / 2 - 0.1, WALL + 8.0)
c.location = (0, HD, USBC_Z)
boolean_cut(shell, c, "usb-c bore")
c = rrect_prism("cutUsbOut", USBC_REC, USBC_RECH, 1.6, 2.0)
c.location = (0, HD, USBC_Z)
boolean_cut(shell, c, "usb-c recess")
print("Type-C %.1fx%.1f 沉台 %.1fx%.1f 深1.0 @z=%.1f"
      % (USBC_W, USBC_H, USBC_REC, USBC_RECH, USBC_Z))

# ==================== 7. 螺丝凸台（从内壁长出来）====================
# 凸台中心距内壁 4.0，半径 4 ⇒ 与内壁相切，union 后是连着的（悬空的话 STL 里
# 就是 4 根没连上的柱子）。底部 z=3.2 落在底口上方，打印时靠支撑；
# 装配后由底板上对应的 Ø8 凸台顶住，同时起定位作用。
# 距内壁 3.6、半径 4 ⇒ 重叠 0.4。相切（差 0.0）不算相交，布尔 union 不会合并，
# 打印出来还是 4 根独立柱子。必须留实打实的重叠量。
BOSSCX, BOSSCY = W / 2 - WALL - 3.6, D / 2 - WALL - 3.6
BOSS_D, BOSS_H = 8.0, 9.6
BOSS_Z0 = WALL
bosses = []
for sx in (-1, 1):
    for sy in (-1, 1):
        bpy.ops.mesh.primitive_cylinder_add(
            radius=BOSS_D / 2, depth=BOSS_H, vertices=40,
            location=(sx * BOSSCX, sy * BOSSCY, BOSS_Z0 + BOSS_H / 2))
        b = bpy.context.object
        b.name = "boss"
        bosses.append(b)

bpy.ops.object.select_all(action='DESELECT')
for b in bosses:
    b.select_set(True)
bpy.context.view_layer.objects.active = bosses[0]
bpy.ops.object.join()
boss = bpy.context.object
boss.name = "Bosses"

bpy.context.view_layer.objects.active = shell
md = shell.modifiers.new("u", 'BOOLEAN')
md.operation = 'UNION'
md.object = boss
try:
    md.solver = 'EXACT'
except Exception:
    pass
bpy.ops.object.modifier_apply(modifier=md.name)
bpy.data.objects.remove(boss, do_unlink=True)
print("凸台 Ø%.0f 高%.1f @(±%.1f,±%.1f) 伸入侧壁 %.1f  polys %d"
      % (BOSS_D, BOSS_H, BOSSCX, BOSSCY, BOSS_D / 2 - (W / 2 - WALL - BOSSCX),
         len(shell.data.polygons)))

for sx in (-1, 1):
    for sy in (-1, 1):
        bpy.ops.mesh.primitive_cylinder_add(
            radius=1.25, depth=BOSS_H + 6.0, vertices=24,
            location=(sx * BOSSCX, sy * BOSSCY, BOSS_Z0 + BOSS_H / 2))
        boolean_cut(shell, bpy.context.object)

bpy.context.view_layer.objects.active = shell
shell.select_set(True)
try:
    bpy.ops.object.shade_auto_smooth(angle=math.radians(35))
except Exception:
    bpy.ops.object.shade_flat()

# ==================== 8. 底板 ====================
PL_W, PL_D, PL_T = 112.0, 70.0, WALL
plate = rrect_prism("BottomPlate", PL_W, PL_D, 3.0, PL_T, M_PLATE, seg=14)
plate.rotation_euler = (math.radians(90), 0, 0)
plate.location = (0, 0, -PL_T / 2.0)          # z = -3.2 .. 0，盖在底面下方

# 底板顶柱：顶到主壳凸台底面，既托住它又给螺丝导向
pads = []
for sx in (-1, 1):
    for sy in (-1, 1):
        bpy.ops.mesh.primitive_cylinder_add(
            radius=BOSS_D / 2, depth=WALL, vertices=40,
            location=(sx * BOSSCX, sy * BOSSCY, WALL / 2.0))
        pads.append(bpy.context.object)
bpy.ops.object.select_all(action='DESELECT')
for p_ in pads:
    p_.select_set(True)
bpy.context.view_layer.objects.active = pads[0]
bpy.ops.object.join()
pad = bpy.context.object
bpy.context.view_layer.objects.active = plate
md = plate.modifiers.new("u", 'BOOLEAN')
md.operation = 'UNION'
md.object = pad
try:
    md.solver = 'EXACT'
except Exception:
    pass
bpy.ops.object.modifier_apply(modifier=md.name)
bpy.data.objects.remove(pad, do_unlink=True)

# 螺丝过孔 Ø3.4 + 底面沉台 Ø6.0 深 1.6（M3 盘头藏进去，底面平整）
for sx in (-1, 1):
    for sy in (-1, 1):
        bpy.ops.mesh.primitive_cylinder_add(
            radius=1.7, depth=PL_T + 8.0, vertices=24,
            location=(sx * BOSSCX, sy * BOSSCY, 0.0))
        boolean_cut(plate, bpy.context.object)
        bpy.ops.mesh.primitive_cylinder_add(
            radius=3.0, depth=3.2, vertices=32,
            location=(sx * BOSSCX, sy * BOSSCY, -PL_T + 0.8))
        boolean_cut(plate, bpy.context.object)

bpy.context.view_layer.objects.active = plate
plate.select_set(True)
try:
    bpy.ops.object.shade_auto_smooth(angle=math.radians(35))
except Exception:
    bpy.ops.object.shade_flat()
print("底板 %.1f x %.1f x %.1f + 4 顶柱 Ø%.0f | polys %d"
      % (PL_W, PL_D, PL_T, BOSS_D, len(plate.data.polygons)))

# ==================== 9. 保存 + 导出 STL ====================
print("OBJ %d | SHELL %s | PLATE %s"
      % (len(bpy.data.objects),
         [round(v, 1) for v in shell.dimensions],
         [round(v, 1) for v in plate.dimensions]))

OUT = r"G://workbuddy//语音助手//deliverables"
try:
    bpy.ops.wm.save_as_mainfile(filepath=OUT + "//companion-robot-shell-printable.blend")
    print("SAVED blend")
except Exception as e:
    print("SAVEFAIL", e)

for ob, fn in ((shell, "shell-main"), (plate, "shell-bottom")):
    bpy.ops.object.select_all(action='DESELECT')
    ob.select_set(True)
    bpy.context.view_layer.objects.active = ob
    try:
        # use_scene_unit=True 会乘上 scale_length=0.001（导出成"米"），
        # 切片软件读到的就是一个 0.12mm 的微雕。关掉它，原始坐标即是毫米。
        bpy.ops.wm.stl_export(filepath=OUT + "//stl//%s.stl" % fn,
                              export_selected_objects=True,
                              global_scale=1.0,
                              use_scene_unit=False)
        print("STL", fn)
    except Exception as e:
        print("STLFAIL", fn, e)
