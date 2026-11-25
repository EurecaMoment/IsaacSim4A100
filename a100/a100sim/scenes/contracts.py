"""Describe retained scene operations without importing Kit or a transport SDK.

These schemas describe the public arguments. Uploaded asset handles are resolved
by the gateway; stage-dependent validation remains owned by the scene service.
"""

from __future__ import annotations

from typing import Any

__all__ = ["OPERATIONS", "PARAMETER_SCHEMAS"]

OPERATIONS: dict[str, str] = {
    "asset.import": "Import an uploaded USD into a retained independent scene session",
    "scene.edit": "Apply bounded transforms and supported scalar attributes before simulation",
    "physics.step": "Advance and retain one scene using CUDA PhysX",
    "world.observe": "Read measured CUDA body velocities from the last physics step",
    "scene.inspect": "Read paged USD hierarchy, world transforms and collider attributes",
    "scene.capture": "Render up to 32 authored cameras from the same retained physical state",
    "scene.export": "Export the retained flattened USD and optional dependency package",
    "scene.close": "Close the session while preserving its immutable artifacts",
}

_SESSION_ID = {
    "type": "string",
    "pattern": "^[a-f0-9]{32}$",
    "description": "Retained scene session returned by asset.import; unrelated session families are not compatible.",
}
_PRIM_PATH = {"type": "string", "minLength": 2, "description": "Absolute, nonroot USD prim path."}


def _object(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    """Describe a bounded public argument object."""
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


def _vector(default: list[int], description: str) -> dict[str, Any]:
    """Describe one three-dimensional transform component."""
    return {
        "type": "array",
        "items": {"type": "number"},
        "minItems": 3,
        "maxItems": 3,
        "default": default,
        "description": description,
    }


PARAMETER_SCHEMAS: dict[str, dict[str, Any]] = {
    "asset.import": _object(
        {
            "session_id": _SESSION_ID,
            "source": _object(
                {
                    "asset_id": {"type": "string", "description": "Asset handle returned by UploadAsset."},
                    "relative_path": {
                        "type": "string",
                        "description": "Uploaded .usd, .usda, .usdc or .usdz file within the asset handle.",
                    },
                },
                ["asset_id", "relative_path"],
            ),
            "path": {**_PRIM_PATH, "default": "/World/Scene"},
            "fix_base": {
                "type": "boolean",
                "description": (
                    "Omit to preserve authored USD references. When supplied, normalize native asset units, "
                    "center geometry and configure colliders; false also creates a dynamic rigid body."
                ),
            },
            "mass_kg": {
                "type": "number",
                "default": 0.25,
                "description": "Rigid body mass in kilograms, applied only when fix_base is false.",
            },
            "source_up_axis": {
                "type": "string",
                "description": (
                    "Override source up-axis when fix_base is supplied. Y (case-insensitive) rotates to Z-up; "
                    "other values leave orientation unchanged. Omit to use the uploaded USD metadata."
                ),
            },
            "physics": {
                "type": "object",
                "description": (
                    "Contact inputs applied when fix_base is supplied. Additional metadata is accepted but "
                    "does not override collider selection or physical behavior."
                ),
                "properties": {
                    "static_friction": {"type": "number", "default": 0.5},
                    "dynamic_friction": {"type": "number", "default": 0.5},
                    "restitution": {"type": "number", "default": 0},
                    "collider": {"description": "Caller metadata only; collider mode is selected by fix_base."},
                    "mass_source": {"description": "Caller provenance metadata only; mass is supplied by mass_kg."},
                },
                "additionalProperties": True,
            },
        },
        ["source"],
    ),
    "scene.edit": _object(
        {
            "session_id": _SESSION_ID,
            "changes": {
                "type": "array",
                "maxItems": 256,
                "description": "Apply changes in order. Any edit invalidates the retained physics observation.",
                "items": {
                    "oneOf": [
                        _object(
                            {
                                "op": {"type": "string", "enum": ["transform"]},
                                "path": _PRIM_PATH,
                                "translation": _vector([0, 0, 0], "Translation in stage meters."),
                                "rotation_deg": _vector([0, 0, 0], "XYZ Euler rotation in degrees."),
                                "scale": _vector([1, 1, 1], "Scale along each axis."),
                            },
                            ["op", "path"],
                        ),
                        {
                            **_object(
                                {
                                    "op": {"type": "string", "enum": ["set"]},
                                    "path": _PRIM_PATH,
                                    "attribute": {"type": "string", "enum": ["a100sim:semanticLabel"]},
                                    "type": {"type": "string", "enum": ["string"]},
                                    "value": {"description": "Semantic label, converted to a string by the service."},
                                },
                                ["op", "path", "attribute", "type", "value"],
                            ),
                            "additionalProperties": True,
                        },
                    ],
                },
            },
        },
        ["session_id", "changes"],
    ),
    "physics.step": _object(
        {
            "session_id": _SESSION_ID,
            "steps": {"type": "integer", "minimum": 1, "maximum": 1200, "default": 180},
            "dt": {"type": "number", "minimum": 0.0001, "maximum": 0.02, "default": 1 / 60},
        },
        ["session_id"],
    ),
    "world.observe": _object(
        {
            "session_id": _SESSION_ID,
            "bodies": {
                "type": "array",
                "items": _PRIM_PATH,
                "description": "Omit for all measured rigid bodies. Run physics.step after the last scene mutation.",
            },
            "imu": {"type": "boolean", "enum": [False], "default": False},
            "contacts": {"type": "boolean", "enum": [False], "default": False},
        },
        ["session_id"],
    ),
    "scene.inspect": _object(
        {
            "session_id": _SESSION_ID,
            "limit": {"type": "integer", "minimum": 1, "maximum": 2000, "default": 2000},
            "cursor": {
                "anyOf": [
                    {"type": "integer", "minimum": 0, "maximum": 200000},
                    {"type": "string", "pattern": "^[0-9]+$"},
                ],
                "default": 0,
                "description": (
                    "Pass next_cursor unchanged while has_more is true. Both integer offsets and returned digit "
                    "strings are accepted; the service requires a value from 0 to 200000. Omit on the first page."
                ),
            },
        },
        ["session_id"],
    ),
    "scene.capture": _object(
        {
            "session_id": _SESSION_ID,
            "cameras": {
                "type": "array",
                "items": _PRIM_PATH,
                "minItems": 1,
                "maxItems": 32,
                "description": "Authored cameras in this scene. Run physics.step after the last scene mutation.",
            },
            "size": {"type": "integer", "minimum": 64, "maximum": 2048, "default": 768},
            "spp": {"type": "integer", "minimum": 1, "maximum": 1024, "default": 32},
            "annotations": {"type": "boolean", "default": True},
        },
        ["session_id", "cameras"],
    ),
    "scene.export": _object(
        {
            "session_id": _SESSION_ID,
            "format": {"type": "string", "enum": ["usd", "usdz"], "default": "usd"},
        },
        ["session_id"],
    ),
    "scene.close": _object(
        {
            "session_id": _SESSION_ID,
            "delete": {
                "type": "boolean",
                "enum": [False],
                "default": False,
                "description": "Closing preserves the retained scene and captured artifacts; deletion is unsupported.",
            },
        },
        ["session_id"],
    ),
}
