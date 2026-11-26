"""Translate the registered native assets' explicit PBR inputs for CUDA rendering."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import drjit as dr
import mitsuba as mi
import numpy as np
from a100sim.materials import read_usd_material
from PIL import Image
from pxr import Usd, UsdShade

# Decode the 256 possible file samples once, with the same float32 transfer
# used for full-resolution textures. Apply shader overrides before filtering.
_RAW_SAMPLES = np.arange(256, dtype=np.float32) / 255
_LINEAR_SAMPLES = np.where(_RAW_SAMPLES <= 0.04045, _RAW_SAMPLES / 12.92, ((_RAW_SAMPLES + 0.055) / 1.055) ** 2.4)


@lru_cache(maxsize=48)
def pixels(path: str) -> np.ndarray:
    """Read native texture channels into a bounded cache of RGBA arrays."""
    with Image.open(path) as image:
        return np.asarray(image.convert("RGBA"))


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
    source = pixels(path)
    samples = _RAW_SAMPLES if raw or channel is not None else _LINEAR_SAMPLES
    if channel is not None:
        source = source[:, :, channel : channel + 1]
    else:
        source = source[:, :, :3]
    scale = np.asarray(multiply if multiply is not None else 1.0)
    if scale.ndim == 0 or np.all(scale == scale.flat[0]):
        factor = scale if scale.ndim == 0 else scale[:1]
        table = (samples * factor).astype(np.float32)
        data = table[source]
    else:
        tables = (samples[:, None] * scale).astype(np.float32)
        data = np.empty(source.shape, dtype=np.float32)
        for index in range(source.shape[2]):
            data[:, :, index] = tables[:, index][source[:, :, index]]
    return {
        "type": "bitmap",
        "bitmap": mi.Bitmap(np.ascontiguousarray(data, np.float32)),
        "raw": True,
        "wrap_mode": "repeat",
    }


class DoorColor(mi.Texture):
    """Evaluate MI_DoorA's ordered RGB mask blends after filtering its textures."""

    def __init__(self, base, mask, colors):
        super().__init__(mi.Properties())
        self.base, self.mask, self.colors = base, mask, colors

    def eval_3(self, si, active=True):
        mask = self.mask.eval_3(si, active)
        tint = self.colors[3]
        for channel in range(3):
            tint = dr.lerp(tint, self.colors[channel], mask[channel])
        return self.base.eval_3(si, active) * tint

    eval = eval_3

    def eval_1(self, si, active=True):
        return mi.luminance(self.eval_3(si, active))

    def is_spatially_varying(self):
        return True

    def to_string(self):
        return "MI_DoorA masked base color"


class DoorNormal(mi.Texture):
    """Unpack the filtered XY normal channels used by OmniUe4Base."""

    def __init__(self, normal):
        super().__init__(mi.Properties())
        self.normal = normal

    def eval_3(self, si, active=True):
        sample = self.normal.eval_3(si, active)
        x, y = 2 * sample.x - 1, 2 * sample.y - 1
        z = dr.sqrt(dr.maximum(0, 1 - x * x - y * y))
        # OmniUe4Base uses tangent_u * x - tangent_v * y + normal * z.
        return 0.5 * (mi.Color3f(x, -y, z) + 1)

    def is_spatially_varying(self):
        return True

    def to_string(self):
        return "MI_DoorA tangent-space normal"


def door_surface(shader: UsdShade.Shader) -> dict[str, Any]:
    """Translate the registered MI_DoorA program and its USD instance overrides."""
    defaults = {
        "BaseColor": "T_PanicDoor_bc.png",
        "ColorMask": "T_PanicDoor_mask.png",
        "Normal": "T_PanicDoor_n.png",
        "AO_R_Rough_G_Metallic_B_": "T_PanicDoor_ao_r_m_e.png",
    }
    mdl = shader.GetSourceAsset("mdl")
    paths = {}
    for name, filename in defaults.items():
        path = texture_path(shader, name)
        if path is None:
            fallback = Path(mdl.resolvedPath or mdl.path).parent / "Textures" / filename
            if not fallback.is_file():
                raise ValueError(f"Native MI_DoorA texture is unavailable: {fallback}")
            path = str(fallback)
        paths[name] = path
    colors = [list(input_value(shader, f"Color0{i}", (1, 1, 1, 1)))[:3] for i in range(1, 5)]
    base = mi.load_dict(bitmap(paths["BaseColor"]))
    native_scalar = mi.variant() == "scalar_rgb"
    if any(color != [1, 1, 1] for color in colors):
        mask = mi.load_dict(bitmap(paths["ColorMask"], raw=True))
        tints = [mi.Color3f(color) for color in colors]
        if native_scalar:
            base = mi.load_dict(
                {
                    "type": "bf_door_pbr",
                    "image": base,
                    "mask": mask,
                    **{f"color{i}": tint for i, tint in enumerate(tints)},
                }
            )
        else:
            base = DoorColor(base, mask, tints)
    normal = mi.load_dict(bitmap(paths["Normal"], raw=True))
    normal = mi.load_dict({"type": "bf_door_pbr", "image": normal}) if native_scalar else DoorNormal(normal)
    orm = paths["AO_R_Rough_G_Metallic_B_"]
    return {
        "type": "normalmap",
        "normalmap": normal,
        "material": {
            "type": "principled",
            "base_color": base,
            "roughness": bitmap(orm, channel=1, raw=True),
            "metallic": bitmap(orm, channel=2, raw=True),
            "specular": 0.5,
        },
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
    if source == "MI_DoorA":
        return door_surface(shader), None
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
            _RAW_SAMPLES[pixels(albedo)[:, :, 3:4]] * float(input_value(shader, "Opacity_Multiply", 1))
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
            data = _RAW_SAMPLES[pixels(orm)[:, :, 3:4]] * color * intensity
            emitter = {
                "type": "area",
                "radiance": {
                    "type": "bitmap",
                    "bitmap": mi.Bitmap(np.ascontiguousarray(data, np.float32)),
                    "raw": True,
                },
            }
    return surface, emitter
