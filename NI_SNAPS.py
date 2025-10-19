bl_info = {
    "name": "NI SNAPS",
    "author": "Wisp",
    "version": (1, 0, 0),
    "blender": (2, 80, 0),
    "location": "3D Viewport > N-Panel > NI SNAPS",
    "description": "Create Snap Points on vertices/edges/faces, asset library (with source templates), origin setting tools, materials assigner, texture resize tools, cleanup tools, and GLB export helpers (single & batch).",
    "category": "3D View",
}
import math
classes = ()
import bpy
import bmesh
import os
from contextlib import contextmanager
from mathutils import Vector, Matrix
from bpy.types import Operator, Panel, PropertyGroup, UIList
from bpy.props import (
    StringProperty, FloatProperty, EnumProperty, PointerProperty, BoolProperty,
    CollectionProperty, IntProperty
)

# --- Refresh wiring: global owner for msgbus subscriptions ---
_NS_MSGBUS_OWNER = object()

def _ns_on_active_change():
    """Msgbus callback when active object changes; rebuild list for new object."""
    try:
        scene = bpy.context.scene
        if scene is not None:
            _ns_refresh_texture_list(scene, force_rebuild=True)
    except Exception as ex:
        print(f"Msgbus error: {ex}")  # Print error to console for debugging

# =====================================================================================
# Settings / Enums
# =====================================================================================

EMPTY_DISPLAY_TYPES = [
    ("PLAIN_AXES", "Plain Axes", "Simple XYZ lines"),
    ("ARROWS", "Arrows", "3D arrows"),
    ("CUBE", "Cube", "Wire cube"),
    ("CIRCLE", "Circle", "Wire circle"),
    ("SPHERE", "Sphere", "Wire sphere"),
    ("CONE", "Cone", "Wire cone"),
    ("SINGLE_ARROW", "Single Arrow", "Single axis arrow"),
]

def _icon_for_type(ty: str) -> str:
    return {
        "MESH": "OUTLINER_OB_MESH",
        "CURVE": "OUTLINER_OB_CURVE",
        "SURFACE": "OUTLINER_OB_SURFACE",
        "META": "OUTLINER_OB_META",
        "FONT": "OUTLINER_OB_FONT",
        "ARMATURE": "OUTLINER_OB_ARMATURE",
        "LATTICE": "OUTLINER_OB_LATTICE",
        "EMPTY": "OUTLINER_OB_EMPTY",
        "LIGHT": "OUTLINER_OB_LIGHT",
        "CAMERA": "OUTLINER_OB_CAMERA",
        "GPENCIL": "OUTLINER_OB_GREASEPENCIL",
        "VOLUME": "OUTLINER_OB_VOLUME",
        "POINTCLOUD": "POINTCLOUD_DATA",
        "LIGHT_PROBE": "OUTLINER_OB_LIGHTPROBE",
    }.get(ty, "OBJECT_DATA")

def enum_all_collections(self, context):
    cols = list(bpy.data.collections)
    if not cols:
        return [("__NONE__", "<No collections>", "No collections found in this file", "ERROR", 0)]
    return [(c.name, c.name, f"Use collection '{c.name}'", "OUTLINER_COLLECTION", i) for i, c in enumerate(cols)]

def enum_assets_in_selected_collection(self, context):
    coll_name = self.library_collection
    coll = bpy.data.collections.get(coll_name) if coll_name and coll_name != "__NONE__" else None
    if not coll:
        return [("__NONE__", "<No library found>", "Collection not found", "ERROR", 0)]

    def _iter_colls(c):
        yield c
        for ch in c.children:
            yield from _iter_colls(ch)

    lib_objects = []
    for c in _iter_colls(coll):
        lib_objects.extend(list(c.objects))

    if not lib_objects:
        return [("__NONE__", "<Library is empty>", "No objects in library", "INFO", 0)]

    items = []
    for idx, obj in enumerate(lib_objects):
        icon = _icon_for_type(obj.type)
        items.append((obj.name, obj.name, f"Add '{obj.name}' from library", icon, idx))
    return items

def enum_material_slot_sources(self, context):
    root = bpy.data.collections.get("MATERIAL SLOTS")
    if not root:
        return [("__NONE__", "<No 'MATERIAL SLOTS' collection>", "Create a collection named 'MATERIAL SLOTS' with material template objects", "ERROR", 0)]

    def _iter_colls(c):
        yield c
        for ch in c.children:
            yield from _iter_colls(ch)

    objs = []
    for c in _iter_colls(root):
        objs.extend(list(c.objects))

    if not objs:
        return [("__NONE__", "<No objects in 'MATERIAL SLOTS'>", "Put at least one object with materials in the collection", "INFO", 0)]

    items = []
    for i, obj in enumerate(objs):
        items.append((obj.name, obj.name, f"Use materials from '{obj.name}'", _icon_for_type(obj.type), i))
    return items

# =====================================================================================
# Texture Resize: Data Model
# =====================================================================================

class NINodeTextureItem(PropertyGroup):
    image_name: StringProperty(name="Image Name", default="")
    width: IntProperty(name="W", default=0)
    height: IntProperty(name="H", default=0)
    file_size: StringProperty(name="File Size", default="")
    selected: BoolProperty(name="Selected", default=False)


def _ns_display_filename_from_image_name(image_name):
    try:
        img = bpy.data.images.get(image_name)
        if img and img.filepath:
            p = bpy.path.abspath(img.filepath)
            import os
            base = os.path.basename(p) if p else None
            if base and base.strip():
                return base
    except Exception:
        pass
    return image_name

class OBJECT_UL_ni_textures(UIList):
    """Texture list with size and file size on the right."""
    bl_idname = "OBJECT_UL_ni_textures"

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            split = layout.split(factor=0.5)
            left = split.row(align=True)
            left.prop(item, "selected", text="")
            left.label(text=_ns_display_filename_from_image_name(item.image_name), icon="IMAGE_DATA")

            right = split.split(factor=0.5)
            middle = right.row()
            middle.alignment = 'CENTER'
            middle.label(text=f"{item.width}x{item.height}")

            far_right = right.row()
            far_right.alignment = 'RIGHT'
            far_right.label(text=item.file_size)
        elif self.layout_type == 'GRID':
            layout.alignment = 'CENTER'
            layout.label(text="")

# =====================================================================================
# Settings
# =====================================================================================

class NISnapSettings(PropertyGroup):
    # --- Creation
    collection_name: StringProperty(name="Root Collection", default="NI SNAPS")
    empty_name: StringProperty(name="Empty Name", default="NI_SNAP")
    display_type: EnumProperty(name="Display Type", items=EMPTY_DISPLAY_TYPES, default="PLAIN_AXES")
    empty_size: FloatProperty(name="Display Size", default=0.8, min=0.1, soft_max=10.0)
    make_subcollections: BoolProperty(name="Sub-collection per object (do not change unless advanced) ", default=True)
    select_created: BoolProperty(name="Select source objects after creating", default=True)
    deselect_vertices: BoolProperty(name="Deselect snap points after creating", default=True)

    # --- Foldouts
    ui_show_creation: BoolProperty(name="Creation Settings", default=False)
    ui_show_library:  BoolProperty(name="Asset Library", default=False)
    ui_show_origin:   BoolProperty(name="Set Object Origin", default=False)
    ui_show_assign:   BoolProperty(name="Assign Materials", default=False)
    ui_show_tex:      BoolProperty(name="Texture Resize", default=False)
    ui_show_create:   BoolProperty(name="Create Snaps", default=False)
    ui_show_cleanup:  BoolProperty(name="Cleanup", default=False)
    ui_show_export:   BoolProperty(name="Export Tools", default=False)

    # --- Asset Library
    library_collection: EnumProperty(
        name="Collection",
        description="Pick which collection acts as the asset library",
        items=enum_all_collections,
    )
    library_active: EnumProperty(
        name="Asset",
        description="Choose an object from the asset library to add",
        items=enum_assets_in_selected_collection,
    )

    # --- Template marker & destination
    template_marker: StringProperty(
        name="Template Suffix",
        description="Suffix used to mark library source templates (e.g. '__SRC')",
        default="__SRC",
    )
    template_target_collection: EnumProperty(
        name="Template Collection",
        description="Where to move the object when marking as a library source. Snaps remain under NI SNAPS.",
        items=enum_all_collections,
    )

    # --- Resize All Snaps
    resize_all_size: FloatProperty(
        name="Size",
        description="Target display size for all NI_SNAP empties in this file",
        default=1.0, min=0.0001, soft_max=100.0
    )

    # --- Export behavior
    hide_after_export: BoolProperty(
        name="Hide After Export",
        description="After a successful export, hide the exported mesh(es) and their related NI SNAP empties",
        default=False
    )

    # --- Batch Export: Proximity include (selected-only scope)
    include_nearby_meshes: BoolProperty(
        name="Include Nearby Meshes",
        description="Batch Export only: also include selected meshes that are within the padding distance of a primary (a selected mesh that has NI_SNAP empties).",
        default=False,
    )
    proximity_padding: FloatProperty(
        name="Proximity Padding (m)",
        description="Distance used to decide if a selected mesh should ride along with a nearby primary during batch export.",
        default=0.2,
        min=0.0, soft_max=5.0
    )


    # --- Materials assigner source
    material_source: EnumProperty(
        name="Material From",
        description="Pick a template object (from 'MATERIAL SLOTS') whose materials will be assigned to selected meshes",
        items=enum_material_slot_sources,
    )

    # --- Texture Resize Settings
    tex_output_dir: StringProperty(
        name="Output Folder",
        description="Where resized images will be saved. Leave empty to use same folder as .blend file",
        subtype='DIR_PATH',
        default=""
    )
    tex_format: EnumProperty(
        name="Format",
        description="Output file format",
        items=[("JPEG", "JPEG", ""), ("PNG", "PNG", "")],
        default="JPEG"
    )
    tex_size_enum: EnumProperty(
        name="Size",
        description="Target width (aspect preserved; never upscale)",
        items=[
            ("264", "264", ""),
            ("512", "512", ""),
            ("1024", "1024", ""),
            ("2048", "2048", ""),
            ("4096", "4096", ""),
        ],
        default="1024"
    )

    texture_items: CollectionProperty(type=NINodeTextureItem)
    texture_index: IntProperty(default=0)
    tex_sig: StringProperty(default="")

# =====================================================================================
# Utilities
# =====================================================================================

def ensure_child_collection(name, parent_coll):
    for child in parent_coll.children:
        if child.name == name:
            return child
    new = bpy.data.collections.new(name)
    parent_coll.children.link(new)
    return new

def ensure_root_collection(context, name):
    coll = bpy.data.collections.get(name)
    if coll is None:
        coll = bpy.data.collections.new(name)
    if coll.name not in [c.name for c in context.scene.collection.children]:
        context.scene.collection.children.link(coll)
    return coll



def move_exported_group_to_collection(context, main_obj, snaps, extras):
    """Disabled: no folder moves. We now only optionally hide after export."""
    return

    def _link_only(obj, dest):
        if not obj or not dest or obj.name not in bpy.data.objects:
            return
        if dest not in obj.users_collection:
            dest.objects.link(obj)
        for c in list(obj.users_collection):
            if c != dest:
                try:
                    c.objects.unlink(obj)
                except Exception:
                    pass

    # Move NI SNAPS sub-collection named <Name> under the bucket (no rename)
    ni_root = bpy.data.collections.get(s.collection_name)
    if ni_root and s.make_subcollections:
        sub = ni_root.children.get(main_obj.name)
        if sub:
            if sub not in bucket.children:
                bucket.children.link(sub)
            try:
                if sub in ni_root.children:
                    ni_root.children.unlink(sub)
            except Exception:
                pass

    # Link objects exclusively to the bucket
    _link_only(main_obj, bucket)
    for e in snaps:
        _link_only(e, bucket)
    for x in extras:
        _link_only(x, bucket)


    def link_only(obj, dest):
        if not obj or not dest or obj.name not in bpy.data.objects:
            return
        if dest not in obj.users_collection:
            dest.objects.link(obj)
        for c in list(obj.users_collection):
            if c != dest:
                try:
                    c.objects.unlink(obj)
                except Exception:
                    pass

    # Move NI SNAPS sub-collection named <Name> under the bucket (no rename)
    ni_root = bpy.data.collections.get(s.collection_name)
    if ni_root and s.make_subcollections:
        sub = ni_root.children.get(main_obj.name)
        if sub:
            if sub not in bucket.children:
                bucket.children.link(sub)
            try:
                if sub in ni_root.children:
                    ni_root.children.unlink(sub)
            except Exception:
                pass

    # Link objects exclusively to the bucket
    link_only(main_obj, bucket)
    for e in snaps:
        link_only(e, bucket)
    for x in extras:
        link_only(x, bucket)

    def link_only(obj, dest):
        if not obj or not dest or obj.name not in bpy.data.objects:
            return
        if dest not in obj.users_collection:
            dest.objects.link(obj)
        for c in list(obj.users_collection):
            if c != dest:
                try:
                    c.objects.unlink(obj)
                except Exception:
                    pass

    # Move NI SNAPS sub-collection named <Name> under the bucket (no rename)
    ni_root = bpy.data.collections.get(s.collection_name)
    if ni_root and s.make_subcollections:
        sub = ni_root.children.get(main_obj.name)
        if sub:
            if sub not in bucket.children:
                bucket.children.link(sub)
            try:
                if sub in ni_root.children:
                    ni_root.children.unlink(sub)
            except Exception:
                pass

    link_only(main_obj, bucket)
    for e in snaps:
        link_only(e, bucket)
    for x in extras:
        link_only(x, bucket)

def iter_collection_tree(coll):
    yield coll
    for child in coll.children:
        yield from iter_collection_tree(child)

def selected_vert_world_coords(obj, bm):
    return [obj.matrix_world @ v.co for v in bm.verts if v.select]

def selected_edge_midpoints_world(obj, bm):
    coords = []
    for e in bm.edges:
        if e.select and len(e.verts) == 2:
            v1, v2 = e.verts
            mid = (v1.co + v2.co) * 0.5
            coords.append(obj.matrix_world @ mid)
    return coords

def selected_face_centers_world(obj, bm):
    coords = []
    for f in bm.faces:
        if f.select:
            coords.append(obj.matrix_world @ f.calc_center_median())
    return coords

def create_empty_at(loc, name, display_type, size, collection, source_name=None):
    empty = bpy.data.objects.new(name, None)
    empty.empty_display_type = display_type
    empty.empty_display_size = size
    empty.scale = (1.0, 1.0, 1.0)
    empty.location = loc
    if source_name:
        try:
            empty["ni_source"] = source_name
        except Exception:
            pass
    collection.objects.link(empty)
    return empty

def world_bbox_center(obj):
    corners_world = [obj.matrix_world @ Vector(corner) for corner in obj.bound_box]
    min_v = Vector((min(v.x for v in corners_world),
                    min(v.y for v in corners_world),
                    min(v.z for v in corners_world)))
    max_v = Vector((max(v.x for v in corners_world),
                    max(v.y for v in corners_world),
                    max(v.z for v in corners_world)))
    return (min_v + max_v) * 0.5

def world_bottom_center_from_boundbox(obj):
    corners_world = [obj.matrix_world @ Vector(corner) for corner in obj.bound_box]
    xs = [v.x for v in corners_world]; ys = [v.y for v in corners_world]; zs = [v.z for v in corners_world]
    return Vector(((min(xs) + max(xs)) * 0.5, (min(ys) + max(ys)) * 0.5, min(zs)))

def world_top_center_from_boundbox(obj):
    corners_world = [obj.matrix_world @ Vector(corner) for corner in obj.bound_box]
    xs = [v.x for v in corners_world]; ys = [v.y for v in corners_world]; zs = [v.z for v in corners_world]
    return Vector(((min(xs) + max(xs)) * 0.5, (min(ys) + max(ys)) * 0.5, max(zs)))

def world_left_mid_from_boundbox(obj):
    corners_world = [obj.matrix_world @ Vector(c) for c in obj.bound_box]
    xs = [v.x for v in corners_world]; ys = [v.y for v in corners_world]; zs = [v.z for v in corners_world]
    return Vector((min(xs), (min(ys) + max(ys)) * 0.5, (min(zs) + max(zs)) * 0.5))

def world_right_mid_from_boundbox(obj):
    corners_world = [obj.matrix_world @ Vector(c) for c in obj.bound_box]
    xs = [v.x for v in corners_world]; ys = [v.y for v in corners_world]; zs = [v.z for v in corners_world]
    return Vector((max(xs), (min(ys) + max(ys)) * 0.5, (min(zs) + max(zs)) * 0.5))

def world_aabb(obj):
    corners = [obj.matrix_world @ Vector(c) for c in obj.bound_box]
    min_v = Vector((min(v.x for v in corners),
                    min(v.y for v in corners),
                    min(v.z for v in corners)))
    max_v = Vector((max(v.x for v in corners),
                    max(v.y for v in corners),
                    max(v.z for v in corners)))
    return min_v, max_v

def aabb_min_distance(a_min, a_max, b_min, b_max):
    # 0.0 if overlapped; otherwise Euclidean gap between boxes
    dx = max(0.0, b_min.x - a_max.x, a_min.x - b_max.x)
    dy = max(0.0, b_min.y - a_max.y, a_min.y - b_max.y)
    dz = max(0.0, b_min.z - a_max.z, a_min.z - b_max.z)
    return math.sqrt(dx*dx + dy*dy + dz*dz)

def inflate_aabb(a_min, a_max, pad):
    return Vector((a_min.x - pad, a_min.y - pad, a_min.z - pad)), Vector((a_max.x + pad, a_max.y + pad, a_max.z + pad))


def _override_to_view3d(context):
    win = context.window
    if not win:
        return None
    screen = win.screen
    for area in screen.areas:
        if area.type == 'VIEW_3D':
            for region in area.regions:
                if region.type == 'WINDOW':
                    return {'window': win, 'screen': screen, 'area': area, 'region': region}
    return None

def strip_marker(name: str, marker: str) -> str:
    if marker and name.endswith(marker):
        return name[: -len(marker)]
    return name

def ensure_unique_object_name(base: str) -> str:
    if base not in bpy.data.objects:
        return base
    i = 2
    while True:
        cand = f"{base}_{i}"
        if cand not in bpy.data.objects:
            return cand
        i += 1

def ensure_unique_collection_name(base: str) -> str:
    if base not in bpy.data.collections:
        return base
    i = 2
    while True:
        cand = f"{base}_{i}"
        if cand not in bpy.data.collections:
            return cand
        i += 1




    def link_only(obj, dest):
        if not obj or not dest or obj.name not in bpy.data.objects:
            return
        if dest not in obj.users_collection:
            dest.objects.link(obj)
        for c in list(obj.users_collection):
            if c != dest:
                try:
                    c.objects.unlink(obj)
                except Exception:
                    pass

    # Move NI SNAPS sub-collection named <Name> under the bucket (no rename)
    ni_root = bpy.data.collections.get(s.collection_name)
    if ni_root and s.make_subcollections:
        sub = ni_root.children.get(main_obj.name)
        if sub:
            if sub not in bucket.children:
                bucket.children.link(sub)
            try:
                if sub in ni_root.children:
                    ni_root.children.unlink(sub)
            except Exception:
                pass

    link_only(main_obj, bucket)
    for e in snaps:
        link_only(e, bucket)
    for x in extras:
        link_only(x, bucket)
    def _link_only(obj, dest_coll):
        if not obj or not dest_coll or obj.name not in bpy.data.objects:
            return
        if dest_coll not in obj.users_collection:
            dest_coll.objects.link(obj)
        for c in list(obj.users_collection):
            if c != dest_coll:
                try:
                    c.objects.unlink(obj)
                except Exception:
                    pass

    # Try to move the NI SNAPS sub-collection folder itself if it exists
    ni_root = bpy.data.collections.get(s.collection_name)
    if ni_root and s.make_subcollections:
        sub = ni_root.children.get(main_obj.name)
        if sub:
            # Link under exported bucket and unlink from NI root
            if sub not in bucket.children:
                bucket.children.link(sub)
            try:
                if sub in ni_root.children:
                    ni_root.children.unlink(sub)
            except Exception:
                pass

    # Move objects into the bucket (mesh, snaps, extras)
    _link_only(main_obj, bucket)
    for e in snaps:
        _link_only(e, bucket)
    for x in extras:
        _link_only(x, bucket)
    def link_object_only_to_collection(obj, dest_coll):
        if not obj or not dest_coll or obj.name not in bpy.data.objects:
            return
        if dest_coll not in obj.users_collection:
            dest_coll.objects.link(obj)
        for c in list(obj.users_collection):
            if c != dest_coll:
                try:
                    c.objects.unlink(obj)
                except Exception:
                    pass

    # Try to move the NI SNAPS sub-collection folder itself if it exists
    ni_root = bpy.data.collections.get(s.collection_name)
    if ni_root and s.make_subcollections:
        sub = ni_root.children.get(main_obj.name)
        if sub:
            # Link under exported bucket and unlink from NI root
            if sub not in bucket.children:
                bucket.children.link(sub)
            try:
                if sub in ni_root.children:
                    ni_root.children.unlink(sub)
            except Exception:
                pass

    # Move objects into the bucket (mesh, snaps, extras)
    link_object_only_to_collection(main_obj, bucket)
    for e in snaps:
        link_object_only_to_collection(e, bucket)
    for x in extras:
        link_object_only_to_collection(x, bucket)






def link_object_only_to_collection(obj, dest_coll):
    """Link obj exclusively to dest_coll (unlink from any other collections)."""
    try:
        if obj is None or dest_coll is None:
            return
        if obj.name not in bpy.data.objects:
            return
        if dest_coll not in obj.users_collection:
            dest_coll.objects.link(obj)
        for c in list(obj.users_collection):
            if c != dest_coll:
                try:
                    c.objects.unlink(obj)
                except Exception:
                    pass
    except Exception as ex:
        print("[NI SNAPS] link_object_only_to_collection failed:", ex)


def iter_all_under(coll):
    for c in iter_collection_tree(coll):
        for o in c.objects:
            yield o

def get_related_empties_for_object(obj, settings, root_coll):
    """Find snaps for an object."""
    pref = settings.empty_name
    related = []

    if not root_coll:
        return related

    if settings.make_subcollections:
        sub = root_coll.children.get(obj.name)
        if sub:
            for e in iter_all_under(sub):
                if e.type == 'EMPTY' and e.name.startswith(pref):
                    related.append(e)
            if related:
                return related
        for e in iter_all_under(root_coll):
            if e.type == 'EMPTY' and e.name.startswith(pref) and e.get("ni_source") == obj.name:
                related.append(e)
        if related:
            return related
        return []

    for e in root_coll.objects:
        if e.type == 'EMPTY' and e.name.startswith(pref) and e.get("ni_source") == obj.name:
            related.append(e)
    if related:
        return related

    corners = [obj.matrix_world @ Vector(c) for c in obj.bound_box]
    minx = min(v.x for v in corners); maxx = max(v.x for v in corners)
    miny = min(v.y for v in corners); maxy = max(v.y for v in corners)
    minz = min(v.z for v in corners); maxz = max(v.z for v in corners)
    for e in root_coll.objects:
        if e.type == 'EMPTY' and e.name.startswith(pref):
            p = e.location
            if (minx - 1e-6) <= p.x <= (maxx + 1e-6) and \
               (miny - 1e-6) <= p.y <= (maxy + 1e-6) and \
               (minz - 1e-6) <= p.z <= (maxz + 1e-6):
                related.append(e)
    return related

@contextmanager
def preserve_cursor(context):
    cursor = context.scene.cursor
    backup = cursor.location.copy()
    try:
        yield cursor
    finally:
        cursor.location = backup

def ensure_object_mode():
    try:
        bpy.ops.object.mode_set(mode="OBJECT")
    except Exception:
        pass

# =====================================================================================
# Texture Resize: Refresh Logic


def _img_display_name(img):
    try:
        if img and img.filepath:
            import os, bpy
            p = bpy.path.abspath(img.filepath)
            return os.path.basename(p) if p else img.name
    except Exception:
        pass
    return getattr(img, "name", "")

# =====================================================================================

def _get_image_file_size(img):
    """Get human-readable file size for an image."""
    if not img:
        return "N/A"
    if img.filepath:
        try:
            filepath = bpy.path.abspath(img.filepath)
            if os.path.isfile(filepath):
                size_bytes = os.path.getsize(filepath)
                if size_bytes < 1024:
                    return f"{size_bytes}B"
                elif size_bytes < 1024 * 1024:
                    return f"{size_bytes / 1024:.1f}KB"
                else:
                    return f"{size_bytes / (1024 * 1024):.2f}MB"
        except Exception as e:
            print(f"Error getting file size for {getattr(img, 'name', '?')} at {img.filepath}: {e}")
    if getattr(img, "packed_file", None):
        size_bytes = img.packed_file.size
        if size_bytes < 1024:
            return f"{size_bytes}B"
        elif size_bytes < 1024 * 1024:
            return f"{size_bytes / 1024:.1f}KB"
        else:
            return f"{size_bytes / (1024 * 1024):.2f}MB"
    return "No file"

def _probe_dims_from_disk(img: bpy.types.Image):
    """
    Return (w, h) by reading the file on disk to avoid stale .size after relinks.
    Falls back to img.size if disk probe fails (packed/missing).
    """
    if img and img.filepath:
        try:
            path = bpy.path.abspath(img.filepath)
            if os.path.isfile(path):
                tmp = bpy.data.images.load(path, check_existing=False)
                w = int(tmp.size[0]) if tmp.size else 0
                h = int(tmp.size[1]) if tmp.size else 0
                bpy.data.images.remove(tmp, do_unlink=True)
                if w > 0 and h > 0:
                    return w, h
        except Exception:
            pass
    try:
        if img and img.size:
            return int(img.size[0]), int(img.size[1])
    except Exception:
        pass
    return 0, 0

def _ns_build_selection_signature():
    """Produce a string signature representing current selection's texture set (by datablock name)."""
    names = []
    for ob in bpy.context.selected_objects:
        if ob.type != 'MESH':
            continue
        for slot in ob.material_slots:
            mat = slot.material
            if not mat:
                continue
            nt = getattr(mat, "node_tree", None)
            if not nt:
                continue
            for node in nt.nodes:
                if node.type == 'TEX_IMAGE' and node.image:
                    names.append(node.image.name)
    names = sorted(set(names))
    return "|".join(names)

def _ns_refresh_texture_list(scene, force_rebuild=False):
    """Sync Scene.ni_snap_settings.texture_items with current selection (and refresh all rows)."""
    s = getattr(scene, "ni_snap_settings", None)
    if not s:
        return

    sig = _ns_build_selection_signature()
    new_names = sig.split("|") if sig else []

    # Update in place if the set is unchanged
    old_names = [it.image_name for it in s.texture_items]
    if not force_rebuild and new_names == old_names and sig == s.tex_sig:
        for it in s.texture_items:
            img = bpy.data.images.get(it.image_name)
            w, h = _probe_dims_from_disk(img)
            it.width, it.height = w, h
            it.file_size = _get_image_file_size(img)
        return

    # Rebuild (preserving selection/active where possible)
    prev_selected = {it.image_name: bool(it.selected) for it in s.texture_items}
    prev_active_name = s.texture_items[s.texture_index].image_name if (0 <= s.texture_index < len(s.texture_items)) else None

    s.texture_items.clear()
    for nm in new_names:
        img = bpy.data.images.get(nm)
        it = s.texture_items.add()
        it.image_name = nm
        w, h = _probe_dims_from_disk(img)
        it.width, it.height = w, h
        it.file_size = _get_image_file_size(img)
        it.selected = prev_selected.get(nm, False)

    s.texture_index = 0
    if prev_active_name and prev_active_name in new_names:
        s.texture_index = new_names.index(prev_active_name)

    s.tex_sig = sig

def _ns_depsgraph_update(scene, depsgraph):
    try:
        _ns_refresh_texture_list(scene)
    except Exception:
        pass

# =====================================================================================
# Core creation
# =====================================================================================

def create_snaps_for_selection(context, element_mode: str):
    s = context.scene.ni_snap_settings
    root = ensure_root_collection(context, s.collection_name)

    sel_meshes = [o for o in context.selected_objects if o.type == "MESH"]
    if not sel_meshes:
        return 0, {}

    view_layer = context.view_layer
    orig_active = view_layer.objects.active
    orig_mode = orig_active.mode if orig_active else "OBJECT"

    total_added = 0

    for obj in sel_meshes:
        view_layer.objects.active = obj
        target_coll = ensure_child_collection(obj.name, root) if s.make_subcollections else root

        try:
            bpy.ops.object.mode_set(mode="EDIT")
            bm = bmesh.from_edit_mesh(obj.data)
            bm.verts.ensure_lookup_table()
            bm.edges.ensure_lookup_table()
            bm.faces.ensure_lookup_table()

            if element_mode == 'VERT':
                world_coords = selected_vert_world_coords(obj, bm)
                if s.deselect_vertices:
                    for v in bm.verts:
                        if v.select:
                            v.select = False
                    bmesh.update_edit_mesh(obj.data)
            elif element_mode == 'EDGE':
                world_coords = selected_edge_midpoints_world(obj, bm)
                if s.deselect_vertices:
                    for e in bm.edges:
                        if e.select:
                            e.select = False
                    bmesh.update_edit_mesh(obj.data)
            elif element_mode == 'FACE':
                world_coords = selected_face_centers_world(obj, bm)
                if s.deselect_vertices:
                    for f in bm.faces:
                        if f.select:
                            f.select = False
                    bmesh.update_edit_mesh(obj.data)
            else:
                world_coords = []
        finally:
            bpy.ops.object.mode_set(mode="OBJECT")

        for wc in world_coords:
            create_empty_at(
                wc, s.empty_name, s.display_type, s.empty_size, target_coll, source_name=obj.name
            )
            total_added += 1

    if s.select_created and sel_meshes:
        for ob in context.selected_objects:
            ob.select_set(False)
        last = None
        for ob in sel_meshes:
            ob.select_set(True)
            last = ob
        if last:
            view_layer.objects.active = last
        try:
            bpy.ops.object.mode_set(mode="OBJECT")
        except Exception:
            pass
    else:
        if orig_active and orig_active.name in bpy.data.objects:
            view_layer.objects.active = orig_active
            try:
                bpy.ops.object.mode_set(mode=orig_mode)
            except Exception:
                bpy.ops.object.mode_set(mode="OBJECT")

    return total_added, {}

# =====================================================================================
# Clean helpers
# =====================================================================================

def _unlink_from_all_parents(coll):
    for parent in list(bpy.data.collections):
        for ch in list(parent.children):
            if ch == coll:
                parent.children.unlink(coll)
    for scene in bpy.data.scenes:
        for ch in list(scene.collection.children):
            if ch == coll:
                scene.collection.children.unlink(coll)

def _remove_collection_tree_if_empty(coll):
    if not coll:
        return False
    for child in list(coll.children):
        _remove_collection_tree_if_empty(child)
    if len(coll.objects) == 0 and len(coll.children) == 0:
        _unlink_from_all_parents(coll)
        bpy.data.collections.remove(coll)
        return True
    return False

# =====================================================================================
# Operators: Create / Clean
# =====================================================================================

class OBJECT_OT_ni_snaps_create_vertices(Operator):
    bl_idname = "object.ni_snaps_create_vertices"
    bl_label = "Create on Vertices"
    bl_options = {"REGISTER", "UNDO"}
    def execute(self, context):
        total, _ = create_snaps_for_selection(context, 'VERT')
        self.report({"INFO"}, f"Created {total} empties on vertices.")
        return {"FINISHED"} if total > 0 else {"CANCELLED"}

class OBJECT_OT_ni_snaps_create_edges(Operator):
    bl_idname = "object.ni_snaps_create_edges"
    bl_label = "Create on Edges"
    bl_options = {"REGISTER", "UNDO"}
    def execute(self, context):
        total, _ = create_snaps_for_selection(context, 'EDGE')
        self.report({"INFO"}, f"Created {total} empties on edges.")
        return {"FINISHED"} if total > 0 else {"CANCELLED"}

class OBJECT_OT_ni_snaps_create_faces(Operator):
    bl_idname = "object.ni_snaps_create_faces"
    bl_label = "Create on Faces"
    bl_options = {"REGISTER", "UNDO"}
    def execute(self, context):
        total, _ = create_snaps_for_selection(context, 'FACE')
        self.report({"INFO"}, f"Created {total} empties on faces.")
        return {"FINISHED"} if total > 0 else {"CANCELLED"}

class OBJECT_OT_ni_snaps_clean(Operator):
    bl_idname = "object.ni_snaps_clean"
    bl_label = "Delete Selected Objects Snaps"
    bl_options = {"REGISTER", "UNDO"}
    def execute(self, context):
        s = context.scene.ni_snap_settings
        root = bpy.data.collections.get(s.collection_name)
        if root is None:
            self.report({"INFO"}, f"No collection named '{s.collection_name}' found.")
            return {"CANCELLED"}
        sel_objs = [o for o in context.selected_objects if o.type == "MESH"]
        to_delete = []
        subs_to_check = []
        for obj in sel_objs:
            target_colls = [root]
            if s.make_subcollections:
                sub = root.children.get(obj.name)
                if sub:
                    target_colls = list(iter_collection_tree(sub))
                    subs_to_check.append(sub)
            for coll in target_colls:
                for candidate in list(coll.objects):
                    if candidate.type == 'EMPTY' and candidate.name.startswith(s.empty_name):
                        to_delete.append(candidate)
        for ob in to_delete:
            bpy.data.objects.remove(ob, do_unlink=True)
        removed_cols = 0
        if s.make_subcollections:
            for sub in set(subs_to_check):
                if sub and sub.name != s.collection_name:
                    if _remove_collection_tree_if_empty(sub):
                        removed_cols += 1
        self.report({"INFO"}, f"Deleted {len(to_delete)} empties; removed {removed_cols} empty collection(s).")
        return {"FINISHED"}

class OBJECT_OT_ni_snaps_purge_orphans(Operator):
    bl_idname = "object.ni_snaps_purge_orphans"
    bl_label = "Delete Empty Snaps + Collections"
    bl_options = {"REGISTER", "UNDO"}
    def execute(self, context):
        s = context.scene.ni_snap_settings
        root = bpy.data.collections.get(s.collection_name)
        if not root:
            self.report({'INFO'}, "NI SNAPS root collection not found; nothing to purge.")
            return {'CANCELLED'}

        deleted = 0
        for coll in list(iter_collection_tree(root)):
            for ob in list(coll.objects):
                if ob.type == 'EMPTY' and ob.name.startswith(s.empty_name):
                    src = ob.get("ni_source")
                    if (not src) or (src not in bpy.data.objects):
                        try:
                            bpy.data.objects.remove(ob, do_unlink=True)
                            deleted += 1
                        except Exception:
                            pass

        removed_cols = 0
        for sub in list(root.children):
            if _remove_collection_tree_if_empty(sub):
                removed_cols += 1

        self.report({'INFO'}, f"Purged {deleted} orphan snaps; removed {removed_cols} empty collection(s).")
        return {'FINISHED'}

# =====================================================================================
# Assign Materials
# =====================================================================================

class OBJECT_OT_ni_snaps_assign_materials(Operator):
    bl_idname = "object.ni_snaps_assign_materials"
    bl_label = "Assign Materials To Selected"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        s = context.scene.ni_snap_settings
        src_name = s.material_source
        if not src_name or src_name == "__NONE__":
            self.report({'WARNING'}, "Pick a material source object from 'MATERIAL SLOTS'.")
            return {'CANCELLED'}

        src = bpy.data.objects.get(src_name)
        if not src or not getattr(src, "data", None):
            self.report({'WARNING'}, f"Material source '{src_name}' not found or has no data.")
            return {'CANCELLED'}

        src_mats = [m for m in getattr(src.data, "materials", []) if m]
        if not src_mats:
            src_mats = [sl.material for sl in src.material_slots if sl.material]

        if not src_mats:
            self.report({'WARNING'}, f"'{src_name}' has no materials to assign.")
            return {'CANCELLED'}

        targets = [o for o in context.selected_objects if o.type == "MESH"]
        if not targets:
            self.report({'WARNING'}, "Select one or more mesh objects to assign materials to.")
            return {'CANCELLED'}

        ensure_object_mode()

        changed = 0
        for obj in targets:
            me = obj.data
            try:
                me.materials.clear()
            except Exception:
                for i in range(len(me.materials)):
                    me.materials[i] = None
                while len(me.materials) > 0:
                    try:
                        me.materials.pop(index=0)
                    except Exception:
                        break
            for m in src_mats:
                me.materials.append(m)
            changed += 1

        self.report({'INFO'}, f"Assigned {len(src_mats)} material(s) from '{src_name}' to {changed} object(s).")
        return {'FINISHED'}

# =====================================================================================
# Origin Operators
# =====================================================================================

class OBJECT_OT_ni_snaps_origin_bottom_center(Operator):
    bl_idname = "object.ni_snaps_origin_bottom_center"
    bl_label = "Origin to Bottom"
    bl_options = {"REGISTER", "UNDO"}
    def execute(self, context):
        with preserve_cursor(context):
            ensure_object_mode()
            sel_meshes = [o for o in context.selected_objects if o.type == "MESH"]
            for obj in sel_meshes:
                for o in context.selected_objects: o.select_set(False)
                obj.select_set(True); context.view_layer.objects.active = obj
                context.scene.cursor.location = world_bottom_center_from_boundbox(obj)
                bpy.ops.object.origin_set(type='ORIGIN_CURSOR', center='MEDIAN')
            for o in sel_meshes: o.select_set(True)
        return {"FINISHED"}

class OBJECT_OT_ni_snaps_origin_top_center(Operator):
    bl_idname = "object.ni_snaps_origin_top_center"
    bl_label = "Origin to Top"
    bl_options = {"REGISTER", "UNDO"}
    def execute(self, context):
        with preserve_cursor(context):
            ensure_object_mode()
            sel_meshes = [o for o in context.selected_objects if o.type == "MESH"]
            for obj in sel_meshes:
                for o in context.selected_objects: o.select_set(False)
                obj.select_set(True); context.view_layer.objects.active = obj
                context.scene.cursor.location = world_top_center_from_boundbox(obj)
                bpy.ops.object.origin_set(type='ORIGIN_CURSOR', center='MEDIAN')
            for o in sel_meshes: o.select_set(True)
        return {"FINISHED"}

class OBJECT_OT_ni_snaps_origin_center(Operator):
    bl_idname = "object.ni_snaps_origin_center"
    bl_label = "Origin to Center"
    bl_options = {"REGISTER", "UNDO"}
    def execute(self, context):
        with preserve_cursor(context):
            ensure_object_mode()
            sel_meshes = [o for o in context.selected_objects if o.type == "MESH"]
            for obj in sel_meshes:
                for o in context.selected_objects: o.select_set(False)
                obj.select_set(True); context.view_layer.objects.active = obj
                context.scene.cursor.location = world_bbox_center(obj)
                bpy.ops.object.origin_set(type='ORIGIN_CURSOR', center='MEDIAN')
            for o in sel_meshes: o.select_set(True)
        return {"FINISHED"}

class OBJECT_OT_ni_snaps_origin_cursor(Operator):
    bl_idname = "object.ni_snaps_origin_cursor"
    bl_label = "Origin to Cursor"
    bl_options = {"REGISTER", "UNDO"}
    def execute(self, context):
        bpy.ops.object.origin_set(type='ORIGIN_CURSOR', center='MEDIAN')
        return {"FINISHED"}

class OBJECT_OT_ni_snaps_origin_world_zero(Operator):
    bl_idname = "object.ni_snaps_origin_world_zero"
    bl_label = "Origin to World Zero"
    bl_options = {"REGISTER", "UNDO"}
    def execute(self, context):
        with preserve_cursor(context) as cursor:
            cursor.location = Vector((0.0, 0.0, 0.0))
            bpy.ops.object.origin_set(type='ORIGIN_CURSOR', center='MEDIAN')
        return {"FINISHED"}

class OBJECT_OT_ni_snaps_origin_left_mid(Operator):
    bl_idname = "object.ni_snaps_origin_left_mid"
    bl_label = "Origin to Left"
    bl_options = {"REGISTER", "UNDO"}
    def execute(self, context):
        with preserve_cursor(context):
            ensure_object_mode()
            sel_meshes = [o for o in context.selected_objects if o.type == "MESH"]
            for obj in sel_meshes:
                for o in context.selected_objects: o.select_set(False)
                obj.select_set(True); context.view_layer.objects.active = obj
                context.scene.cursor.location = world_left_mid_from_boundbox(obj)
                bpy.ops.object.origin_set(type='ORIGIN_CURSOR', center='MEDIAN')
            for o in sel_meshes: o.select_set(True)
        return {"FINISHED"}

class OBJECT_OT_ni_snaps_origin_right_mid(Operator):
    bl_idname = "object.ni_snaps_origin_right_mid"
    bl_label = "Origin to Right"
    bl_options = {"REGISTER", "UNDO"}
    def execute(self, context):
        with preserve_cursor(context):
            ensure_object_mode()
            sel_meshes = [o for o in context.selected_objects if o.type == "MESH"]
            for obj in sel_meshes:
                for o in context.selected_objects: o.select_set(False)
                obj.select_set(True); context.view_layer.objects.active = obj
                context.scene.cursor.location = world_right_mid_from_boundbox(obj)
                bpy.ops.object.origin_set(type='ORIGIN_CURSOR', center='MEDIAN')
            for o in sel_meshes: o.select_set(True)
        return {"FINISHED"}

class OBJECT_OT_ni_snaps_origin_to_selected(Operator):
    bl_idname = "object.ni_snaps_origin_to_selected"
    bl_label = "Origin to Selected"
    bl_options = {"REGISTER", "UNDO"}
    def execute(self, context):
        orig_mode = context.mode
        with preserve_cursor(context):
            override = _override_to_view3d(context)
            try:
                if override:
                    with context.temp_override(**override):
                        bpy.ops.view3d.snap_cursor_to_selected()
                else:
                    bpy.ops.view3d.snap_cursor_to_selected()
            except Exception:
                self.report({'WARNING'}, "Could not snap cursor to selected.")
                return {'CANCELLED'}
            ensure_object_mode()
            if not context.selected_objects:
                self.report({'WARNING'}, "No objects selected.")
                return {'CANCELLED'}
            bpy.ops.object.origin_set(type='ORIGIN_CURSOR', center='MEDIAN')
        try:
            if orig_mode and orig_mode.startswith('EDIT'):
                bpy.ops.object.mode_set(mode='EDIT')
        except Exception:
            pass
        self.report({'INFO'}, "Origin set to selection. Cursor & mode restored.")
        return {'FINISHED'}

# =====================================================================================
# Export Operators
# =====================================================================================

def _ensure_glb_path(path: str) -> str:
    base, ext = os.path.splitext(path)
    return base + ".glb" if ext.lower() != ".glb" else path

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        s = context.scene.ni_snap_settings
        root = bpy.data.collections.get(s.collection_name)

        ensure_object_mode()
        view = context.view_layer

        sel_meshes = [o for o in context.selected_objects if o.type == "MESH"]
        if not sel_meshes:
            self.report({'WARNING'}, "No mesh objects selected.")
            return {'CANCELLED'}

        ref = view.objects.active if (view.objects.active in sel_meshes) else sel_meshes[0]
        ref_origin_world = ref.matrix_world.translation.copy()

        out_dir = self.directory or (os.path.dirname(self.filepath) if self.filepath else bpy.path.abspath("//"))
        out_dir_abs = bpy.path.abspath(out_dir)
        if not os.path.isdir(out_dir_abs):
            self.report({'ERROR'}, f"Output directory is invalid: {out_dir}")
            return {'CANCELLED'}

        clean_name = strip_marker(ref.name, s.template_marker)
        safe_name = bpy.path.clean_name(clean_name)
        full_path = _ensure_glb_path(os.path.join(out_dir_abs, safe_name))

        snaps = set()
        for obj in sel_meshes:
            for e in get_related_empties_for_object(obj, s, root):
                snaps.add(e)

        orig_selected = list(context.selected_objects)
        orig_active = view.objects.active

        loc_backup = {}
        for obj in sel_meshes:
            loc_backup[obj] = obj.location.copy()
        for e in snaps:
            loc_backup[e] = e.location.copy()

        local_shift = {}
        exported_ok = False
        try:
            for obj in sel_meshes:
                obj_origin = obj.matrix_world.translation
                world_delta = ref_origin_world - obj_origin
                local_delta = obj.matrix_world.inverted().to_3x3() @ world_delta
                obj.data.transform(Matrix.Translation(-local_delta))
                obj.data.update()
                obj.location += world_delta
                local_shift[obj] = local_delta

            offset = -ref_origin_world
            for obj in sel_meshes: obj.location += offset
            for e in snaps: e.location += offset

            for o in context.selected_objects: o.select_set(False)
            for obj in sel_meshes: obj.select_set(True)
            for e in snaps: e.select_set(True)
            view.objects.active = ref

            bpy.ops.export_scene.gltf(
                'EXEC_DEFAULT',
                export_format='GLB',
                use_selection=True,
                filepath=bpy.path.abspath(full_path)
            )
            exported_ok = True

        except Exception as ex:
            self.report({'ERROR'}, f"Export failed: {ex}")

        try:
            ensure_object_mode()
            for o2, loc in loc_backup.items():
                if o2 and o2.name in bpy.data.objects:
                    o2.location = loc
            for obj, ldelta in local_shift.items():
                if obj and obj.name in bpy.data.objects:
                    obj.data.transform(Matrix.Translation(ldelta))
                    obj.data.update()
        finally:
            for o in context.selected_objects: o.select_set(False)
            for o in orig_selected:
                if o and o.name in bpy.data.objects:
                    o.select_set(True)
            if orig_active and orig_active.name in bpy.data.objects:
                view.objects.active = orig_active

        
        # Always file exported items under Exported Meshes/<PrimaryName>
        try:
            # 'ref' is the primary object we centered/named the export on
            extras = [o for o in sel_meshes if o != ref]
            move_exported_group_to_collection(context, ref, list(snaps), extras)
        except Exception:
            # Never let filing fail the export
            pass

        if exported_ok and s.hide_after_export:
            for obj in sel_meshes:
                if obj and obj.name in bpy.data.objects:
                    obj.hide_set(True)
            for e in snaps:
                if e and e.name in bpy.data.objects:
                    e.hide_set(True)
            # Never let filing fail the export
            pass

        if exported_ok:
            self.report({'INFO'}, f"Exported: {bpy.path.abspath(full_path)}")
            return {'FINISHED'}
        else:
            return {'CANCELLED'}


class OBJECT_OT_ni_snaps_batch_export_glb(Operator):
    bl_idname = "object.ni_snaps_batch_export_glb"
    bl_label = "Batch Export as GLB"
    bl_options = {"REGISTER", "UNDO"}

    directory: StringProperty(name="Output Directory", subtype='DIR_PATH', default="")
    filepath: StringProperty(name="File Path", subtype='FILE_PATH', default="")

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def _export_group(self, context, out_dir_abs, main_obj, snaps, extras, hide_after=False):
        """Exports (main_obj + snaps + extras) centered at main_obj, then restores state."""
        ensure_object_mode()
        view = context.view_layer

        # Backup locations for restore
        loc_backup = {main_obj: main_obj.location.copy()}
        for e in snaps:
            loc_backup[e] = e.location.copy()
        for x in extras:
            loc_backup[x] = x.location.copy()

        # Move group to origin of the main obj
        offset = -main_obj.location
        main_obj.location += offset
        for e in snaps: e.location += offset
        for x in extras: x.location += offset

        # Build selection for export
        for o in context.selected_objects: o.select_set(False)
        main_obj.select_set(True)
        for e in snaps: e.select_set(True)
        for x in extras: x.select_set(True)
        view.objects.active = main_obj

        clean = strip_marker(main_obj.name, context.scene.ni_snap_settings.template_marker)
        fname = bpy.path.clean_name(clean) + ".glb"
        full_path = os.path.join(out_dir_abs, fname)

        success = False
        try:
            bpy.ops.export_scene.gltf(
                'EXEC_DEFAULT',
                export_format='GLB',
                use_selection=True,
                filepath=bpy.path.abspath(full_path)
            )
            success = True
        except Exception as ex:
            self.report({'WARNING'}, f"Failed to export {main_obj.name}: {ex}")
        finally:
            # Restore positions
            for o2, loc in loc_backup.items():
                if o2 and o2.name in bpy.data.objects:
                    o2.location = loc

        # Move exported items into Exported Meshes/<PrimaryName>_EXP
        if success:
            try:
                move_exported_group_to_collection(context, main_obj, snaps, extras)
            except Exception:
                pass

        # Hide-after: hide primary + snaps + extras (viewport-only) AFTER moving
        if success and hide_after:
            if main_obj and main_obj.name in bpy.data.objects:
                main_obj.hide_set(True)
            for e in snaps:
                if e and e.name in bpy.data.objects:
                    e.hide_set(True)
            for x in extras:
                if x and x.name in bpy.data.objects:
                    x.hide_set(True)

        return success

    def execute(self, context):
        s = context.scene.ni_snap_settings
        root = bpy.data.collections.get(s.collection_name)

        out_dir = self.directory or (os.path.dirname(self.filepath) if self.filepath else bpy.path.abspath("//"))
        out_dir_abs = bpy.path.abspath(out_dir)
        if not os.path.isdir(out_dir_abs):
            self.report({'ERROR'}, f"Output directory is invalid: {out_dir}")
            return {'CANCELLED'}

        sel_meshes = [o for o in context.selected_objects if o.type == "MESH"]
        if not sel_meshes:
            self.report({"WARNING"}, "No mesh objects selected.")
            return {"CANCELLED"}

        ensure_object_mode()
        view = context.view_layer
        orig_active = view.objects.active
        orig_selected = list(context.selected_objects)

        # === If proximity feature is OFF, fall back to original per-object export ===
        if not s.include_nearby_meshes:
            exported = 0
            for obj in sel_meshes:
                snaps = get_related_empties_for_object(obj, s, root)
                if self._export_group(context, out_dir_abs, obj, snaps, [], hide_after=s.hide_after_export):
                    exported += 1

            # Restore selection/active
            for o in context.selected_objects: o.select_set(False)
            for o in orig_selected:
                if o and o.name in bpy.data.objects:
                    o.select_set(True)
            if orig_active and orig_active.name in bpy.data.objects:
                view.objects.active = orig_active

            self.report({'INFO'}, f"Batch exported {exported} GLB file(s) to: {out_dir_abs}")
            return {'FINISHED'}

        # ===== Proximity path (selected-only scope, closest primary wins) =====
        padding = float(max(0.0, s.proximity_padding))

        # Primaries: selected meshes that have NI SNAP empties
        primaries = [o for o in sel_meshes if get_related_empties_for_object(o, s, root)]
        if not primaries:
            self.report({'INFO'}, "No primaries with NI_SNAP empties found; exporting all selected individually (no proximity).")
            s.include_nearby_meshes = False
            return self.execute(context)

        extras_candidates = [o for o in bpy.data.objects if o.type == "MESH" and not o.hide_get() and o not in primaries]

        # Precompute AABBs
        p_aabbs = {p: world_aabb(p) for p in primaries}
        e_aabbs = {e: world_aabb(e) for e in extras_candidates}

        # Assign each extra to the CLOSEST primary within padding (AABB distance)
        assignment = {p: set() for p in primaries}
        for extra, (emin, emax) in e_aabbs.items():
            best_p, best_d = None, None
            for p in primaries:
                pmin, pmax = p_aabbs[p]
                pmin_inf, pmax_inf = inflate_aabb(pmin, pmax, padding)
                d = aabb_min_distance(pmin_inf, pmax_inf, emin, emax)
                if d <= padding:
                    if (best_d is None) or (d < best_d):
                        best_d, best_p = d, p
            if best_p:
                assignment[best_p].add(extra)

        # Export per-primary with its own snaps + assigned extras
        exported = 0
        for obj in primaries:
            snaps = get_related_empties_for_object(obj, s, root)
            extras = list(assignment.get(obj, set()))
            if self._export_group(context, out_dir_abs, obj, snaps, extras, hide_after=s.hide_after_export):
                exported += 1

        # Restore selection/active
        for o in context.selected_objects: o.select_set(False)
        for o in orig_selected:
            if o and o.name in bpy.data.objects:
                o.select_set(True)
        if orig_active and orig_active.name in bpy.data.objects:
            view.objects.active = orig_active

        self.report({'INFO'}, f"Batch exported {exported} GLB file(s) to: {out_dir_abs}")
        return {'FINISHED'}

# =====================================================================================
# Asset Library operators
# =====================================================================================

def _find_in_collection_tree(coll, name):
    for o in coll.objects:
        if o.name == name:
            return o
    for ch in coll.children:
        found = _find_in_collection_tree(ch, name)
        if found:
            return found
    return None

def _active_object_or_first_selected(context):
    view = context.view_layer
    if view.objects.active:
        return view.objects.active
    if context.selected_objects:
        return context.selected_objects[0]
    return None

class OBJECT_OT_ni_snaps_add_library_copy(Operator):
    bl_idname = "object.ni_snaps_add_library_copy"
    bl_label = "Add Library Asset (Copy)"
    bl_options = {"REGISTER", "UNDO"}
    def execute(self, context):
        s = context.scene.ni_snap_settings
        lib_name = s.library_collection
        obj_name = s.library_active
        if not lib_name or lib_name == "__NONE__":
            self.report({'WARNING'}, "No asset library collection selected.")
            return {'CANCELLED'}
        coll = bpy.data.collections.get(lib_name)
        if not coll:
            self.report({'WARNING'}, f"Asset library collection '{lib_name}' not found.")
            return {'CANCELLED'}
        if not obj_name or obj_name == "__NONE__":
            self.report({'WARNING'}, "No asset chosen.")
            return {'CANCELLED'}
        src = bpy.data.objects.get(obj_name)
        if not src:
            self.report({'WARNING'}, f"Object '{obj_name}' not found in this file.")
            return {'CANCELLED'}

        base_name = strip_marker(src.name, s.template_marker)
        desired = ensure_unique_object_name(base_name)

        new = src.copy()
        if getattr(src, "data", None):
            new.data = src.data.copy()
        target_coll = context.collection or context.scene.collection
        target_coll.objects.link(new)

        new.name = desired
        drop_loc = context.scene.cursor.location.copy()
        delta = drop_loc - src.location
        new.location = drop_loc

        root_snaps = ensure_root_collection(context, s.collection_name)
        lib_snaps = []

        if s.make_subcollections and root_snaps:
            sub = root_snaps.children.get(src.name)
            if sub:
                for c in iter_collection_tree(sub):
                    for e in c.objects:
                        if e.type == 'EMPTY' and e.name.startswith(s.empty_name):
                            lib_snaps.append(e)
        if not lib_snaps and root_snaps:
            for c in iter_collection_tree(root_snaps):
                for e in c.objects:
                    if e.type == 'EMPTY' and e.name.startswith(s.empty_name) and e.get("ni_source") == src.name:
                        lib_snaps.append(e)

        dest_coll = ensure_child_collection(new.name, root_snaps) if s.make_subcollections else root_snaps

        copied = 0
        for e_src in lib_snaps:
            e_new = e_src.copy()
            e_new.data = None
            e_new.location = e_src.location + delta
            try:
                e_new["ni_source"] = new.name
            except Exception:
                pass
            dest_coll.objects.link(e_new)
            copied += 1

        for ob in context.selected_objects: ob.select_set(False)
        new.select_set(True)
        context.view_layer.objects.active = new

        msg = f"Added asset '{src.name}'"
        msg += f" with {copied} snap(s)." if copied else "; no snaps found under NI SNAPS."
        self.report({'INFO'}, msg)
        return {'FINISHED'}

class OBJECT_OT_ni_snaps_add_slot_2d(Operator):
    bl_idname = "object.ni_snaps_add_slot_2d"
    bl_label = "Add 2D Slot"
    bl_options = {"REGISTER", "UNDO"}
    def execute(self, context):
        s = context.scene.ni_snap_settings
        slot_coll = bpy.data.collections.get("SLOTS AND COLLISIONS")
        if not slot_coll:
            self.report({'WARNING'}, "Collection 'SLOTS AND COLLISIONS' not found.")
            return {'CANCELLED'}
        src_name = f"NI_SLOT-2D{s.template_marker}"
        src = _find_in_collection_tree(slot_coll, src_name)
        if not src:
            self.report({'WARNING'}, f"Source object '{src_name}' not found in 'SLOTS AND COLLISIONS'.")
            return {'CANCELLED'}

        ref = _active_object_or_first_selected(context)
        ref_loc = ref.matrix_world.translation.copy() if ref else Vector((0,0,0))
        target_coll = context.collection or (ref.users_collection[0] if ref and ref.users_collection else context.scene.collection)

        new = src.copy()
        if getattr(src, "data", None):
            new.data = src.data.copy()
        target_coll.objects.link(new)

        base_name = strip_marker(src.name, s.template_marker)
        new.name = ensure_unique_object_name(base_name)
        new.location = ref_loc

        for o in context.selected_objects: o.select_set(False)
        new.select_set(True)
        context.view_layer.objects.active = new

        self.report({'INFO'}, f"Added 2D Slot: {new.name}")
        return {'FINISHED'}

class OBJECT_OT_ni_snaps_add_slot_3d(Operator):
    bl_idname = "object.ni_snaps_add_slot_3d"
    bl_label = "Add 3D Slot"
    bl_options = {"REGISTER", "UNDO"}
    def execute(self, context):
        s = context.scene.ni_snap_settings
        slot_coll = bpy.data.collections.get("SLOTS AND COLLISIONS")
        if not slot_coll:
            self.report({'WARNING'}, "Collection 'SLOTS AND COLLISIONS' not found.")
            return {'CANCELLED'}
        src_name = f"NI_SLOT-3D{s.template_marker}"
        src = _find_in_collection_tree(slot_coll, src_name)
        if not src:
            self.report({'WARNING'}, f"Source object '{src_name}' not found in 'SLOTS AND COLLISIONS'.")
            return {'CANCELLED'}

        ref = _active_object_or_first_selected(context)
        ref_loc = ref.matrix_world.translation.copy() if ref else Vector((0,0,0))
        target_coll = context.collection or (ref.users_collection[0] if ref and ref.users_collection else context.scene.collection)

        new = src.copy()
        if getattr(src, "data", None):
            new.data = src.data.copy()
        target_coll.objects.link(new)

        base_name = strip_marker(src.name, s.template_marker)
        new.name = ensure_unique_object_name(base_name)
        new.location = ref_loc

        for o in context.selected_objects: o.select_set(False)
        new.select_set(True)
        context.view_layer.objects.active = new

        self.report({'INFO'}, f"Added 3D Slot: {new.name}")
        return {'FINISHED'}

# --- Disabled by cleanup: mark-as-library-source removed ---
# 
# class OBJECT_OT_ni_snaps_mark_as_library_source(Operator):
#     bl_idname = "object.ni_snaps_mark_as_library_source"
#     bl_label = "Mark Selected as Library Source"
#     bl_options = {"REGISTER", "UNDO"}
# 
#     def execute(self, context):
#         s = context.scene.ni_snap_settings
#         marker = s.template_marker
#         dest_name = s.template_target_collection
# 
#         if not marker:
#             self.report({'WARNING'}, "Template Suffix is empty.")
#             return {'CANCELLED'}
#         if not dest_name or dest_name == "__NONE__":
#             self.report({'WARNING'}, "Choose a Template Collection first.")
#             return {'CANCELLED'}
# 
#         dest_coll = bpy.data.collections.get(dest_name)
#         if not dest_coll:
#             dest_coll = bpy.data.collections.new(dest_name)
#             context.scene.collection.children.link(dest_coll)
# 
#         root = ensure_root_collection(context, s.collection_name)
#         meshes = [o for o in context.selected_objects if o.type == "MESH"]
#         if not meshes:
#             self.report({'WARNING'}, "Select one or more mesh objects.")
#             return {'CANCELLED'}
# 
#         changed = 0
#         retagged = 0
#         renamed_subs = 0
# 
#         for obj in meshes:
#             old_name = obj.name
#             if not old_name.endswith(marker):
#                 new_name = f"{old_name}{marker}"
#                 if new_name in bpy.data.objects:
#                     new_name = ensure_unique_object_name(new_name)
#                 obj.name = new_name
#                 changed += 1
#             else:
#                 new_name = old_name
# 
#             related_empties = get_related_empties_for_object(obj, s, root)
#             for e in set(related_empties):
#                 try:
#                     e["ni_source"] = new_name
#                     retagged += 1
#                 except Exception:
#                     pass
# 
#             if s.make_subcollections and root:
#                 sub = root.children.get(old_name) or root.children.get(new_name)
#                 if sub and sub.name != new_name:
#                     desired = ensure_unique_collection_name(new_name)
#                     sub.name = desired
#                     renamed_subs += 1
# 
#             link_object_only_to_collection(obj, dest_coll)
# 
#         self.report({'INFO'}, f"Marked {changed} object(s) with '{marker}'. Retagged {retagged} snap(s). Renamed {renamed_subs} NI SNAPS sub-collection(s). Moved {len(meshes)} object(s) to '{dest_coll.name}'.")
#         return {'FINISHED'}
# 
# # =====================================================================================
# # Resize All Snaps
# # =====================================================================================
# 
class OBJECT_OT_ni_snaps_resize_all_snaps(Operator):
    bl_idname = "object.ni_snaps_resize_all_snaps"
    bl_label = "Resize All Snaps"
    bl_options = {"REGISTER", "UNDO"}
    def execute(self, context):
        s = context.scene.ni_snap_settings
        prefix = s.empty_name
        target_size = float(s.resize_all_size)
        count = 0
        for ob in bpy.data.objects:
            if ob.type == 'EMPTY' and ob.name.startswith(prefix):
                ob.empty_display_size = target_size
                count += 1
        self.report({'INFO'}, f"Resized {count} snaps to {target_size}.")
        return {'FINISHED'}

# =====================================================================================
# Assign selected snaps to active (with group-offset snap to active origin)
# =====================================================================================

class OBJECT_OT_ni_snaps_assign_snaps_to_active(Operator):
    """Assign selected NI SNAP empties to the active mesh, move them into its NI SNAPS sub-collection, clean old ones, then move the group so its median aligns to the active's origin (preserving offsets)."""
    bl_idname = "object.ni_snaps_assign_snaps_to_active"
    bl_label = "Assign Snaps To Active Object"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        s = context.scene.ni_snap_settings
        view = context.view_layer
        active = view.objects.active

        if not active or active.type != "MESH":
            self.report({'WARNING'}, "Active object must be a mesh.")
            return {'CANCELLED'}

        snaps = [o for o in context.selected_objects if (o.type == 'EMPTY' and o.name.startswith(s.empty_name))]
        if not snaps:
            self.report({'WARNING'}, "Select one or more NI SNAP empties to reassign.")
            return {'CANCELLED'}

        root = ensure_root_collection(context, s.collection_name)
        dest_coll = ensure_child_collection(active.name, root) if s.make_subcollections else root

        # Compute group median BEFORE moving collections so we can offset later
        if snaps:
            median = sum((e.location for e in snaps), Vector((0.0, 0.0, 0.0))) / float(len(snaps))
        else:
            median = active.location.copy()

        removed_cols = 0
        moved = 0
        ni_tree = set(iter_collection_tree(root))

        for e in snaps:
            old_colls = list(e.users_collection)

            # Set new source tag
            try:
                e["ni_source"] = active.name
            except Exception:
                pass

            # Move to destination collection exclusively
            link_object_only_to_collection(e, dest_coll)
            moved += 1

            # Cleanup any now-empty NI SNAPS sub-collections
            for c in old_colls:
                if c in ni_tree and c != dest_coll:
                    if _remove_collection_tree_if_empty(c):
                        removed_cols += 1

        # === Selection to Active (Offset): translate group so its median lands at active origin ===
        delta = active.matrix_world.translation - median
        for e in snaps:
            e.location += delta

        self.report({'INFO'}, f"Assigned {moved} snap(s) to '{active.name}', moved group to active origin with offset. Removed {removed_cols} empty NI SNAPS collection(s).")
        return {'FINISHED'}

# =====================================================================================
# Texture Resize: IMPROVED Operators
# =====================================================================================

def _resize_texture_items(context, items, target_w, out_fmt, base_dir_abs):
    done = 0
    relinked = 0
    failed = []
    
    for it in items:
        img = bpy.data.images.get(it.image_name)
        if not img or not img.size:
            failed.append(f"{it.image_name}: No image or no size")
            continue

        ow, oh = int(img.size[0]), int(img.size[1])
        if ow <= 0 or oh <= 0:
            failed.append(f"{img.name}: Invalid dimensions {ow}x{oh}")
            continue
            
        nw = target_w
        nh = max(1, int(round(oh * (target_w / ow))))

        width_folder = os.path.join(base_dir_abs, str(nw))
        if img.filepath:
            try:
                stem = os.path.splitext(os.path.basename(bpy.path.abspath(img.filepath)))[0]
            except Exception:
                stem = img.name
        else:
            stem = img.name
        
        stem = bpy.path.clean_name(stem)
        ext = ".png" if out_fmt == "PNG" else ".jpg"
        save_path = os.path.join(width_folder, stem + ext)

        if os.path.isfile(save_path):
            try:
                temp_check_img = bpy.data.images.load(save_path, check_existing=False)
                existing_w = int(temp_check_img.size[0]) if temp_check_img.size else 0
                existing_h = int(temp_check_img.size[1]) if temp_check_img.size else 0
                bpy.data.images.remove(temp_check_img, do_unlink=True)
                if existing_w == nw and existing_h == nh:
                    try:
                        img.filepath = save_path
                        img.reload()
                        img.pack(); img.unpack(method='WRITE_ORIGINAL')
                        for mat in bpy.data.materials:
                            if mat.use_nodes and mat.node_tree:
                                for node in mat.node_tree.nodes:
                                    if node.type == 'TEX_IMAGE' and node.image == img:
                                        node.image = None; node.image = img
                        for area in context.screen.areas:
                            if area.type == 'VIEW_3D': area.tag_redraw()
                        relinked += 1
                        continue
                    except Exception as e:
                        failed.append(f"{img.name}: Failed to relink existing - {e}")
                        continue
            except Exception:
                pass

        try:
            os.makedirs(width_folder, exist_ok=True)
        except Exception as e:
            failed.append(f"{img.name}: Cannot create folder - {e}")
            continue

        img_copy = img.copy()
        try:
            img_copy.scale(nw, nh)
            rs = context.scene.render.image_settings
            old_format, old_color, old_quality = rs.file_format, rs.color_mode, rs.quality
            old_compression = getattr(rs, "compression", None)

            rs.file_format = out_fmt
            if out_fmt == 'PNG':
                rs.color_mode = 'RGBA'
                if hasattr(rs, "compression"): rs.compression = 15
            else:
                rs.color_mode = 'RGB'
                rs.quality = 90

            img_copy.save_render(save_path)

            rs.file_format, rs.color_mode, rs.quality = old_format, old_color, old_quality
            if old_compression is not None and hasattr(rs, "compression"):
                rs.compression = old_compression

            bpy.data.images.remove(img_copy)

            try:
                nodes = []
                for mat in bpy.data.materials:
                    if mat.use_nodes and mat.node_tree:
                        for node in mat.node_tree.nodes:
                            if node.type == 'TEX_IMAGE' and node.image == img:
                                nodes.append(node)

                img.filepath = save_path
                img.reload()
                img.pack(); img.unpack(method='WRITE_ORIGINAL')

                for n in nodes:
                    n.image = None; n.image = img
                for area in context.screen.areas:
                    if area.type == 'VIEW_3D': area.tag_redraw()

            except Exception as e:
                failed.append(f"{img.name}: Saved but failed to relink - {e}")

            done += 1

        except Exception as e:
            failed.append(f"{img.name}: {str(e)}")
            try: bpy.data.images.remove(img_copy)
            except: pass
    
    if failed:
        print("\n=== Resize Failures ===")
        for msg in failed:
            print(f"  - {msg}")
    
    return done, relinked


# =====================================================================================
# Texture List: Select All / None + Refresh
# =====================================================================================
# --- Removed: OBJECT_OT_ni_snaps_tex_select_all (not needed) ---
class OBJECT_OT_ni_snaps_tex_refresh(Operator):
    """Rebuild the texture list and probe file sizes/dimensions."""
    bl_idname = "object.ni_snaps_tex_refresh"
    bl_label = "Refresh"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        try:
            _ns_refresh_texture_list(context.scene, force_rebuild=True)
            self.report({'INFO'}, "Texture list refreshed.")
            return {'FINISHED'}
        except Exception as e:
            self.report({'ERROR'}, f"Refresh failed: {e}")
            return {'CANCELLED'}
class OBJECT_OT_ni_snaps_resize_textures(Operator):
    bl_idname = "object.ni_snaps_resize_textures"
    bl_label = "Resize Selected"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        s = context.scene.ni_snap_settings
        items = s.texture_items
        if len(items) == 0:
            self.report({'WARNING'}, "No textures found in the selection.")
            return {'CANCELLED'}

        targets = [it for it in items if it.selected]
        if not targets and 0 <= s.texture_index < len(items):
            targets = [items[s.texture_index]]
        if not targets:
            self.report({'WARNING'}, "Nothing selected.")
            return {'CANCELLED'}

        if s.tex_output_dir.strip():
            base_dir_abs = bpy.path.abspath(s.tex_output_dir.strip())
        else:
            blend_path = bpy.data.filepath
            if not blend_path:
                self.report({'ERROR'}, "Save your .blend file first, or specify an output folder.")
                return {'CANCELLED'}
            base_dir_abs = os.path.dirname(bpy.path.abspath(blend_path))
        
        try:
            os.makedirs(base_dir_abs, exist_ok=True)
        except Exception as e:
            self.report({'ERROR'}, f"Cannot create output folder: {base_dir_abs} ({e})")
            return {'CANCELLED'}

        try:
            target_w = int(s.tex_size_enum)
        except Exception:
            target_w = 1024

        out_fmt = s.tex_format
        done, relinked = _resize_texture_items(context, targets, target_w, out_fmt, base_dir_abs)

        s.tex_sig = ""
        _ns_refresh_texture_list(context.scene, force_rebuild=True)
        
        if relinked > 0:
            self.report({'INFO'}, f"Resized {done} texture(s). Re-linked {relinked} existing texture(s) at target size.")
        else:
            self.report({'INFO'}, f"Resized {done} of {len(targets)} texture(s) to width {target_w}.")
        return {'FINISHED'}

class OBJECT_OT_ni_snaps_resize_textures_all(Operator):
    bl_idname = "object.ni_snaps_resize_textures_all"
    bl_label = "Resize All Textures In List"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        s = context.scene.ni_snap_settings
        items = s.texture_items
        if len(items) == 0:
            self.report({'WARNING'}, "No textures found in the list.")
            return {'CANCELLED'}

        if s.tex_output_dir.strip():
            base_dir_abs = bpy.path.abspath(s.tex_output_dir.strip())
        else:
            blend_path = bpy.data.filepath
            if not blend_path:
                self.report({'ERROR'}, "Save your .blend file first, or specify an output folder.")
                return {'CANCELLED'}
            base_dir_abs = os.path.dirname(bpy.path.abspath(blend_path))
        
        try:
            os.makedirs(base_dir_abs, exist_ok=True)
        except Exception as e:
            self.report({'ERROR'}, f"Cannot create output folder: {base_dir_abs} ({e})")
            return {'CANCELLED'}

        try:
            target_w = int(s.tex_size_enum)
        except Exception:
            target_w = 1024

        out_fmt = s.tex_format
        done, relinked = _resize_texture_items(context, list(items), target_w, out_fmt, base_dir_abs)

        s.tex_sig = ""
        _ns_refresh_texture_list(context.scene, force_rebuild=True)
        
        if relinked > 0:
            self.report({'INFO'}, f"Resized {done} texture(s). Re-linked {relinked} existing texture(s) at target size.")
        else:
            self.report({'INFO'}, f"Resized {done} of {len(items)} texture(s) to width {target_w}.")
        return {'FINISHED'}

class OBJECT_OT_ni_snaps_open_texture_location(Operator):
    bl_idname = "object.ni_snaps_open_texture_location"
    bl_label = "Open File Location"
    bl_options = {"REGISTER"}

    def execute(self, context):
        s = context.scene.ni_snap_settings
        items = s.texture_items
        
        if len(items) == 0 or s.texture_index >= len(items):
            self.report({'WARNING'}, "No texture selected.")
            return {'CANCELLED'}
        
        selected_item = items[s.texture_index]
        img = bpy.data.images.get(selected_item.image_name)
        
        if not img or not img.filepath:
            self.report({'WARNING'}, f"Image '{selected_item.image_name}' has no file path.")
            return {'CANCELLED'}
        
        filepath = bpy.path.abspath(img.filepath)
        if not os.path.isfile(filepath):
            self.report({'WARNING'}, f"File does not exist: {filepath}")
            return {'CANCELLED'}
        
        folder = os.path.dirname(filepath)
        
        import subprocess, platform
        try:
            if platform.system() == "Windows":
                os.startfile(folder)
            elif platform.system() == "Darwin":
                subprocess.Popen(["open", folder])
            else:
                subprocess.Popen(["xdg-open", folder])
            self.report({'INFO'}, f"Opened: {folder}")
            return {'FINISHED'}
        except Exception as e:
            self.report({'ERROR'}, f"Failed to open folder: {e}")
            return {'CANCELLED'}

# =====================================================================================
# Panel
# =====================================================================================
# =====================================================================================
# Reorder-friendly UI (centralized section order + compact draw helpers)
# =====================================================================================

# Change this list to reorder your UI anytime (keys must match UI_SECTIONS below)
UI_SECTION_ORDER = [
    "creation",
    "library",
    "assign",
    "tex",
    "origin",
    "create",
    "cleanup",
    "export",
    "footer",  # keep footer last if you want the signature at the bottom
]

def _ui_section_creation(layout, s, context):
    box = layout.box()
    row = box.row()
    row.prop(s, "ui_show_creation",
             text="Creation Settings",
             icon="TRIA_DOWN" if s.ui_show_creation else "TRIA_RIGHT",
             emboss=False)
    if s.ui_show_creation:
        col = box.column(align=True)
        col.prop(s, "collection_name")
        col.prop(s, "make_subcollections")
        col.prop(s, "empty_name")
        col.prop(s, "display_type")
        col.separator()
        col.prop(s, "empty_size")
        col.separator()
        col.prop(s, "select_created")
        col.prop(s, "deselect_vertices")
        col.separator()
#         col.prop(s, "template_marker", text="Template Suffix")  # HIDDEN per request
#         col.prop(s, "template_target_collection", text="Template Collection")  # HIDDEN per request
        col.separator()
#         col.operator(OBJECT_OT_ni_snaps_mark_as_library_source.bl_idname, icon="BOOKMARKS")  # HIDDEN per request

def _ui_section_library(layout, s, context):
    box = layout.box()
    row = box.row()
    row.prop(s, "ui_show_library",
             text="Asset Library",
             icon="TRIA_DOWN" if s.ui_show_library else "TRIA_RIGHT",
             emboss=False)
    if s.ui_show_library:
        col = box.column(align=True)
        col.prop(s, "library_collection", text="Collection")
        col.prop(s, "library_active", text="Asset")
        col.separator()
        col.operator(OBJECT_OT_ni_snaps_add_library_copy.bl_idname, icon="IMPORT")
        col.separator()
        r = col.row(align=True)
        r.operator("object.ni_snaps_add_slot_2d", text="Add 2D Slot", icon="MESH_PLANE")
        r.separator()
        r.operator("object.ni_snaps_add_slot_3d", text="Add 3D Slot", icon="MESH_CUBE")

def _ui_section_assign(layout, s, context):
    box = layout.box()
    row = box.row()
    row.prop(s, "ui_show_assign",
             text="Assign Materials",
             icon="TRIA_DOWN" if s.ui_show_assign else "TRIA_RIGHT",
             emboss=False)
    if s.ui_show_assign:
        col = box.column(align=True)
        col.prop(s, "material_source", text="Material From")
        col.separator()
        col.operator(OBJECT_OT_ni_snaps_assign_materials.bl_idname, icon="MATERIAL")

def _ui_section_tex(layout, s, context):
    box = layout.box()
    row = box.row()
    row.prop(s, "ui_show_tex",
             text="Texture Resize",
             icon="TRIA_DOWN" if s.ui_show_tex else "TRIA_RIGHT",
             emboss=False)
    if s.ui_show_tex:
        sub = box.box()
        sub.label(text="Settings")
        sub.prop(s, "tex_output_dir", text="Output Folder")
        row2 = sub.row(align=True)
        row2.prop(s, "tex_format", text="Format")
        row2.prop(s, "tex_size_enum", text="Width")
        sub2 = box.box()
        row_tex = sub2.row()
        split = row_tex.split(factor=0.88)
        left = split.row()
        left.label(text="Textures")
        right = split.row(align=True)
        right.operator('object.ni_snaps_tex_refresh', text='', icon='FILE_REFRESH')
        rows = 6
        sub2.template_list("OBJECT_UL_ni_textures", "", s, "texture_items", s, "texture_index", rows=rows)
        row_btn = box.row(align=True)
        row_btn.operator(OBJECT_OT_ni_snaps_resize_textures_all.bl_idname,
                         text="Resize All In List",
                         icon="IMAGE_ALPHA")
        row_btn.separator()
        row_btn.operator(OBJECT_OT_ni_snaps_resize_textures.bl_idname,
                         text="Resize Selected",
                         icon="IMAGE_ZDEPTH")

        box.operator(OBJECT_OT_ni_snaps_open_texture_location.bl_idname,
                     text="Open Selected File Location",
                     icon="FILE_FOLDER")

def _ui_section_origin(layout, s, context):
    box = layout.box()
    row = box.row()
    row.prop(s, "ui_show_origin",
             text="Set Object Origin",
             icon="TRIA_DOWN" if s.ui_show_origin else "TRIA_RIGHT",
             emboss=False)
    if s.ui_show_origin:
        r = box.row(align=True)
        r.operator("object.ni_snaps_origin_to_selected", text="To Selected", icon="PIVOT_CURSOR")
        r.separator()
        r.operator("object.ni_snaps_origin_cursor", text="To Cursor", icon="CURSOR")

        r = box.row(align=True)
        r.operator("object.ni_snaps_origin_world_zero", text="To World Zero", icon="WORLD")
        r.separator()
        r.operator("object.ni_snaps_origin_center", text="To Center", icon="PIVOT_MEDIAN")

        r = box.row(align=True)
        r.operator("object.ni_snaps_origin_top_center", text="To Top", icon="TRIA_UP")
        r.separator()
        r.operator("object.ni_snaps_origin_bottom_center", text="To Bottom", icon="TRIA_DOWN")

        r = box.row(align=True)
        r.operator("object.ni_snaps_origin_left_mid", text="To Left", icon="TRIA_LEFT")
        r.separator()
        r.operator("object.ni_snaps_origin_right_mid", text="To Right", icon="TRIA_RIGHT")

def _ui_section_create(layout, s, context):
    box = layout.box()
    row = box.row()
    row.prop(s, "ui_show_create",
             text="Create Snaps",
             icon="TRIA_DOWN" if s.ui_show_create else "TRIA_RIGHT",
             emboss=False)
    if s.ui_show_create:
        col = box.column(align=True)
        col.operator("object.ni_snaps_create_vertices", icon="VERTEXSEL")
        col.separator()
        col.operator("object.ni_snaps_create_edges", icon="EDGESEL")
        col.separator()
        col.operator("object.ni_snaps_create_faces", icon="FACESEL")
        col.separator()
        col.operator("object.ni_snaps_assign_snaps_to_active",
                     text="Assign Snaps To Active Object",
                     icon="CON_CHILDOF")

def _ui_section_cleanup(layout, s, context):
    box = layout.box()
    row = box.row()
    row.prop(s, "ui_show_cleanup",
             text="Cleanup",
             icon="TRIA_DOWN" if s.ui_show_cleanup else "TRIA_RIGHT",
             emboss=False)
    if s.ui_show_cleanup:
        col = box.column(align=True)
        col.operator("object.ni_snaps_clean", icon="TRASH")
        col.separator()
        col.operator("object.ni_snaps_purge_orphans", icon="TRASH")
        col.separator()
        row = col.row(align=True)
        row.prop(s, "resize_all_size", text="Size")
        row.separator()
        row.operator("object.ni_snaps_resize_all_snaps", text="Resize All Snaps", icon="SORTSIZE")


def _ui_section_export(layout, s, context):
    box = layout.box()
    row = box.row()
    row.prop(s, "ui_show_export",
             text="Export Tools",
             icon="TRIA_DOWN" if s.ui_show_export else "TRIA_RIGHT",
             emboss=False)
    if s.ui_show_export:
        col = box.column(align=True)
        col.separator()
        col.operator("object.ni_snaps_batch_export_glb", text="Batch Export as GLB", icon="EXPORT")
        col.separator()
        col.operator("object.ni_snaps_unhide_exported", text="Unhide All Exported Meshes", icon="HIDE_OFF")

        # ▼ Hide-after-export FIRST (above proximity options)
        col.separator()
        col.prop(s, "hide_after_export", text="Hide After Export")

        # ▼ Batch-only proximity options
        sub = col.column(align=True)
        sub.separator()
        sub.prop(s, "include_nearby_meshes", text="Include Nearby Meshes")
        sub.prop(s, "proximity_padding", text="Proximity Padding (m)")


def _ui_section_footer(layout, s, context):
    box = layout.box()
    col = box.column(align=True)
    col.alignment = 'CENTER'
    col.label(text="Created with love for my fellow islanders - Wisp")

# Key → draw function mapping (edit order via UI_SECTION_ORDER above)
UI_SECTIONS = {
    "creation": _ui_section_creation,
    "library":  _ui_section_library,
    "assign":   _ui_section_assign,
    "tex":      _ui_section_tex,
    "origin":   _ui_section_origin,
    "create":   _ui_section_create,
    "cleanup":  _ui_section_cleanup,
    "export":   _ui_section_export,
    "footer":   _ui_section_footer,
}

class VIEW3D_PT_ni_snaps_panel(Panel):
    bl_label = "NI SNAPS"
    bl_idname = "VIEW3D_PT_ni_snaps_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "NI SNAPS"

    def draw(self, context):
        layout = self.layout
        s = context.scene.ni_snap_settings

        # Render sections in the chosen order
        for key in UI_SECTION_ORDER:
            draw_fn = UI_SECTIONS.get(key)
            if draw_fn:
                draw_fn(layout, s, context)

# =====================================================================================
# Registration
# =====================================================================================
class OBJECT_OT_ni_snaps_unhide_exported(Operator):
    bl_idname = "object.ni_snaps_unhide_exported"
    bl_label = "Unhide All Exported Meshes"
    bl_description = "Unhide meshes and related snaps (viewport only). Does not move collections."
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        s = context.scene.ni_snap_settings
        # Unhide all meshes in the scene
        mesh_count = 0
        snap_count = 0
        for o in bpy.data.objects:
            if o.type == 'MESH':
                if o.hide_get():
                    o.hide_set(False)
                    mesh_count += 1

        # Unhide empties that look like snaps: either ni_source == any or name starts with prefix
        pref = (s.empty_name or "").strip()
        for o in bpy.data.objects:
            if o.type == 'EMPTY':
                is_snap = ('ni_source' in o.keys()) or (pref and o.name.startswith(pref))
                if is_snap and o.hide_get():
                    o.hide_set(False)
                    snap_count += 1

        self.report({'INFO'}, f"Unhid {mesh_count} mesh(es) and {snap_count} snap(s).")
        return {'FINISHED'}
classes = (
    NINodeTextureItem,
    OBJECT_UL_ni_textures,
    NISnapSettings,
    OBJECT_OT_ni_snaps_create_vertices,
    OBJECT_OT_ni_snaps_create_edges,
    OBJECT_OT_ni_snaps_create_faces,
    OBJECT_OT_ni_snaps_clean,
    OBJECT_OT_ni_snaps_purge_orphans,
    OBJECT_OT_ni_snaps_resize_all_snaps,
    OBJECT_OT_ni_snaps_origin_bottom_center,
    OBJECT_OT_ni_snaps_origin_top_center,
    OBJECT_OT_ni_snaps_origin_center,
    OBJECT_OT_ni_snaps_origin_cursor,
    OBJECT_OT_ni_snaps_origin_world_zero,
    OBJECT_OT_ni_snaps_origin_left_mid,
    OBJECT_OT_ni_snaps_origin_right_mid,
    OBJECT_OT_ni_snaps_origin_to_selected,
    OBJECT_OT_ni_snaps_batch_export_glb,
    OBJECT_OT_ni_snaps_add_library_copy,
    OBJECT_OT_ni_snaps_add_slot_2d,
    OBJECT_OT_ni_snaps_add_slot_3d,
    OBJECT_OT_ni_snaps_assign_materials,
    OBJECT_OT_ni_snaps_tex_refresh,
    OBJECT_OT_ni_snaps_resize_textures,
    OBJECT_OT_ni_snaps_resize_textures_all,
    OBJECT_OT_ni_snaps_open_texture_location,
    OBJECT_OT_ni_snaps_assign_snaps_to_active,  # updated behavior
    OBJECT_OT_ni_snaps_unhide_exported,

    VIEW3D_PT_ni_snaps_panel,
)
# =====================================================================================
# Centralized operator descriptions (edit here any time)
# =====================================================================================

OP_DESCRIPTIONS = {
    # Create Snaps
    "object.ni_snaps_create_vertices": "Create NI_SNAP empties at selected vertices.",
    "object.ni_snaps_create_edges": "Create NI_SNAP empties at midpoints of selected edges.",
    "object.ni_snaps_create_faces": "Create NI_SNAP empties at centers of selected faces.",

    # Cleanup
    "object.ni_snaps_clean": "Delete NI_SNAP empties related to the currently selected mesh objects.",
    "object.ni_snaps_purge_orphans": "Remove NI_SNAP empties whose source objects are missing and delete empty NI SNAPS sub-collections.",

    # Resize all snaps
    "object.ni_snaps_resize_all_snaps": "Set the display size for all NI_SNAP empties in this file.",

    # Origin tools
    "object.ni_snaps_origin_bottom_center": "Move origin of selected meshes to the bottom of their bounding box.",
    "object.ni_snaps_origin_top_center": "Move origin of selected meshes to the top of their bounding box.",
    "object.ni_snaps_origin_center": "Move origin of selected meshes to the center of their bounding box.",
    "object.ni_snaps_origin_cursor": "Set origin of selected objects to the 3D cursor.",
    "object.ni_snaps_origin_world_zero": "Set origin of selected objects to world origin (0,0,0).",
    "object.ni_snaps_origin_left_mid": "Move origin of selected meshes to the left mid of their bounding box.",
    "object.ni_snaps_origin_right_mid": "Move origin of selected meshes to the right mid of their bounding box.",
    "object.ni_snaps_origin_to_selected": "Snap cursor to selected geometry and set origin of selected objects to that position; restore mode afterwards.",

    # Export
    "object.ni_snaps_export_glb": "Export all selected meshes and their NI_SNAP empties as a single GLB (no joining).",
    "object.ni_snaps_batch_export_glb": "Export each selected mesh (and its NI_SNAP empties) to a separate GLB file.",

    # Asset library
    "object.ni_snaps_add_library_copy": "Add a copy of the chosen library asset at the 3D cursor (with snaps if found).",
    "object.ni_snaps_add_slot_2d": "Add a 2D slot from ‘SLOTS AND COLLISIONS’ at the active object’s location.",
    "object.ni_snaps_add_slot_3d": "Add a 3D slot from ‘SLOTS AND COLLISIONS’ at the active object’s location.",    # Materials
    "object.ni_snaps_assign_materials": "Assign materials from the chosen template object (‘MATERIAL SLOTS’) to selected meshes.",

    # Texture resize
    "object.ni_snaps_resize_textures": "Resize the selected texture(s) to the target width and relink in materials.",
    "object.ni_snaps_resize_textures_all": "Resize all textures listed to the target width and relink in materials.",
    "object.ni_snaps_open_texture_location": "Open the folder containing the currently selected texture file.",    "object.ni_snaps_tex_refresh": "Rebuild the texture list and update file sizes/dimensions.",

    # Snap assignment
    "object.ni_snaps_assign_snaps_to_active": "Assign selected NI_SNAP empties to the active mesh and offset the group so its median lands at the active origin.",
}

def _apply_operator_descriptions():
    """Populate bl_description for all operators from OP_DESCRIPTIONS (or class docstring)."""
    try:
        from bpy.types import Operator as _BpyOperator
    except Exception:
        _BpyOperator = None

    for cls in classes:
        try:
            if _BpyOperator and issubclass(cls, _BpyOperator):
                desc = OP_DESCRIPTIONS.get(getattr(cls, "bl_idname", ""), None)
                if not desc:
                    doc = (cls.__doc__ or "").strip()
                    desc = doc if doc else None
                if desc:
                    cls.bl_description = desc
        except Exception:
            # Never let a tooltip failure block registration
            pass

# =====================================================================================
# Registration (clean, 4-space indent only)
# =====================================================================================


# ---------------------------------------------------------------------------
# Safe class discovery (fallback if `classes` is missing)
# ---------------------------------------------------------------------------
def _ns_discover_classes():
    import inspect
    from bpy.types import Operator, Panel, UIList, PropertyGroup
    found = []
    current_globals = globals()
    for name, obj in list(current_globals.items()):
        try:
            if inspect.isclass(obj) and issubclass(obj, (Operator, Panel, UIList, PropertyGroup)):
                found.append(obj)
        except Exception:
            pass
    def key(c):
        from bpy.types import Operator, Panel, UIList, PropertyGroup
        if issubclass(c, PropertyGroup): return (0, c.__name__)
        if issubclass(c, UIList):        return (1, c.__name__)
        if issubclass(c, Operator):      return (2, c.__name__)
        if issubclass(c, Panel):         return (3, c.__name__)
        return (4, c.__name__)
    return sorted(found, key=key)



def register():
    from bpy.utils import register_class
    for cls in classes:
        register_class(cls)
    # Property group registration if not already (often done elsewhere)
    try:
        import bpy
        from bpy.props import PointerProperty
        if not hasattr(bpy.types.Scene, "ni_snap_settings"):
            bpy.types.Scene.ni_snap_settings = PointerProperty(type=NISnapSettings)
    except Exception:
        pass

    # --- Handlers: keep list fresh on any depsgraph change ---
    try:
        import bpy
        if _ns_depsgraph_update not in bpy.app.handlers.depsgraph_update_post:
            bpy.app.handlers.depsgraph_update_post.append(_ns_depsgraph_update)
    except Exception:
        pass

    # --- On file load, force one rebuild so UI isn't empty after opening a .blend ---
    def _ns_on_load(dummy):
        try:
            import bpy
            scn = bpy.context.scene
            if scn:
                _ns_refresh_texture_list(scn, force_rebuild=True)
        except Exception:
            pass
    try:
        import bpy
        # Avoid duplicates
        for h in list(bpy.app.handlers.load_post):
            if getattr(h, "__name__", "") == "_ns_on_load":
                bpy.app.handlers.load_post.remove(h)
        bpy.app.handlers.load_post.append(_ns_on_load)
    except Exception:
        pass

    # --- Msgbus: refresh when active object changes (fast + precise) ---
    try:
        import bpy
        # Clear any old subscriptions for safety
        bpy.msgbus.clear_by_owner(_NS_MSGBUS_OWNER)
        bpy.msgbus.subscribe_rna(
            key=(bpy.types.LayerObjects, "active"),
            owner=_NS_MSGBUS_OWNER,
            args=(),
            notify=_ns_on_active_change,
            options={'PERSIST'}
        )
        # Also subscribe to selection changes
        bpy.msgbus.subscribe_rna(
            key=(bpy.types.LayerObjects, "selected"),
            owner=_NS_MSGBUS_OWNER,
            args=(),
            notify=_ns_on_active_change,  # Use same callback
            options={'PERSIST'}
        )
    except Exception as ex:
        print(f"Msgbus subscription error: {ex}")

    # --- Handlers: keep list fresh on any depsgraph change ---
    try:
        import bpy
        if _ns_depsgraph_update not in bpy.app.handlers.depsgraph_update_post:
            bpy.app.handlers.depsgraph_update_post.append(_ns_depsgraph_update)
    except Exception:
        pass

    # --- On file load, force one rebuild and re-subscribe msgbus ---
    def _ns_on_load(dummy):
        try:
            import bpy
            bpy.msgbus.clear_by_owner(_NS_MSGBUS_OWNER)
            bpy.msgbus.subscribe_rna(
                key=(bpy.types.LayerObjects, "active"),
                owner=_NS_MSGBUS_OWNER,
                args=(),
                notify=_ns_on_active_change,
                options={'PERSIST'}
            )
            bpy.msgbus.subscribe_rna(
                key=(bpy.types.LayerObjects, "selected"),
                owner=_NS_MSGBUS_OWNER,
                args=(),
                notify=_ns_on_active_change,
                options={'PERSIST'}
            )
            scn = bpy.context.scene
            if scn:
                _ns_refresh_texture_list(scn, force_rebuild=True)
        except Exception as ex:
            print(f"Load post error: {ex}")

    try:
        import bpy
        # Avoid duplicates
        for h in list(bpy.app.handlers.load_post):
            if getattr(h, "__name__", "") == "_ns_on_load":
                bpy.app.handlers.load_post.remove(h)
        bpy.app.handlers.load_post.append(_ns_on_load)
    except Exception:
        pass

def unregister():
    from bpy.utils import unregister_class
    try:
        import bpy
        # Remove handlers we added
        if _ns_depsgraph_update in bpy.app.handlers.depsgraph_update_post:
            bpy.app.handlers.depsgraph_update_post.remove(_ns_depsgraph_update)
        # Remove our load handler by name
        for h in list(bpy.app.handlers.load_post):
            if getattr(h, "__name__", "") == "_ns_on_load":
                bpy.app.handlers.load_post.remove(h)
        # Clear msgbus
        bpy.msgbus.clear_by_owner(_NS_MSGBUS_OWNER)
    except Exception:
        pass
    for cls in reversed(classes):
        try:
            unregister_class(cls)
        except Exception:
            pass
    try:
        import bpy
        if hasattr(bpy.types.Scene, "ni_snap_settings"):
            del bpy.types.Scene.ni_snap_settings
    except Exception:
        pass
def unregister():
    import bpy
    global classes
    # Handlers
    try:
        if _ns_depsgraph_update in bpy.app.handlers.depsgraph_update_post:
            bpy.app.handlers.depsgraph_update_post.remove(_ns_depsgraph_update)
    except Exception:
        pass
    # Scene pointer
    try:
        del bpy.types.Scene.ni_snap_settings
    except Exception:
        pass
    # Choose iteration list
    try:
        _iter = classes if isinstance(classes, (tuple, list)) and classes else tuple(_ns_discover_classes())
    except Exception:
        _iter = []
    # Unregister in reverse
    for cls in reversed(_iter):
        try:
            bpy.utils.unregister_class(cls)
        except Exception:
            pass


# ======================= NI SNAPS AUTO-REFRESH WATCHDOG =======================
try:
    import bpy
except Exception:
    bpy = None

_ns_watchdog_running = False
_ns_watchdog_stop = False
_ns_watchdog_last_sig = ""

def _ns_build_selection_signature():
    names = []
    try:
        ctx = bpy.context
        ob = ctx.view_layer.objects.active if ctx and ctx.view_layer else None
        objs = []
        if ob: objs.append(ob)
        for o in getattr(ctx, "selected_objects", []) or []:
            if o not in objs:
                objs.append(o)
        for ob in objs:
            if getattr(ob, "type", None) != 'MESH':
                continue
            for slot in getattr(ob, "material_slots", []) or []:
                mat = getattr(slot, "material", None)
                if not mat: continue
                nt = getattr(mat, "node_tree", None)
                if not nt: continue
                for node in getattr(nt, "nodes", []) or []:
                    if getattr(node, "type", "") == 'TEX_IMAGE' and getattr(node, "image", None):
                        names.append(node.image.name)
        names = sorted(set(names))
        return "|".join(names)
    except Exception:
        return ""

def _ns_selection_watchdog():
    global _ns_watchdog_running, _ns_watchdog_stop, _ns_watchdog_last_sig
    try:
        if _ns_watchdog_stop or bpy is None:
            _ns_watchdog_running = False
            return None
        sc = bpy.context.scene if bpy and bpy.context else None
        if not sc:
            return 0.25
        sig = _ns_build_selection_signature()
        if sig != _ns_watchdog_last_sig:
            _ns_watchdog_last_sig = sig
            try:
                if '_ns_refresh_texture_list' in globals():
                    _ns_refresh_texture_list(sc, force_rebuild=True)
            except Exception as ex:
                print("[NI SNAPS] refresh (watchdog) failed:", ex)
        return 0.25
    except Exception as ex:
        print("[NI SNAPS] watchdog error:", ex)
        return 0.5

def _ns_start_watchdog():
    global _ns_watchdog_running, _ns_watchdog_stop
    if bpy is None: return
    _ns_watchdog_stop = False
    if not _ns_watchdog_running:
        try:
            bpy.app.timers.register(_ns_selection_watchdog, persistent=True, first_interval=0.1)
            _ns_watchdog_running = True
        except Exception as ex:
            print("[NI SNAPS] couldn't start watchdog:", ex)

def _ns_stop_watchdog():
    global _ns_watchdog_stop
    _ns_watchdog_stop = True

# Wrap/augment existing register/unregister
try:
    _NI_SNAPS_ORIG_REGISTER = register  # type: ignore[name-defined]
except Exception:
    _NI_SNAPS_ORIG_REGISTER = None

def register():
    if _NI_SNAPS_ORIG_REGISTER:
        try:
            _NI_SNAPS_ORIG_REGISTER()
        except Exception as ex:
            print("[NI SNAPS] original register() failed:", ex)
    _ns_start_watchdog()

try:
    _NI_SNAPS_ORIG_UNREGISTER = unregister  # type: ignore[name-defined]
except Exception:
    _NI_SNAPS_ORIG_UNREGISTER = None

def unregister():
    _ns_stop_watchdog()
    if _NI_SNAPS_ORIG_UNREGISTER:
        try:
            _NI_SNAPS_ORIG_UNREGISTER()
        except Exception as ex:
            print("[NI SNAPS] original unregister() failed:", ex)

# Start after file load
if bpy is not None:
    try:
        def _ni_snaps__on_load(dummy):
            _ns_start_watchdog()
        for h in list(bpy.app.handlers.load_post):
            if getattr(h, "__name__", "") == "_ni_snaps__on_load":
                bpy.app.handlers.load_post.remove(h)
        bpy.app.handlers.load_post.append(_ni_snaps__on_load)
    except Exception as ex:
        print("[NI SNAPS] load_post hook failed:", ex)
# ==============================================================================
