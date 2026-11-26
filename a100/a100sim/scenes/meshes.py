"""Preserve native UV seams, face-varying normals and material assignments."""

from __future__ import annotations

from typing import Any

import mitsuba as mi
import numpy as np
from a100sim.materials import MATERIAL_CHANNELS, read_mesh_channels
from a100sim.scenes.materials import native_surface
from pxr import Usd, UsdGeom


def mesh_shape(prim: Usd.Prim, matrix: np.ndarray) -> mi.Mesh:
    """Triangulate each face corner without merging native UV or normal seams."""
    mesh = UsdGeom.Mesh(prim)
    if mesh.GetSubdivisionSchemeAttr().Get() not in (None, "none"):
        raise ValueError(f"Subdivision requires tessellation: {prim.GetPath()}")
    points = np.asarray(mesh.GetPointsAttr().Get(), np.float32)
    counts = np.asarray(mesh.GetFaceVertexCountsAttr().Get(), np.int32)
    indices = np.asarray(mesh.GetFaceVertexIndicesAttr().Get(), np.int32)
    if not len(points) or np.any(counts < 3) or counts.sum() != len(indices):
        raise ValueError(f"Invalid native mesh topology: {prim.GetPath()}")
    # Native triangle soups already use Mitsuba's corner order, including
    # face-varying UV and normal seams. Other polygons retain their fan order.
    triangles = np.all(counts == 3)
    corners = None
    if triangles:
        vertex_indices = indices
    else:
        triangles_per_face = counts - 2
        starts = np.cumsum(counts, dtype=np.intp) - counts
        first = np.repeat(starts, triangles_per_face)
        triangle_starts = np.cumsum(triangles_per_face, dtype=np.intp) - triangles_per_face
        offset = np.arange(len(first)) - np.repeat(triangle_starts, triangles_per_face)
        corners = np.column_stack((first, first + offset + 1, first + offset + 2)).reshape(-1)
        vertex_indices = indices[corners]
    corner_count = len(vertex_indices)

    def per_corner(value: Any, interpolation: str) -> np.ndarray:
        value = np.asarray(value)
        if interpolation in ("vertex", "varying"):
            return value[vertex_indices]
        if interpolation == "faceVarying":
            return value if triangles else value[corners]
        if interpolation == "uniform":
            return np.repeat(value, (counts - 2) * 3, axis=0)
        if interpolation == "constant" and len(value) == 1:
            return np.repeat(value, corner_count, axis=0)
        raise ValueError(f"Unsupported native interpolation: {interpolation}")

    positions = points[vertex_indices]
    normals = mesh.GetNormalsAttr().Get()
    normals = per_corner(normals, mesh.GetNormalsInterpolation()) if normals else None
    st = UsdGeom.PrimvarsAPI(prim).GetPrimvar("st")
    uv = per_corner(st.ComputeFlattened(), st.GetInterpolation()) if st and st.HasValue() else None
    rgb_var = mesh.GetDisplayColorPrimvar()
    rgb = per_corner(rgb_var.ComputeFlattened(), rgb_var.GetInterpolation()) if rgb_var and rgb_var.HasValue() else None
    channels = {}
    for name in MATERIAL_CHANNELS:
        value = UsdGeom.PrimvarsAPI(prim).GetPrimvar("a100sim:" + name)
        if value and value.HasValue():
            channels[name] = per_corner(value.ComputeFlattened(), value.GetInterpolation())
    channels = read_mesh_channels(channels, corner_count)
    surface, emitter = native_surface(prim, vertex_color=rgb is not None, channels=tuple(channels))
    props = mi.Properties()
    props["bsdf"] = mi.load_dict(surface)
    if emitter:
        props["emitter"] = mi.load_dict(emitter)
    shape = mi.Mesh(
        str(prim.GetPath()),
        vertex_count=corner_count,
        face_count=corner_count // 3,
        props=props,
        has_vertex_normals=normals is not None,
        has_vertex_texcoords=uv is not None,
    )
    if rgb is not None:
        shape.add_attribute("vertex_color", 3, np.asarray(rgb, np.float32).reshape(-1))
    for name, channel in channels.items():
        shape.add_attribute("vertex_" + name, 1, np.asarray(channel, np.float32))
    params = mi.traverse(shape)
    params["vertex_positions"] = (positions @ matrix[:3, :3].T + matrix[:3, 3]).astype(np.float32).reshape(-1)
    params["faces"] = np.arange(corner_count, dtype=np.uint32)
    if normals is not None:
        normals = normals @ np.linalg.inv(matrix[:3, :3])
        normals /= np.maximum(np.linalg.norm(normals, axis=1, keepdims=True), 1e-10)
        params["vertex_normals"] = normals.astype(np.float32).reshape(-1)
    if uv is not None:
        uv = np.asarray(uv, np.float32).copy()
        uv[:, 1] = 1 - uv[:, 1]
        params["vertex_texcoords"] = uv.reshape(-1)
    params.update()
    return shape
