"""Embedded cross-device API 0.3."""

from .._errors import MuError
from ._capture import PointRef
from .browser import Download, Tab, TabInfo, TabInventory
from .observations import AXNode, AXObservation, Image, VisionObservation
from .resources import App, Device, Display, NativeWindowInfo, NativeWindowInventory, Window
from .runtime import (
    AttachmentInfo,
    DeviceInventory,
    Inventory,
    ResourceInfo,
    Runtime,
    Session,
)
from .services import ClipboardContent, FileRef

__all__ = [
    "Tab",
    "Download",
    "TabInfo",
    "TabInventory",
    "FileRef",
    "AXNode",
    "AXObservation",
    "App",
    "Window",
    "NativeWindowInfo",
    "NativeWindowInventory",
    "ClipboardContent",
    "AttachmentInfo",
    "Device",
    "Display",
    "DeviceInventory",
    "Image",
    "Inventory",
    "MuError",
    "PointRef",
    "ResourceInfo",
    "Runtime",
    "Session",
    "VisionObservation",
]
