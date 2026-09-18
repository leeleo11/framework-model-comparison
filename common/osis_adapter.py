"""Backward-compatible import path for the real PyOSIS adapter."""

from .pyosis_adapter import FilesystemOSISAdapter, PyOSISAdapter

__all__ = ["FilesystemOSISAdapter", "PyOSISAdapter"]
