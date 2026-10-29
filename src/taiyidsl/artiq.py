"""ARTIQ 9 source export and an independent, deliberately limited driver emulator.

No ARTIQ installation or hardware is used by this module.  Verification checks
the generated Python against the declared device timing contract, not against
an FPGA or the real ARTIQ compiler.
"""

from __future__ import annotations

import ast
import hashlib
import json
import math
import sys
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import fields, is_dataclass
from fractions import Fraction
from pathlib import Path
from types import ModuleType


class ExportError(ValueError):
    """A compiled plan cannot be represented by the supported exporter."""


def _jsonable(value):
    if hasattr(value, "to_dict"):
        return _jsonable(value.to_dict())
    if is_dataclass(value):
        return {f.name: _jsonable(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(v) for v in value]
    if isinstance(value, Fraction):
        return {"numerator": value.numerator, "denominator": value.denominator}
    if isinstance(value, Path):
        return str(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if hasattr(value, "item"):
        return value.item()
    raise TypeError(f"Cannot serialize {type(value).__name__}")


def _devices(compiled):
    return compiled.setup.devices


def _origin_grid(devices):
    grids = [int(getattr(device, "grid_mu", 1)) for device in devices.values()]
    grids += [
        int(getattr(device, "config_grid_mu", 8))
        for device in devices.values()
        if device.kind == "dds"
    ]
    return math.lcm(*grids) if grids else 1


def _words(event, device):
    p = event.params
    frequency, amplitude, phase = (
        float(p["frequency"]),
        float(p["amplitude"]),
        float(p.get("phase", 0.0)),
    )
    if not all(math.isfinite(v) for v in (frequency, amplitude, phase)):
        raise ExportError("DDS parameters must be finite")
    sysclk = float(getattr(device, "sysclk_hz", 1e9))
    if not 0 <= frequency < sysclk / 2 or not 0 <= amplitude <= 1:
        raise ExportError("DDS frequency or amplitude outside supported range")
    ftw_unsigned = round(frequency / sysclk * (1 << 32)) & 0xFFFFFFFF
    # ARTIQ AD9910 accepts an int32 bit pattern, including negative FTWs.
    ftw = ftw_unsigned if ftw_unsigned < (1 << 31) else ftw_unsigned - (1 << 32)
    pow_ = round((phase / (2 * math.pi) % 1.0) * (1 << 16)) & 0xFFFF
    asf = round(amplitude * 0x3FFF)
    return ftw, pow_, asf


def _ordered_events(point):
    # Stable ordering matters for same-timestamp edges. The compiler owns ties.
    return sorted(point.physical_events, key=lambda event: event.start_mu)


def _validate_plan(compiled):
    if hasattr(compiled, "assert_integrity"):
        compiled.assert_integrity()
    devices = _devices(compiled)
    if [p.index for p in compiled.points] != list(range(len(compiled.points))):
        raise ExportError("Compiled point indices must be contiguous starting at zero")
    allowed = {"dds_config", "dds_on", "dds_off", "ttl_on", "ttl_off"}
    for point in compiled.points:
        cursor = 0
        for event in _ordered_events(point):
            if event.kind not in allowed:
                raise ExportError(f"Unsupported physical event: {event.kind}")
            if event.signal not in devices:
                raise ExportError(f"Unknown device: {event.signal}")
            if int(event.start_mu) != event.start_mu or event.start_mu < cursor:
                raise ExportError(
                    f"Point {point.index}: event {event.kind} at {event.start_mu} mu "
                    f"occurs before the driver cursor {cursor} mu; "
                    "serialize preparation calls"
                )
            cursor = int(event.start_mu)
            device = devices[event.signal]
            if event.kind == "dds_config":
                expected_duration = int(device.config_duration_mu)
                if event.duration_mu != expected_duration:
                    raise ExportError(
                        "DDS event duration differs from the device driver contract"
                    )
                _words(event, device)
                cursor += expected_duration
        end = max(int(point.duration_mu) + int(point.origin_mu), cursor)
        if end < 0:
            raise ExportError("Negative shot end")


def _alignment(signature, template):
    """Map an ordered signature onto a supersequence without changing event order."""
    positions, cursor = [], 0
    for item in signature:
        while cursor < len(template) and template[cursor] != item:
            cursor += 1
        if cursor == len(template):
            return None
        positions.append(cursor)
        cursor += 1
    return positions


def _point_end(point):
    return max(
        int(point.duration_mu + point.origin_mu),
        max(
            (int(e.start_mu + e.duration_mu) for e in point.physical_events), default=0
        ),
    )


def _program_groups(compiled):
    """Group only by compiled structure, never by experiment/template name.

    Descending-size representatives make zero-pulse/zero-delay points optional
    subsequences of the normal program. Different event order/device sequences
    stay in separate groups. No RTIO action is inferred, merged or rescheduled.
    """
    groups = []
    for point in sorted(
        compiled.points, key=lambda p: (-len(p.physical_events), p.index)
    ):
        events = _ordered_events(point)
        signature = tuple((e.kind, e.signal) for e in events)
        for group in groups:
            positions = _alignment(signature, group["signature"])
            if positions is not None:
                break
        else:
            group = {
                "signature": signature,
                "points": [],
                "slots": {},
                "columns": [],
                "rows": {},
            }
            groups.append(group)
            positions = list(range(len(signature)))
        group["points"].append(point)
        group["slots"][point.index] = dict(zip(positions, events))
    groups.sort(key=lambda g: min(p.index for p in g["points"]))
    for index, group in enumerate(groups):
        group["index"] = index
        group["points"].sort(key=lambda p: p.index)
    return groups


def _kernel_header(method, origin_grid, parameterized=False):
    signature = (
        f"{method}(self, values: TArray(TInt64, 1))"
        if parameterized
        else f"{method}(self)"
    )
    return [
        "    @kernel",
        f"    def {signature}:",
        "        self.core.reset()",
        f"        t0 = ((now_mu() + {origin_grid - 1}) // {origin_grid}) "
        f"* {origin_grid}",
        "        at_mu(t0)",
    ]


def _event_lines(
    kind, attr, start, *, words=None, reference=None, duration=None, indent="        "
):
    lines = [f"{indent}at_mu(t0 + {start})"]
    if kind == "dds_config":
        ftw, pow_, asf = words
        lines += [
            f"{indent}self.{attr}.set_mu({ftw}, pow_={pow_}, asf={asf}, "
            f"phase_mode=PHASE_MODE_TRACKING, ref_time_mu=t0 + {reference})",
            f"{indent}if now_mu() > t0 + {start} + {duration}:",
            f"{indent}    raise ValueError('DDS driver exceeded "
            "declared configuration budget')",
        ]
    elif kind.startswith("dds_"):
        lines.append(f"{indent}self.{attr}.sw.{kind[4:]}()")
    else:
        lines.append(f"{indent}self.{attr}.{kind[4:]}()")
    return lines


def _parameterized_kernel(group, compiled, attributes, origin_grid):
    vectors = {}
    declarations = []

    def expression(label, values, present=None, register=False):
        values = tuple(int(v) for v in values)
        selected = (
            [v for v, active in zip(values, present) if active]
            if present is not None
            else list(values)
        )
        if selected and len(set(selected)) == 1:
            return str(selected[0])
        if values not in vectors:
            column = len(vectors)
            vectors[values] = label
            group["columns"].append(label)
            declarations.append(f"        {label} = values[{column}]")
        name = vectors[values]
        return f"int32({name})" if register else name

    body = []
    points = group["points"]
    for slot, (kind, device_name) in enumerate(group["signature"]):
        events = [group["slots"][p.index].get(slot) for p in points]
        present = [e is not None for e in events]
        enabled = None
        if not all(present):
            enabled = expression(f"emit_{kind}_{slot}", present)
            body.append(f"        if {enabled} != 0:")
        indent = "            " if enabled is not None else "        "
        start = expression(
            f"{kind}_{slot}_mu", [e.start_mu if e else 0 for e in events], present
        )
        kwargs = {}
        if kind == "dds_config":
            device = _devices(compiled)[device_name]
            words = [_words(e, device) if e else (0, 0, 0) for e in events]
            kwargs["words"] = tuple(
                expression(
                    f"{name}_{slot}", [w[i] for w in words], present, register=True
                )
                for i, name in enumerate(("ftw", "pow", "asf"))
            )
            kwargs["reference"] = expression(
                f"phase_reference_{slot}_mu",
                [
                    e.params.get("ref_time_mu", p.origin_mu) if e else 0
                    for p, e in zip(points, events)
                ],
                present,
            )
            kwargs["duration"] = str(int(device.config_duration_mu))
        body.append(f"{indent}# {kind}: {device_name}")
        body.extend(
            _event_lines(kind, attributes[device_name], start, indent=indent, **kwargs)
        )
    end = expression("shot_end_mu", [_point_end(p) for p in points])
    vectors_in_order = list(vectors)
    for row, point in enumerate(points):
        group["rows"][point.index] = [vector[row] for vector in vectors_in_order]
    lines = _kernel_header(f"kernel_{group['index']}", origin_grid, parameterized=True)
    lines += declarations + body
    lines += [
        f"        at_mu(t0 + {end})",
        "        self.core.wait_until_mu(now_mu())",
        "",
    ]
    return lines


def _generate(compiled, mode):
    if mode not in {"auto", "unrolled"}:
        raise ValueError("Export mode must be 'auto' or 'unrolled'")
    _validate_plan(compiled)
    devices = _devices(compiled)
    origin_grid = _origin_grid(devices)
    names = sorted(devices)
    attributes = {name: f"device_{index}" for index, name in enumerate(names)}
    groups = _program_groups(compiled) if mode == "auto" else []
    kernel_lines = []
    for group in groups:
        kernel_lines.extend(
            _parameterized_kernel(group, compiled, attributes, origin_grid)
        )
    lines = [
        '"""Generated for ARTIQ 9; real compilation and hardware are NOT VERIFIED.',
        "The timing contract, optical calibration and device_db must be validated",
        "before use. Set result_adapter before run(); "
        'no detector driver is assumed."""',
        "from artiq.experiment import EnvExperiment, kernel, at_mu, now_mu",
        "from artiq.coredevice.ad9910 import PHASE_MODE_TRACKING",
        "",
        "class GeneratedExperiment(EnvExperiment):",
        "    def build(self):",
        "        self.core = self.get_device('core')",
    ]
    if mode == "auto":
        lines[6:6] = [
            "from artiq.experiment import TArray, TInt64",
            "from numpy import array, int32, int64",
        ]
    for name in names:
        lines.append(f"        self.{attributes[name]} = self.get_device({name!r})")
    lines += [
        "        self.result_adapter = None",
    ]
    dispatch = {}
    if mode == "auto":
        rows = {}
        for group in groups:
            for point in group["points"]:
                dispatch[point.index] = group["index"]
            rows.update(group["rows"])
        lines.append(
            "        self._point_groups = "
            f"{tuple(dispatch[p.index] for p in compiled.points)!r}"
        )
        for group in groups:
            lines.append(
                f"        # kernel_{group['index']} values columns: "
                f"{', '.join(group['columns']) or '(all constants)'}"
            )
        lines.append("        self._point_values = [")
        for point in compiled.points:
            lines.append(
                f"            array({rows[point.index]!r}, dtype=int64),  "
                f"# point {point.index}"
            )
        lines.append("        ]")
    lines += [
        "",
        "    @kernel",
        "    def initialize_devices(self):",
        "        self.core.reset()",
    ]
    for name in names:
        attribute = attributes[name]
        if devices[name].kind == "dds":
            lines += [
                f"        self.{attribute}.init()",
                "        self.core.break_realtime()",
                f"        self.{attribute}.sw.off()",
            ]
        else:
            lines.append(f"        self.{attribute}.off()")
    lines += ["        self.core.wait_until_mu(now_mu())", ""]
    lines.extend(kernel_lines)
    for point in compiled.points if mode == "unrolled" else []:
        lines += _kernel_header(f"point_{point.index}", origin_grid)
        for event in _ordered_events(point):
            kwargs = {}
            if event.kind == "dds_config":
                kwargs = {
                    "words": _words(event, devices[event.signal]),
                    "reference": int(event.params.get("ref_time_mu", point.origin_mu)),
                    "duration": int(event.duration_mu),
                }
            lines += _event_lines(
                event.kind, attributes[event.signal], int(event.start_mu), **kwargs
            )
        end = _point_end(point)
        lines += [
            f"        at_mu(t0 + {end})",
            "        self.core.wait_until_mu(now_mu())",
            "",
        ]
    lines += [
        "    def _validate_target(self):",
        "        if abs(self.core.ref_period - "
        f"{float(compiled.setup.tick_seconds)!r}) > 1e-18:",
        "            raise ValueError('Core reference period "
        "differs from compiled target')",
    ]
    for name in names:
        if devices[name].kind == "dds":
            expected_sysclk = float(getattr(devices[name], "sysclk_hz", 1e9))
            tolerance = max(1e-6, abs(expected_sysclk) * 1e-12)
            lines += [
                f"        if abs(self.{attributes[name]}.sysclk - "
                f"{expected_sysclk!r}) > {tolerance!r}:",
                "            raise ValueError('DDS clock differs "
                f"from compiled target: {name}')",
            ]
    lines += [
        "",
        "    def _execute_point(self, point):",
    ]
    if mode == "auto":
        if len(groups) == 1:
            lines.append("        self.kernel_0(self._point_values[point])")
        else:
            lines.append("        group = self._point_groups[point]")
            for group in groups:
                word = "if" if group["index"] == 0 else "elif"
                lines += [
                    f"        {word} group == {group['index']}:",
                    f"            self.kernel_{group['index']}"
                    "(self._point_values[point])",
                ]
            lines += [
                "        else:",
                "            raise ValueError('Unknown compiled kernel group')",
            ]
    else:
        lines.append("        getattr(self, 'point_' + str(point))()")
    lines += [
        "",
        "    def _run_shot(self, point, shot):",
        "        self.result_adapter.before_shot(point, shot)",
        "        self._execute_point(point)",
        "        self.result_adapter.after_shot(point, shot)",
        "",
        "    def run(self):",
        "        if self.result_adapter is None:",
        "            raise RuntimeError('A configured result_adapter "
        "is required; see manifest.json')",
        "        self._validate_target()",
        "        self.initialize_devices()",
    ]
    if compiled.averaging_mode == "cyclic":
        lines += [
            f"        for shot in range({compiled.shots}):",
            f"            for point in range({len(compiled.points)}):",
            "                self._run_shot(point, shot)",
        ]
    else:
        lines += [
            f"        for point in range({len(compiled.points)}):",
            f"            for shot in range({compiled.shots}):",
            "                self._run_shot(point, shot)",
        ]
    lines += ["        self.result_adapter.finish()", ""]
    source = "\n".join(lines)
    ast.parse(source)
    description = {
        "mode": mode,
        "kernel_count": len(groups) if mode == "auto" else len(compiled.points),
        "source_lines": len(source.splitlines()),
        "host_loops": "preserved",
        "groups": [
            {
                "kernel": f"kernel_{g['index']}",
                "points": [p.index for p in g["points"]],
                "parameter_columns": g["columns"],
                "event_slots": [
                    {"kind": kind, "device": dev} for kind, dev in g["signature"]
                ],
            }
            for g in groups
        ],
    }
    return source, description


def generate_source(compiled, *, mode="auto"):
    """Generate generic parameterized kernels by default; unrolled is a debug mode."""
    return _generate(compiled, mode)[0]


class _DriverClock:
    def __init__(self):
        self.cursor = 0
        self.origin = 0
        self.events = []
        self.awaiting_origin = True
        self.wait_endpoint = None

    def reset(self):
        self.cursor = (
            125_001  # Deliberately unaligned, to exercise exported origin alignment.
        )
        self.origin = self.cursor
        self.awaiting_origin = True
        self.wait_endpoint = None

    def at_mu(self, timestamp):
        if timestamp < self.cursor:
            raise ExportError(
                "Generated code moves backward through a blocking driver call"
            )
        self.cursor = int(timestamp)
        if self.awaiting_origin:
            self.origin = self.cursor
            self.awaiting_origin = False


class _MockCore:
    def __init__(self, clock, ref_period=1e-9):
        self.clock = clock
        self.ref_period = ref_period

    def reset(self):
        self.clock.reset()

    def break_realtime(self):
        self.clock.cursor += 125_000

    def wait_until_mu(self, timestamp):
        if timestamp < self.clock.cursor:
            raise ExportError("Invalid wait endpoint")
        self.clock.wait_endpoint = int(timestamp - self.clock.origin)


class _MockSwitch:
    def __init__(self, clock, name, prefix, grid_mu=1):
        self.clock, self.name, self.prefix = clock, name, prefix
        self.grid_mu = grid_mu

    def _edge(self, edge):
        if self.clock.cursor % self.grid_mu:
            raise ExportError(f"Switch event violates {self.name} timing grid")
        self.clock.events.append(
            {
                "kind": self.prefix + edge,
                "signal": self.name,
                "start_mu": self.clock.cursor - self.clock.origin,
            }
        )

    def on(self):
        self._edge("on")

    def off(self):
        self._edge("off")


class _MockDDS:
    def __init__(self, clock, name, device):
        self.clock, self.name, self.device = clock, name, device
        self.sysclk = float(getattr(device, "sysclk_hz", 1e9))
        self.grid_mu = int(getattr(device, "grid_mu", 1))
        self.config_grid_mu = int(getattr(device, "config_grid_mu", 8))
        self.sw = _MockSwitch(clock, name, "dds_", self.grid_mu)

    def init(self):
        pass  # Initialization is outside verified static shot timelines.

    def set_mu(self, ftw, pow_=0, asf=0x3FFF, phase_mode=-1, ref_time_mu=-1):
        if self.clock.cursor % self.config_grid_mu or ref_time_mu % self.grid_mu:
            raise ExportError(
                "DDS configuration or phase reference violates timing grid"
            )
        if phase_mode != 2:
            raise ExportError("Expected tracking phase mode")
        if (
            not -(1 << 31) <= ftw < (1 << 31)
            or not 0 <= pow_ < (1 << 16)
            or not 0 <= asf <= 0x3FFF
        ):
            raise ExportError("Invalid DDS tuning word")
        self.clock.events.append(
            {
                "kind": "dds_config",
                "signal": self.name,
                "start_mu": self.clock.cursor - self.clock.origin,
                "duration_mu": int(self.device.config_duration_mu),
                "ftw": ftw,
                "pow": pow_,
                "asf": asf,
                "ref_time_mu": int(ref_time_mu - self.clock.origin),
            }
        )
        # This duration belongs to the device contract, not to the event under test.
        self.clock.cursor += int(self.device.config_duration_mu)


@contextmanager
def _fake_artiq(clock, device_map):
    module_names = (
        "artiq",
        "artiq.experiment",
        "artiq.coredevice",
        "artiq.coredevice.ad9910",
    )
    previous = {name: sys.modules.get(name) for name in module_names}
    modules = {name: ModuleType(name) for name in module_names}

    class EnvExperiment:
        def get_device(self, name):
            return device_map[name]

    modules["artiq.experiment"].EnvExperiment = EnvExperiment
    modules["artiq.experiment"].kernel = lambda function: function
    modules["artiq.experiment"].at_mu = clock.at_mu
    modules["artiq.experiment"].now_mu = lambda: clock.cursor
    modules["artiq.experiment"].TInt64 = int
    modules["artiq.experiment"].TArray = lambda dtype, dimensions: object
    modules["artiq.coredevice.ad9910"].PHASE_MODE_TRACKING = 2
    try:
        sys.modules.update(modules)
        yield
    finally:
        for name, old in previous.items():
            if old is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = old


def _expected_events(compiled, point):
    expected = []
    for event in _ordered_events(point):
        item = {
            "kind": event.kind,
            "signal": event.signal,
            "start_mu": int(event.start_mu),
        }
        if event.kind == "dds_config":
            ftw, pow_, asf = _words(event, _devices(compiled)[event.signal])
            item.update(
                duration_mu=int(event.duration_mu),
                ftw=ftw,
                pow=pow_,
                asf=asf,
                ref_time_mu=int(event.params.get("ref_time_mu", point.origin_mu)),
            )
        expected.append(item)
    return expected


def _verify_host_order(instance, compiled):
    """Run emitted host loops with a checking adapter and no kernel work.

    Compare online rather than allocating the full point x shot product.
    """
    if compiled.averaging_mode == "cyclic":
        expected = (
            (p.index, shot) for shot in range(compiled.shots) for p in compiled.points
        )
    else:
        expected = (
            (p.index, shot) for p in compiled.points for shot in range(compiled.shots)
        )
    state = {"pending": None, "executed": False, "count": 0, "finished": False}
    sentinel = object()

    class Adapter:
        def before_shot(self, point, shot):
            if state["pending"] is not None or next(expected, sentinel) != (
                point,
                shot,
            ):
                raise ExportError(
                    "Generated host loop order differs from averaging plan"
                )
            state["pending"] = (point, shot)
            state["executed"] = False

        def after_shot(self, point, shot):
            if state["pending"] != (point, shot) or not state["executed"]:
                raise ExportError(
                    "Generated result adapter hooks differ from shot execution"
                )
            state["pending"] = None
            state["count"] += 1

        def finish(self):
            if state["pending"] is not None or next(expected, sentinel) is not sentinel:
                raise ExportError("Generated host loop omitted shots")
            state["finished"] = True

    def dispatch(point):
        if (
            state["pending"] is None
            or state["pending"][0] != point
            or state["executed"]
        ):
            raise ExportError(
                "Generated host point dispatch differs from adapter index"
            )
        state["executed"] = True

    instance.result_adapter = Adapter()
    instance._execute_point = dispatch
    # Legacy unrolled source may dispatch directly rather than via _execute_point.
    for point in compiled.points:
        setattr(
            instance, f"point_{point.index}", lambda index=point.index: dispatch(index)
        )
    instance.initialize_devices = lambda: None
    instance.run()
    if not state["finished"]:
        raise ExportError("Generated host loop did not finish result collection")
    return {
        "status": "passed",
        "shots_checked": state["count"],
        "averaging_mode": compiled.averaging_mode,
    }


def _verify_source(compiled, source):
    if hasattr(compiled, "assert_integrity"):
        compiled.assert_integrity()
    ast.parse(source)
    clock = _DriverClock()
    device_map = {"core": _MockCore(clock, float(compiled.setup.tick_seconds))}
    for name, device in _devices(compiled).items():
        device_map[name] = (
            _MockDDS(clock, name, device)
            if device.kind == "dds"
            else _MockSwitch(clock, name, "ttl_", int(getattr(device, "grid_mu", 1)))
        )
    namespace = {"__name__": "generated_offline_check"}
    point_reports = []
    traces = []
    with _fake_artiq(clock, device_map):
        exec(compile(source, "<generated-artiq>", "exec"), namespace)  # noqa: S102 -- trusted generated test artifact
        instance = namespace["GeneratedExperiment"]()
        instance.build()
        instance._validate_target()
        for point in compiled.points:
            clock.events = []
            if hasattr(instance, "_execute_point"):
                instance._execute_point(point.index)
            else:
                getattr(instance, f"point_{point.index}")()
            expected = _expected_events(compiled, point)
            if clock.events != expected:
                raise ExportError(
                    f"Point {point.index}: generated driver events "
                    "differ from compiled timeline"
                )
            endpoint = _point_end(point)
            if (
                clock.cursor - clock.origin != endpoint
                or clock.wait_endpoint != endpoint
            ):
                raise ExportError(
                    f"Point {point.index}: generated end/wait time "
                    "differs from compiled timeline"
                )
            trace = {
                "events": _jsonable(clock.events),
                "end_mu": endpoint,
                "wait_mu": clock.wait_endpoint,
            }
            traces.append(trace)
            point_reports.append(
                {
                    "point": point.index,
                    "events": len(clock.events),
                    "status": "passed",
                    "end_mu": endpoint,
                    "trace_sha256": hashlib.sha256(
                        json.dumps(trace, sort_keys=True).encode()
                    ).hexdigest(),
                }
            )
        host_order = _verify_host_order(instance, compiled)
    return {
        "offline_export_check": "passed",
        "python_syntax": "passed",
        "artiq_compile": "not_verified",
        "hardware": "not_verified",
        "points": point_reports,
        "host_order": host_order,
        "limitations": [
            "Mock verifies declared timing contracts, not real driver timing",
            "RTIO throughput and optical phase are not verified",
        ],
    }, traces


def verify_export(compiled, source_path=None, *, source=None, mode="auto"):
    """Execute trusted generated source against independent mock drivers, per point.

    Also checks completion time and actual host-loop order/hook association.
    This test harness executes Python; it is not a sandbox for untrusted source.
    """
    if source is None:
        source = (
            Path(source_path).read_text(encoding="utf-8")
            if source_path
            else generate_source(compiled, mode=mode)
        )
    return _verify_source(compiled, source)[0]


def verify_equivalence(compiled):
    """Compare executed compact and unrolled traces, not just their source plans."""
    auto_source, auto_info = _generate(compiled, "auto")
    unrolled_source, unrolled_info = _generate(compiled, "unrolled")
    auto_report, auto_traces = _verify_source(compiled, auto_source)
    unrolled_report, unrolled_traces = _verify_source(compiled, unrolled_source)
    if (
        auto_traces != unrolled_traces
        or auto_report["host_order"] != unrolled_report["host_order"]
    ):
        raise ExportError("Parameterized and unrolled execution are not equivalent")
    return {
        "status": "passed",
        "points": auto_report["points"],
        "host_order": auto_report["host_order"],
        "auto": auto_info,
        "unrolled": unrolled_info,
        "compared": [
            "ordered physical events",
            "event timestamps",
            "DDS words",
            "phase references",
            "driver durations",
            "shot end/wait times",
            "host order and result hooks",
        ],
        "artiq_compile": "not_verified",
        "hardware": "not_verified",
    }


def export_artiq(compiled, output_dir, *, mode="auto"):
    """Write reviewable source, the compiled plan, and verification manifest."""
    source, code_generation = _generate(compiled, mode)
    report = verify_export(compiled, source=source)
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    source_path, plan_path, manifest_path = (
        output / "experiment.py",
        output / "compiled.json",
        output / "manifest.json",
    )
    quantization = []
    for point in compiled.points:
        for event in point.physical_events:
            if event.kind != "dds_config":
                continue
            device = _devices(compiled)[event.signal]
            ftw, pow_, asf = _words(event, device)
            realized_frequency = (
                (ftw & 0xFFFFFFFF)
                / (1 << 32)
                * float(getattr(device, "sysclk_hz", 1e9))
            )
            realized_amplitude = asf / 0x3FFF
            realized_phase = pow_ / (1 << 16) * 2 * math.pi
            requested_phase = float(event.params.get("phase", 0)) % (2 * math.pi)
            quantization.append(
                {
                    "point": point.index,
                    "device": event.signal,
                    "start_mu": event.start_mu,
                    "words": {"ftw": ftw, "pow": pow_, "asf": asf},
                    "actual": {
                        "frequency": realized_frequency,
                        "amplitude": realized_amplitude,
                        "phase": realized_phase,
                    },
                    "error": {
                        "frequency": realized_frequency
                        - float(event.params["frequency"]),
                        "amplitude": realized_amplitude
                        - float(event.params["amplitude"]),
                        "phase": (realized_phase - requested_phase + math.pi)
                        % (2 * math.pi)
                        - math.pi,
                    },
                }
            )
    manifest = {
        "schema_version": 1,
        "name": compiled.name,
        "target": "ARTIQ 9 / Urukul AD9910 / TTL",
        "source_sha256": hashlib.sha256(source.encode()).hexdigest(),
        "averaging_mode": compiled.averaging_mode,
        "shots": compiled.shots,
        "tick_seconds": float(compiled.setup.tick_seconds),
        "origin_grid_mu": _origin_grid(_devices(compiled)),
        "devices": _jsonable(_devices(compiled)),
        "provenance": _jsonable(compiled.provenance),
        "verification": report,
        "code_generation": code_generation,
        "dds_register_quantization": quantization,
        "result_adapter": {
            "required": True,
            "host_methods": [
                "before_shot(point, shot)",
                "after_shot(point, shot)",
                "finish()",
            ],
            "notes": (
                "External detector transfer and loading are user-configured "
                "host integrations; no detector is assumed."
            ),
        },
        "hardware_prerequisites": [
            "Confirm device_db names, ARTIQ/gateware versions "
            "and declared driver durations",
            "AD9910 configuration grid includes the driver 8-mu cursor alignment; "
            "duration remains an assumed contract",
            "Validate DDS SYNC_CLK/IO_UPDATE alignment and common phase reference",
            "Validate optical path delays separately",
            "Inspect RTIO errors and core log during hardware acceptance",
        ],
    }
    source_path.write_text(source, encoding="utf-8")
    plan_path.write_text(
        json.dumps(_jsonable(compiled), ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    return {
        "source": source_path,
        "compiled": plan_path,
        "manifest": manifest_path,
        "verification": report,
    }
