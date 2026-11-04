"""陪伴机器人外观模型 v6.0
参考图定稿：前脸是一整块半透明保护层（PC），屏幕沉在其后；
LED 藏在保护层下的导光区，看到的是柔光而不是灯珠。
按键低矮（凸 1.0mm）、蚀刻图标；音量三键等高。
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

# ---------------- 主参数 ----------------
W, H, D = 118.0, 127.0, 76.0
# 前脸竖直段从 45 降到 38（底部音箱段压矮）；倒角从 9 收到 5.5——
# 机身窄了之后 9mm 圆角显得笨重，而且会把可放屏幕的平斜面吃掉太多。
FRONT_V, TILT, BEV = 38.0, math.radians(18.0), 5.5
HW, HD = W / 2, D / 2
RISE = H - FRONT_V
SETBACK = RISE * math.tan(TILT)
PLAT = D - SETBACK
FLAT = PLAT - 2 * BEV
P0 = (-HD, FRONT_V)
P1 = (-HD + SETBACK, H)
slope = math.hypot(SETBACK, RISE)
ang = TILT
# 倒角吃掉斜面两端各约 BEV，真正能放东西的平斜面：
SLOPE_FLAT = slope - 2 * BEV
print("W%.0f H%.0f D%.0f | setback %.2f platform %.2f flat %.2f | W/H %.2f"
      % (W, H, D, SETBACK, PLAT, FLAT, W / H))
print("slope %.2f  可放平斜面 %.2f" % (slope, SLOPE_FLAT))


def on_slant(u, v, w=0.0):
    n = Vector((0.0, -math.cos(ang), math.sin(ang)))
    b = Vector((0.0, P0[0] + math.sin(ang) * v, P0[1] + math.cos(ang) * v))
    return Vector((u, 0, 0)) + b + n * w


ROT = (-ang, 0.0, 0.0)


def srgb(h):
    h = h.lstrip('#')
    c = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    return tuple((x / 12.92 if x <= 0.04045 else ((x + 0.055) / 1.055) ** 2.4) for x in c)


def mat(name, hexc, rough=0.5, emis=None, estr=0.0, alpha=None, spec=None):
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    b = next(n for n in m.node_tree.nodes if n.type == 'BSDF_PRINCIPLED')
    r, g, bl = srgb(hexc)
    b.inputs['Base Color'].default_value = (r, g, bl, 1)
    b.inputs['Roughness'].default_value = rough
    if emis:
        er, eg, eb = srgb(emis)
        if 'Emission Color' in b.inputs:
            b.inputs['Emission Color'].default_value = (er, eg, eb, 1)
            b.inputs['Emission Strength'].default_value = estr
    if spec is not None and 'Specular IOR Level' in b.inputs:
        b.inputs['Specular IOR Level'].default_value = spec
    if alpha is not None:
        b.inputs['Alpha'].default_value = alpha
        for attr, val in (('blend_method', 'BLEND'),
                          ('surface_render_method', 'BLENDED'),
                          ('use_backface_culling', False)):
            try:
                setattr(m, attr, val)
            except Exception:
                pass
    return m


M_WHITE = mat("BodyWhite", "#F4EFE7", 0.38)
M_RED = mat("BodyRed", "#B5342C", 0.40)
# 保护层：alpha 混合下"面自身的高光"会按 alpha 叠上去，镜面太强就整片发灰、
# 屏幕和灯全糊掉。压镜面强度、收窄高光，才既有玻璃感又不糊。
M_PANEL = mat("CoverPanel", "#FFFDF8", 0.11, alpha=0.045, spec=0.28)
M_SCR = mat("Screen", "#020203", 0.90, spec=0.0)
M_AMBER = mat("Amber", "#F08A0E", 0.07, emis="#FF8A10", estr=1.0)
M_WHITEP = mat("PillWhite", "#FFFFFF", 0.30)
M_HOLE = mat("Hole", "#141312", 0.9)
M_GASKET = mat("Gasket", "#8C867C", 0.70)
M_KEYCAP = mat("KeyCapFIX", "#EFE9DE", 0.36)     # 名字固定，渲染脚本按名字复位
M_ENGRAVE = mat("Engrave", "#6B6660", 0.62)
M_UIW = mat("UIWhite", "#FFFFFF", 0.55, emis="#FFFFFF", estr=1.8)
M_UIINK = mat("UIInk", "#000000", 0.6)
M_UIGRAY = mat("UIGray", "#6E6B66", 0.6)
M_UIBLUE = mat("UIBLUE", "#2C7BE5", 0.45, emis="#2C7BE5", estr=0.6)


# ---------------- 通用几何 ----------------
def rrect_pts(w, h, r, seg=10, roll=0.0):
    """圆角矩形轮廓点（XZ 平面，逆时针）。roll 在面内绕法线转——
    欧拉角做不到这件事（XYZ 序下 Ry/Rz 都是世界轴），必须在网格上转。"""
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


def boolean_cut(target, cutter):
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


# ---------------- 机身 ----------------
prof = [(-HD, 0.0), (-HD, FRONT_V), (P1[0], H), (HD, H), (HD, 0.0)]
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

me = bpy.data.meshes.new("BodyMesh")
me.from_pydata(verts, [], faces)
me.update()
body = bpy.data.objects.new("Body", me)
bpy.context.collection.objects.link(body)
bm = bmesh.new()
bm.from_mesh(me)
bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
bm.to_mesh(me)
bm.free()

bpy.context.view_layer.objects.active = body
body.select_set(True)
bv = body.modifiers.new("B", 'BEVEL')
bv.width = BEV
bv.segments = 10
bv.limit_method = 'ANGLE'
bv.angle_limit = math.radians(28)
bpy.ops.object.modifier_apply(modifier=bv.name)
body.data.materials.append(M_WHITE)
try:
    bpy.ops.object.shade_auto_smooth(angle=math.radians(30))
except Exception:
    bpy.ops.object.shade_smooth()

# ---------------- 前脸：保护层凹槽 + 屏幕凹槽 ----------------
SCR_W, SCR_H = 84.54, 56.36          # 4.0" 3:2 显示区
SLAB_W, SLAB_H = 90.5, 62.4          # 屏模组外形（含 3mm 黑边）
# 保护层宽 = W - 2*4.7，两侧只留 4.7mm（原 23mm 大白边，视觉上像贴了张纸）。
# 省下来的宽度全给灯区：环带从 6.5 加到 8.8。
PAN_W, PAN_H = 108.6, 80.0           # 保护层外形
PAN_T = 1.2                          # 保护层厚
POCK_PAN = 2.0                       # 保护层凹槽深
POCK_SCR = 2.6                       # 屏幕凹槽深
GLOW_W = -POCK_PAN + 0.08            # 导光灯板贴在槽底
PANEL_BACK = GLOW_W + 0.06           # 面板背面离灯板 0.06mm
PANEL_FRONT = PANEL_BACK + PAN_T     # 面板正面（应 < 0，即沉在壳面下）
v_c = slope / 2

# 保护层凹槽
c = rrect_prism("cutPanel", PAN_W + 0.4, PAN_H + 0.4, 10.0, POCK_PAN + 2.0)
c.rotation_euler = ROT
c.location = on_slant(0, v_c, -POCK_PAN / 2 + 1.0)
boolean_cut(body, c)

# 屏幕凹槽
c = rrect_prism("cutScreen", SLAB_W + 0.5, SLAB_H + 0.5, 4.0, POCK_SCR + 2.0)
c.rotation_euler = ROT
c.location = on_slant(0, v_c, -POCK_SCR / 2 + 1.0)
boolean_cut(body, c)
print("panel %.1fx%.1f  pocket %.1f/%.1f  slopeFlat %.2f"
      % (PAN_W, PAN_H, POCK_PAN, POCK_SCR, SLOPE_FLAT))

# ---------------- 屏幕（沉在保护层后面）----------------
SCR_FRONT = -POCK_SCR + 0.15                 # 屏前表面（必须高于槽底，否则与槽底共面 z-fighting）
bpy.ops.mesh.primitive_cube_add(size=1)
scr = bpy.context.object
scr.name = "Screen"
scr.scale = (SLAB_W, 1.2, SLAB_H)
bpy.ops.object.transform_apply(scale=True)
scr.rotation_euler = ROT
scr.location = on_slant(0, v_c, SCR_FRONT - 0.6)
b = scr.modifiers.new("r", 'BEVEL'); b.width = 0.8; b.segments = 3
bpy.context.view_layer.objects.active = scr
bpy.ops.object.modifier_apply(modifier=b.name)
scr.data.materials.append(M_SCR)

UIW = SCR_FRONT + 0.10
bpy.ops.mesh.primitive_cube_add(size=1)
ui = bpy.context.object
ui.name = "ScreenUI"
ui.scale = (SCR_W, 0.3, SCR_H)
bpy.ops.object.transform_apply(scale=True)
ui.rotation_euler = ROT
ui.location = on_slant(0, v_c, UIW - 0.15)
b = ui.modifiers.new("r", 'BEVEL'); b.width = 1.2; b.segments = 3
bpy.context.view_layer.objects.active = ui
bpy.ops.object.modifier_apply(modifier=b.name)
ui.data.materials.append(M_UIW)

# ---------------- 18 颗导光光点（藏在保护层下）----------------
BAND = (PAN_W - (SLAB_W + 0.5)) / 2.0
hx = (SLAB_W + 0.5) / 2.0 + BAND / 2.0
hy = (SLAB_H + 0.5) / 2.0 + BAND / 2.0
GLOW_R = 3.3
pts = []
N_TOP, N_SIDE = 5, 4
for k in range(N_TOP):
    pts.append((-hx + (k + 0.5) * (2 * hx / N_TOP), hy))
for k in range(N_SIDE):
    pts.append((hx, -hy + (k + 0.5) * (2 * hy / N_SIDE)))
for k in range(N_TOP):
    pts.append((hx - (k + 0.5) * (2 * hx / N_TOP), -hy))
for k in range(N_SIDE):
    pts.append((-hx, hy - (k + 0.5) * (2 * hy / N_SIDE)))
print("dots %d  带宽 %.2f  间距 顶%.2f 侧%.2f"
      % (len(pts), BAND, 2 * hx / N_TOP, 2 * hy / N_SIDE))

# 每颗灯 = 4 层同心圆盘叠出来的柔光核。不用着色器径向渐变：
# 节点链在 EEVEE 下算出来的强度不可控（试过 Emission 节点 Weight 默认 0、
# 也试过 Object 坐标 MapRange，都不稳）。常量发光是确定性的，叠层就能做出衰减。
LED_R = 3.2        # 单颗光点半径
LED_N = 16         # 同心环数：4 层看得出台阶像水钻，16 层才连续
SEGN = 40


def emit_mat(name, hexc, strength):
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    bs = next(n for n in m.node_tree.nodes if n.type == 'BSDF_PRINCIPLED')
    bs.inputs['Base Color'].default_value = (0.0, 0.0, 0.0, 1.0)
    bs.inputs['Roughness'].default_value = 0.5
    if 'Specular IOR Level' in bs.inputs:
        bs.inputs['Specular IOR Level'].default_value = 0.0
    bs.inputs['Emission Color'].default_value = (*srgb(hexc), 1)
    bs.inputs['Emission Strength'].default_value = strength
    return m


def _dn(k):
    return (k / LED_N) ** 0.85


M_LED_RING = []
for k in range(LED_N):
    dc = (_dn(k) + _dn(k + 1)) / 2.0
    # 曲线要整条抬高：同心环的面积集中在暗端，只抬峰值的话平均亮度还是很低，
    # 渲染出来是"米色哑光珠"而不是"灯"。
    M_LED_RING.append(emit_mat("LedRing%02d" % k, "#FFA63C",
                               4.4 * (1.0 - dc) ** 1.7 + 0.0))

DV, DF, DM = [], [], []
for (px, pz) in pts:
    cbase = len(DV)
    DV.append((px, 0.0, pz))
    for k in range(LED_N):
        r_in = LED_R * _dn(k)
        r_out = LED_R * _dn(k + 1)
        if k == 0:
            b = len(DV)
            for s_ in range(SEGN):
                a = 2 * math.pi * s_ / SEGN
                DV.append((px + r_out * math.cos(a), 0.0, pz + r_out * math.sin(a)))
            for s_ in range(SEGN):
                DF.append((cbase, b + s_, b + (s_ + 1) % SEGN))
                DM.append(k)
        else:
            b0 = len(DV)
            for s_ in range(SEGN):
                a = 2 * math.pi * s_ / SEGN
                DV.append((px + r_in * math.cos(a), 0.0, pz + r_in * math.sin(a)))
            b1 = len(DV)
            for s_ in range(SEGN):
                a = 2 * math.pi * s_ / SEGN
                DV.append((px + r_out * math.cos(a), 0.0, pz + r_out * math.sin(a)))
            for s_ in range(SEGN):
                s2 = (s_ + 1) % SEGN
                DF.append((b0 + s_, b1 + s_, b1 + s2, b0 + s2))
                DM.append(k)

lm = bpy.data.meshes.new("LightRingMesh")
lm.from_pydata(DV, [], DF)
lm.update()
for m_ in M_LED_RING:
    lm.materials.append(m_)
for pi_, mi_ in enumerate(DM):
    lm.polygons[pi_].material_index = mi_
SHOW_CORES = False   # True 才渲染 18 个灯核（出灯效节奏图时用）
ring_obj = bpy.data.objects.new("LightRing", lm)
bpy.context.collection.objects.link(ring_obj)
ring_obj.hide_render = not SHOW_CORES
ring_obj.rotation_euler = ROT
ring_obj.location = on_slant(0, v_c, GLOW_W)
try:
    ring_obj.visible_shadow = False   # 发光片不投影，否则自带一圈暗边
except Exception:
    pass
print("LightRing verts %d faces %d rings %d" % (len(DV), len(DF), LED_N))

# ---------------- 导光底板：把 18 颗灯连成"一圈灯" ----------------
# 只有 18 个分离光斑时，斑与斑之间露出的是凹槽底的机身白，整排看过去像
# "粘了一串珠子"，这正是导光板要避免的。真实导光板是整块板发亮、灯位处更亮。
# 所以先铺一层均匀的低亮度环形底板，18 个亮核再叠在它上面。
def rrect_path(w, h, r, seg=16):
    """闭合路径点（XZ 平面，无重复点）。内外框用同一 seg，点数相同，可直接连成环带。
    采样点全部落在四个圆角上——直线段是两角点之间的直边，本来就不需要细分。"""
    pts = []
    for (cx, cz, a0) in ((w / 2 - r, h / 2 - r, 0), (-w / 2 + r, h / 2 - r, 90),
                         (-w / 2 + r, -h / 2 + r, 180), (w / 2 - r, -h / 2 + r, 270)):
        for i in range(seg):
            a = math.radians(a0 + 90.0 * i / seg)
            pts.append((cx + r * math.cos(a), cz + r * math.sin(a)))
    return pts


GB_OUT_W, GB_OUT_H = PAN_W - 1.2, PAN_H - 1.2
BAND_D = ((GB_OUT_W - (SLAB_W + 1.2)) / 2.0)      # 环带宽度（从外缘量到内缘）

# 单层均匀底板渲出来是"一圈橙色塑料框"，因为平面均匀发光没有明暗结构。
# 真实导光板从外缘到内缘是有层次的：边缘漏光偏暗、中间最亮、贴屏处回收。
# 所以拆成 4 条同心环带，每条一个强度。
BED_N = 16                       # 层数：4 层会露出 3 条分界线，像几圈套在一起
BED_PEAK, BED_BASE = 7.0, 1.2    # 峰值 8.2
BED_LAYERS = []
for _i in range(BED_N):
    _t0, _t1 = _i / BED_N, (_i + 1) / BED_N
    _tm = (_t0 + _t1) / 2.0
    BED_LAYERS.append((_t0, _t1,
                       BED_BASE + BED_PEAK * (math.sin(math.pi * _tm) ** 1.4)))


def _bed_path(t, seg=16):
    d = t * BAND_D
    return rrect_path(GB_OUT_W - 2 * d, GB_OUT_H - 2 * d, 9.0, seg)


M_GLOWBED = [emit_mat("GlowBed%d" % i, "#FF9E3C", st)
             for i, (_a, _b2, st) in enumerate(BED_LAYERS)]
_ts = [0.0] + [ly[1] for ly in BED_LAYERS]
_paths = [_bed_path(t) for t in _ts]
_NV = len(_paths[0])
_BV, _BF, _BM = [], [], []
for _p in _paths:
    for (x, z) in _p:
        _BV.append((x, 0.0, z))
for li in range(len(_paths) - 1):
    o0, o1 = li * _NV, (li + 1) * _NV
    for i in range(_NV):
        j = (i + 1) % _NV
        _BF.append((o0 + i, o0 + j, o1 + j, o1 + i))
        _BM.append(li)
_bm = bpy.data.meshes.new("GlowBedMesh")
_bm.from_pydata(_BV, [], _BF)
_bm.update()
for _m in M_GLOWBED:
    _bm.materials.append(_m)
for _i, _mi in enumerate(_BM):
    _bm.polygons[_i].material_index = _mi
bed = bpy.data.objects.new("GlowBed", _bm)
bpy.context.collection.objects.link(bed)
bed.rotation_euler = ROT
bed.location = on_slant(0, v_c, GLOW_W - 0.03)   # 略在灯核之后，避免共面
try:
    bed.visible_shadow = False
except Exception:
    pass
print("GlowBed band %.2f  %d layers  peak %.1f  verts %d faces %d"
      % (BAND_D, BED_N, BED_BASE + BED_PEAK, len(_BV), len(_BF)))

# ---------------- 保护层（一整块，覆盖屏幕 + 灯环）----------------
panel = rrect_prism("CoverPanel", PAN_W, PAN_H, 10.0, PAN_T, M_PANEL, seg=12)
panel.rotation_euler = ROT
panel.location = on_slant(0, v_c, PANEL_BACK + PAN_T / 2.0)
print("glow %.2f | panel back %.2f front %.2f | screen %.2f (0=壳面)"
      % (GLOW_W, PANEL_BACK, PANEL_FRONT, SCR_FRONT))

# ---------------- 顶部按键 ----------------
fy0, fy1 = P1[0] + BEV, HD - BEV
bcy = (fy0 + fy1) / 2.0
print("flat_y %.2f..%.2f center %.2f" % (fy0, fy1, bcy))


def X(mm):
    return mm - HW


KEY_RISE = 1.0      # 三键统一低矮凸起
KEY_D = 5.0
for nm, cx in [("Btn_Standby", 15.0), ("Btn_VolDown", 45.5), ("Btn_VolUp", 62.5)]:
    # rrect_prism 的矩形在 XZ 面、厚度沿 Y；转到水平面才是键帽
    g = rrect_prism(nm + "_Gasket", 14.5, 17.5, 2.0, 0.9, M_GASKET, seg=6)
    g.rotation_euler = (math.radians(90), 0, 0)
    g.location = (X(cx), bcy, H - 0.05)
    k = rrect_prism(nm, 14.0, 17.0, 1.9, KEY_D, M_KEYCAP, seg=8)
    k.rotation_euler = (math.radians(90), 0, 0)
    k.location = (X(cx), bcy, H + KEY_RISE - KEY_D / 2.0)

# 蚀刻图标（待机=电源符，音量 −/+）
IY = H + KEY_RISE + 0.02


def flat_icon(name, sx, sy, cx, cy_off=0.0, thick=0.35):
    """水平放置的图标片：sx 沿 X、sy 沿 Y。rrect_prism 的矩形在 XZ 面，转 90° 放到水平面。"""
    ob = rrect_prism(name, sx, sy, min(sx, sy) / 2.0 * 0.9, thick, M_ENGRAVE, seg=4)
    ob.rotation_euler = (math.radians(90), 0, 0)
    ob.location = (X(cx), bcy + cy_off, IY)
    return ob


flat_icon("IconMinus", 5.2, 0.9, 45.5)
flat_icon("IconPlusH", 5.2, 0.9, 62.5)
flat_icon("IconPlusV", 0.9, 5.2, 62.5)

bpy.ops.mesh.primitive_torus_add(major_radius=3.0, minor_radius=0.42,
                                 major_segments=44, minor_segments=8)
ring = bpy.context.object
ring.name = "IconPowerRing"
ring.location = (X(15.0), bcy, IY)
ring.data.materials.append(M_ENGRAVE)
flat_icon("IconPowerBar", 0.85, 2.4, 15.0, cy_off=-1.8)

# ---------------- 确认键：Ø24 高光琥珀穹顶 ----------------
bpy.ops.mesh.primitive_cylinder_add(radius=12.0, depth=6.0, vertices=96)
cb = bpy.context.object
cb.name = "Btn_Confirm"
b = cb.modifiers.new("b", 'BEVEL'); b.width = 3.4; b.segments = 9
bpy.context.view_layer.objects.active = cb
bpy.ops.object.modifier_apply(modifier=b.name)
cb.location = (X(98.0), bcy, H)
cb.data.materials.append(M_AMBER)
try:
    bpy.ops.object.shade_auto_smooth(angle=math.radians(40))
except Exception:
    pass

pi = rrect_prism("PillIcon", 14.0, 5.4, 2.6, 0.9, M_WHITEP, seg=8,
                 roll=math.radians(-36))
pi.rotation_euler = (math.radians(90), 0, 0)
pi.location = (X(98.0), bcy, H + 3.0)

# ---------------- 网罩：内凹圆角框 + 密集孔 ----------------
# rrect_prism 的矩形本来就在 XZ 面、厚度沿 Y，正对前面板，不需要旋转
GR_W, GR_H, GR_D = 100.0, 24.0, 1.2
GR_CZ = (FRONT_V - 4.0) / 2.0 + 2.0
c = rrect_prism("cutGrille", GR_W, GR_H, 8.0, GR_D + 1.0, None, seg=10)
c.location = (0, -HD + (GR_D - 1.0) / 2.0, GR_CZ)
boolean_cut(body, c)

HV, HF = [], []
R, SEG = 1.35, 14
nx, nz = 21, 5
GY = -HD + GR_D - 0.06
for ix in range(nx):
    for iz in range(nz):
        cx = -(GR_W - 8) / 2.0 + ix * (GR_W - 8) / (nx - 1)
        cz = GR_CZ - (GR_H - 4) / 2.0 + iz * (GR_H - 4) / (nz - 1)
        b0 = len(HV)
        for k in range(SEG):
            a = 2 * math.pi * k / SEG
            HV.append((cx + R * math.cos(a), GY, cz + R * math.sin(a)))
        HF.append(tuple(range(b0, b0 + SEG)))
hm = bpy.data.meshes.new("HoleMesh")
hm.from_pydata(HV, [], HF)
hm.update()
holes = bpy.data.objects.new("GrilleHoles", hm)
bpy.context.collection.objects.link(holes)
holes.data.materials.append(M_HOLE)
print("grille holes", nx * nz)

# ---------------- 屏幕 UI ----------------
FONT = r"C:\Windows\Fonts\msyh.ttc"


def txt(name, body_txt, size, voff, w, material, xoff=0.0):
    cur = bpy.data.curves.new(name, 'FONT')
    cur.body = body_txt
    cur.align_x = 'CENTER'
    cur.align_y = 'CENTER'
    cur.size = size
    try:
        cur.font = bpy.data.fonts.load(FONT)
    except Exception as e:
        print("FONTFAIL", e)
    ob = bpy.data.objects.new(name, cur)
    bpy.context.collection.objects.link(ob)
    ob.rotation_euler = (math.radians(90) - ang, 0, 0)
    ob.location = on_slant(xoff, v_c + voff, w)
    ob.data.materials.append(material)
    return ob


TW = UIW + 0.07
txt("TxtDate", "上午 08:30   10月29日 周三", 3.0, 17.5, TW, M_UIGRAY)
txt("TxtMain", "该吃药了", 10.0, 4.5, TW, M_UIINK, xoff=5.0)
txt("TxtSub", "降压药 1 片", 4.4, -5.0, TW, M_UIGRAY, xoff=5.0)
txt("TxtBtn", "我知道了", 3.8, -15.0, TW + 0.22, M_WHITEP)   # 必须高于 UIBtn 正面

# 药丸小图标（蓝）
ic = rrect_prism("UIPill", 9.0, 4.4, 2.1, 0.3, M_UIBLUE, seg=6,
                 roll=math.radians(38))
ic.rotation_euler = ROT
ic.location = on_slant(-30.0, v_c + 4.0, TW + 0.05)

bpy.ops.mesh.primitive_cube_add(size=1)
sb = bpy.context.object
sb.name = "UIBtn"
sb.scale = (24.0, 0.3, 7.0)
bpy.ops.object.transform_apply(scale=True)
b = sb.modifiers.new("b", 'BEVEL'); b.width = 2.6; b.segments = 4
bpy.context.view_layer.objects.active = sb
bpy.ops.object.modifier_apply(modifier=b.name)
sb.rotation_euler = ROT
sb.location = on_slant(0, v_c - 15.0, TW + 0.02)
sb.data.materials.append(M_UIBLUE)

_c = [-44.0, -13.5, 3.5, 39.0]
_l, _r = _c[0] - 7.0, _c[3] + 12.0
print("BTN span x %.1f..%.1f  center %.1f  margin %.1f/%.1f"
      % (_l, _r, (_l + _r) / 2, _l + HW, HW - _r))
print("OBJ", len(bpy.data.objects), "BODY", [round(v, 1) for v in body.dimensions])
try:
    bpy.ops.wm.save_as_mainfile(
        filepath=r"G://workbuddy//语音助手//deliverables//companion-robot-model.blend")
    print("SAVED blend")
except Exception as e:
    print("SAVEFAIL", e)
