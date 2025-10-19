
bl_info = {
    "name": "NI SNAPS",
    "author": "Wisp",
    "version": (1, 0, 0),
    "blender": (2, 80, 0),
    "location": "3D Viewport > N-Panel > NI SNAPS",
    "description": "Create Snap Points on vertices/edges/faces, asset tools, origins, materials, texture resize, cleanup, and GLB export.",
    "category": "3D View",
}

def register():
    from . import NI_SNAPS
    NI_SNAPS.register()

def unregister():
    from . import NI_SNAPS
    NI_SNAPS.unregister()
