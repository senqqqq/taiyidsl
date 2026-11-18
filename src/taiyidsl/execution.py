"""Select touched outputs and disclose shared-resource initialization contracts."""


def device_execution_plan(compiled):
    inventory = compiled.setup.devices
    used = sorted(
        {event.signal for point in compiled.points for event in point.physical_events}
    )
    unknown = set(used) - set(inventory)
    if unknown:
        raise ValueError(
            f"Physical events reference unknown devices: {sorted(unknown)}"
        )
    unused = sorted(set(inventory) - set(used))

    def resources(device):
        return {
            f"{kind}:{value}"
            for kind in ("bus", "update_group")
            if (value := getattr(device, kind, None)) is not None
        }

    required = set().union(*(resources(inventory[name]) for name in used))
    shared = {
        resource: sorted(
            name for name in inventory if resource in resources(inventory[name])
        )
        for resource in sorted(required)
    }
    partial = {
        resource: members
        for resource, members in shared.items()
        if set(members) - set(used)
    }
    external = sorted(
        name
        for name in used
        if inventory[name].kind == "dds"
        and any(name in members for members in partial.values())
    )
    return {
        "used_devices": used,
        "unused_devices": unused,
        "shared_resources": shared,
        "prepared_shared_resources_required": sorted(
            key for key, members in partial.items() if set(members) & set(external)
        ),
        "externally_initialized_dds": external,
        "initialize_dds": sorted(
            name
            for name in used
            if inventory[name].kind == "dds" and name not in external
        ),
        "limitations": [
            "Inventory must include all channels on the declared shared resources; "
            "automatic initialization assumes ownership of those resources",
            "core.reset requires exclusive ownership of the RTIO execution domain",
            "DDS init and IO_UPDATE can affect shared resources; "
            "no electrical isolation is proven",
            "Prepared-resource acknowledgement is a host contract, "
            "not a hardware measurement",
        ],
    }
