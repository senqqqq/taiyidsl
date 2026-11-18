"""Shared independent event, RAM effect and host-order checks."""

from __future__ import annotations

from taiyidsl.backends.artiq._contracts import (
    ExportError,
    _ordered_events,
    _words,
)
from taiyidsl.host_plan import execution_order, execution_plan_for


def _expected_events(compiled, point):
    devices = compiled.setup.devices
    expected = []
    for event in _ordered_events(point):
        if event.kind in {"dds_effect", "dds_ram_effect"}:
            continue
        item = {
            "kind": event.kind,
            "signal": event.signal,
            "start_mu": int(event.start_mu),
        }
        if event.kind == "dds_prepare":
            ftw, _, asf = _words(event, devices[event.signal])
            item.update(
                duration_mu=int(event.duration_mu),
                ftw=ftw,
                square_pow=int(event.params["square_pow"]),
                asf=asf,
            )
        elif event.kind == "dds_config":
            ftw, pow_, asf = _words(event, devices[event.signal])
            item.update(
                duration_mu=int(event.duration_mu),
                ftw=ftw,
                pow=pow_,
                asf=asf,
                ref_time_mu=int(event.params.get("ref_time_mu", point.origin_mu)),
            )
        elif event.kind == "dds_ram_start":
            ftw, _, _ = _words(event, devices[event.signal])
            item.update(
                duration_mu=int(event.duration_mu),
                ftw=ftw,
                ram_pow=event.params["ram_pow"],
                ram_step=event.params["ram_step"],
                ram_asf=event.params["ram_asf"],
                ram_profile=event.params["ram_profile"],
                ram_start=event.params["ram_start"],
                ram_end=event.params["ram_end"],
                ram_matched_latency=event.params.get("ram_matched_latency", 0),
            )
        elif event.kind in {"dds_ram_rearm", "dds_ram_phase"}:
            ftw, _, _ = _words(event, devices[event.signal])
            item.update(
                duration_mu=int(event.duration_mu),
                ftw=ftw,
                ram_pow=event.params["ram_pow"],
                ram_profile=event.params["ram_profile"],
            )
        expected.append(item)
    return expected


def _verify_ram_effects(compiled, point, clock):
    """Check committed buffers and independently check visible phase disturbances.

    The emulator derives effect times from the device contract and uploaded data.
    It does not infer equal phase/envelope latency from matched_latency_enable.
    """
    expected = []
    for event in point.physical_events:
        if event.kind != "dds_ram_effect":
            continue
        expected.append(
            {
                "signal": event.signal,
                "start_mu": event.start_mu,
                "carrier_effect_mu": event.params["carrier_effect_mu"],
                "ftw": _words(event, compiled.setup.devices[event.signal])[0],
                **{
                    k: event.params[k]
                    for k in ("ram_pow", "ram_step", "ram_asf", "ram_profile")
                },
            }
        )

    def key(event):
        return (event["start_mu"], event["signal"])

    if sorted(clock.ram_effects, key=key) != sorted(expected, key=key):
        raise ExportError("Committed RAM effects differ from the requested timeline")
    if not expected:
        return []
    gates = {}
    opened = {}
    for event in clock.events:
        name, kind = event["signal"], event["kind"]
        if kind == "dds_on":
            if name in opened:
                raise ExportError("Repeated RF gate rise without a falling edge")
            opened[name] = event["start_mu"]
        elif kind == "dds_off" and name in opened:
            gates.setdefault(name, []).append((opened.pop(name), event["start_mu"]))
    if opened:
        raise ExportError("Shot ends with an open RF gate")
    previous = {}
    for effect in sorted(clock.ram_effects, key=key):
        name = effect["signal"]
        prior = previous.get(name)
        previous[name] = effect
        if prior is None:
            continue
        device = compiled.setup.devices[name]
        cycles = round(device.sysclk_hz * compiled.setup.tick_seconds)
        if device.nco_mode == "continuous":
            if prior["ftw"] != effect["ftw"]:
                raise ExportError("Continuous NCO changed its FTW")
            if prior["ram_pow"] == effect["ram_pow"]:
                continue
            lo, hi = sorted((effect["carrier_effect_mu"], effect["start_mu"]))
            if any(max(lo, on) < min(hi, off) for on, off in gates.get(name, ())):
                raise ExportError("Continuous RAM phase changes inside a declared gate")
            continue
        continuous = (
            prior["ftw"] == effect["ftw"]
            and (
                (effect["carrier_effect_mu"] - prior["carrier_effect_mu"])
                * cycles
                * prior["ftw"]
                + ((prior["ram_pow"] - effect["ram_pow"]) << 16)
            )
            % (1 << 32)
            == 0
        )
        if continuous:
            continue
        interval = round(
            prior["ram_step"] * 4 / device.sysclk_hz / compiled.setup.tick_seconds
        )
        for index, amplitude in enumerate(prior["ram_asf"][:-1]):
            if not amplitude:
                continue
            start = max(
                prior["start_mu"] + index * interval, effect["carrier_effect_mu"]
            )
            end = min(prior["start_mu"] + (index + 1) * interval, effect["start_mu"])
            if any(max(start, on) < min(end, off) for on, off in gates.get(name, ())):
                raise ExportError(
                    "RAM carrier update changes a nonzero outgoing envelope sample"
                )
    return sorted(clock.ram_effects, key=key)


def _verify_host_order(instance, compiled):
    """Run emitted host loops with a checking adapter and no kernel work.

    Compare online rather than allocating the full point x shot product.
    """
    expected = execution_order(execution_plan_for(compiled))
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
