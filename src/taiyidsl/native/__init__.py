"""Offline native ARTIQ declarations; importing this package loads no kernels."""

from .components import NativeModule, NativeProgram, NativeStage
from .declarations import NativeKernelPackage
from .declarations import bind_native_point as bind_native_point
from .declarations import split_native_body as split_native_body
from .declarations import validate_native_resources as validate_native_resources
from .declarations import validate_native_timed_window as validate_native_timed_window

__all__ = ["NativeKernelPackage", "NativeModule", "NativeProgram", "NativeStage"]
