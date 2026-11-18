"""Translate the registered native assets' explicit PBR inputs for CUDA rendering."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import mitsuba as mi
import numpy as np
from a100sim.materials import read_usd_material
from PIL import Image
from pxr import Usd, UsdShade


@lru_cache(maxsize=48)
def pixels(path: str) -> np.ndarray:
    """Read native texture channels into a bounded cache of RGBA arrays."""
    with Image.open(path) as image:
        return np.asarray(image.convert("RGBA"), np.float32) / 255


def input_value(shader: UsdShade.Shader, name: str, default: Any = None) -> Any:
    """Read an authored shader input while retaining explicit zero values."""
    value = shader.GetInput(name)
    value = value.Get() if value else None
    return default if value is None else value


def texture_path(shader: UsdShade.Shader, name: str) -> str | None:
    """Resolve an authored texture or fail when its dependency is missing."""
    value = input_value(shader, name)
    if not value:
        return None
    path = value.resolvedPath or value.path
    if not Path(path).is_file():
        raise ValueError(f"Native texture is unavailable: {name} on {shader.GetPath()}")
    return path


def bitmap(
    path: str, *, channel: int | None = None, raw: bool = False, multiply: float | list[float] | None = None
) -> dict[str, Any]:
    """Create a texture with explicit channel selection and color transfer."""
    if channel is None and multiply is None:
        return {"type": "bitmap", "filename": path, "raw": raw, "wrap_mode": "repeat"}
    data = pixels(path)
    if channel is not None:
        data = data[:, :, channel : channel + 1]
    else:
        data = data[:, :, :3]
        if not raw:
            data = np.where(data <= 0.04045, data / 12.92, ((data + 0.055) / 1.055) ** 2.4)
    if multiply is not None:
        data = data * np.asarray(multiply)
    return {
        "type": "bitmap",
        "bitmap": mi.Bitmap(np.ascontiguousarray(data, np.float32)),
        "raw": True,
        "wrap_mode": "repeat",
    }


def native_surface(
    prim: Usd.Prim, *, vertex_color: bool = False, channels: tuple[str, ...] = ()
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Map supported native MDL inputs to PBR; reject unknown shader programs."""
    material, _ = UsdShade.MaterialBindingAPI(prim).ComputeBoundMaterial()
    shader = material.ComputeSurfaceSource("mdl")[0] if material else None
    if not shader:
        return read_usd_material(prim, vertex_color=vertex_color, channels=channels), None
    source = str(shader.GetPrim().GetAttribute("info:mdl:sourceAsset:subIdentifier").Get())
    if source not in {"OmniPBR", "MI_Props", "MI_TrashCan", "MI_Clock", "MI_DoorEntrance_glass"}:
        raise ValueError(f"No faithful PBR mapping for native material {source}")
    omni = source == "OmniPBR"
    albedo = texture_path(shader, "diffuse_texture" if omni else "BaseColor")
    tint = input_value(shader, "diffuse_color_constant" if omni else "Color", [1, 1, 1])
    surface = {
        "type": "principled",
        "base_color": bitmap(albedo, multiply=list(tint)[:3]) if albedo else {"type": "rgb", "value": list(tint)[:3]},
        "roughness": float(input_value(shader, "reflection_roughness_constant", 0.5)),
        "metallic": float(input_value(shader, "metallic_constant", 0)),
    }
    orm = texture_path(shader, "ORM_texture" if omni else "AO_R_Rough_G_Metallic_B_")
    if orm and (not omni or input_value(shader, "enable_ORM_texture", False)):
        surface["roughness"] = bitmap(
            orm, channel=1, raw=True, multiply=float(input_value(shader, "RoughnessMultiplier", 1))
        )
        surface["metallic"] = bitmap(orm, channel=2, raw=True)
    normal = texture_path(shader, "normalmap_texture" if omni else "Normal")
    if normal:
        surface = {"type": "normalmap", "normalmap": bitmap(normal, raw=True), "material": surface}
    surface = {"type": "twosided", "material": surface}
    if source == "MI_DoorEntrance_glass" and albedo:
        # This material's MDL explicitly uses diffuse alpha as surface opacity.
        alpha = np.clip(
            pixels(albedo)[:, :, 3:4] * float(input_value(shader, "Opacity_Multiply", 1))
            + float(input_value(shader, "Opacity_Add", 0)),
            0,
            1,
        )
        surface = {
            "type": "mask",
            "opacity": {"type": "bitmap", "bitmap": mi.Bitmap(alpha.copy()), "raw": True},
            "material": surface,
        }
    emitter = None
    if source == "MI_Clock" and orm:
        color = np.asarray(input_value(shader, "EmissionColor", [0, 0, 0]))[:3]
        intensity = float(input_value(shader, "EmissionIntensity", 0))
        if intensity > 0 and color.max() > 0:
            data = pixels(orm)[:, :, 3:4] * color * intensity
            emitter = {
                "type": "area",
                "radiance": {
                    "type": "bitmap",
                    "bitmap": mi.Bitmap(np.ascontiguousarray(data, np.float32)),
                    "raw": True,
                },
            }
    return surface, emitter
