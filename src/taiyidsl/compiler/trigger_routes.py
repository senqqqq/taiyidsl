"""Validate logical trigger bindings against a fixed physical Spectrum route."""


def resolve_trigger_signal(group, mapping, setup, explicit=None, *, source=""):
    """Resolve an optional logical name without changing the physical contract."""
    from ..config.hardware import ControlBinding
    from ..ir import CompileError

    signal = (
        explicit if explicit is not None else getattr(group, "trigger_signal", None)
    )
    if signal is None:
        return None
    if not isinstance(signal, str) or not signal:
        raise CompileError(f"Invalid logical Spectrum trigger at {source}")
    try:
        bindings = mapping.bindings(signal)
    except (ValueError, TypeError) as exc:
        raise CompileError(
            f"Invalid Spectrum trigger {signal!r}: {exc} at {source}"
        ) from exc
    expected = set(group.trigger_device_names)
    names = [binding.device for binding in bindings]
    if len(names) != len(set(names)) or set(names) != expected:
        raise CompileError(
            f"Spectrum trigger {signal!r} must map to exactly "
            f"{'one TTL' if len(expected) == 1 else 'the configured TTL set'} "
            f"{sorted(expected)} without changing the configured physical path "
            f"at {source}"
        )
    for binding in bindings:
        device = setup.devices.get(binding.device)
        if device is None or device.kind != "ttl":
            raise CompileError(
                f"Spectrum trigger {signal!r} requires a TTL at {source}"
            )
        if binding != ControlBinding(binding.device):
            raise CompileError(
                f"Spectrum trigger {signal!r} cannot apply signal transforms "
                f"at {source}"
            )
    return signal
