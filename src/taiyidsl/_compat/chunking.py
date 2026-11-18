"""Compatibility exports for host-plan readers and offline chunk construction."""

from taiyidsl.compiler.chunking import build_execution_plan as build_execution_plan
from taiyidsl.compiler.chunking import host_loop_lines as host_loop_lines
from taiyidsl.compiler.chunking import resolve_auto_chunks as resolve_auto_chunks
from taiyidsl.host_plan import execution_order as execution_order
from taiyidsl.host_plan import execution_plan_for as execution_plan_for
from taiyidsl.host_plan import point_execution_layout as point_execution_layout
