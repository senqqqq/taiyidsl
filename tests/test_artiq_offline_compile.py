"""Test compile planning only; these tests do not invoke the ARTIQ compiler."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).parents[1] / "examples" / "artiq_offline_compile.py"
spec = importlib.util.spec_from_file_location("artiq_offline_compile_example", SCRIPT)
offline = importlib.util.module_from_spec(spec)
spec.loader.exec_module(offline)


def test_compile_database_has_only_local_drivers_and_disconnected_core():
    manifest = {
        "tick_seconds": 1e-9,
        "devices": {
            "camera": {"kind": "ttl"},
            **{
                f"dds{i}": {
                    "kind": "dds",
                    "sysclk_hz": 1e9,
                    "bus": "spi0",
                    "update_group": "urukul0",
                }
                for i in range(2)
            },
        },
    }
    db = offline.compile_device_db(manifest, "rv32g")
    assert db["core"]["arguments"]["host"] is None
    assert db["core"]["arguments"]["target"] == "rv32g"
    assert all(entry["type"] == "local" for entry in db.values())
    first, second = [db[f"dds{i}"]["arguments"] for i in range(2)]
    assert first["cpld_device"] == second["cpld_device"]
    assert (first["chip_select"], second["chip_select"]) == (4, 5)
    board = db[first["cpld_device"]]["arguments"]
    assert db[board["io_update_device"]]["class"] == "TTLOut"
    assert (
        len(
            {
                entry["arguments"]["channel"]
                for entry in db.values()
                if "channel" in entry["arguments"]
            }
        )
        == 5
    )


def test_compile_database_rejects_incompatible_shared_board():
    manifest = {
        "tick_seconds": 1e-9,
        "devices": {
            f"dds{i}": {
                "kind": "dds",
                "sysclk_hz": 1e9,
                "bus": "spi0",
                "update_group": "urukul0",
            }
            for i in range(5)
        },
    }
    with pytest.raises(ValueError, match="one compile-only Urukul"):
        offline.compile_device_db(manifest, "rv32g")


@pytest.mark.parametrize("revision", [8, 9])
def test_compile_database_preserves_polar_selector_revision(revision):
    manifest = {
        "tick_seconds": 1e-9,
        "devices": {
            "dds": {
                "kind": "dds",
                "sysclk_hz": 1e9,
                "ram_destination": "powasf",
                "ram_profile_control": f"urukul_rev{revision}",
            }
        },
    }
    db = offline.compile_device_db(manifest, "rv32g")
    board = db["dds"]["arguments"]["cpld_device"]
    assert db[board]["arguments"]["proto_rev"] == revision


@pytest.mark.parametrize("revision", [8, 9])
def test_compile_database_honors_explicit_nonpolar_protocol(revision):
    manifest = {
        "tick_seconds": 1e-9,
        "devices": {
            "dds": {
                "kind": "dds",
                "sysclk_hz": 1e9,
                "ram_destination": "asf",
                "urukul_proto_rev": revision,
            }
        },
    }
    db = offline.compile_device_db(manifest, "rv32g")
    board = db["dds"]["arguments"]["cpld_device"]
    assert db[board]["arguments"]["proto_rev"] == revision


def test_compile_database_rejects_protocol_selector_disagreement():
    manifest = {
        "tick_seconds": 1e-9,
        "devices": {
            "dds": {
                "kind": "dds",
                "sysclk_hz": 1e9,
                "ram_destination": "powasf",
                "ram_profile_control": "urukul_rev8",
                "urukul_proto_rev": 9,
            }
        },
    }
    with pytest.raises(ValueError, match="selector contract"):
        offline.compile_device_db(manifest, "rv32g")


def kernel(method):
    method.artiq_embedded = SimpleNamespace(core_name="core")
    return method


class ParameterizedExperiment:
    _point_groups = (0, 0)
    _point_values = ([1, 2], [3, 4])

    @kernel
    def initialize_devices(self):
        raise AssertionError("Planning must not execute kernels")

    @kernel
    def kernel_0(self, values):
        raise AssertionError("Planning must not execute kernels")


def parameterized_manifest():
    return {
        "code_generation": {
            "mode": "auto",
            "kernel_count": 1,
            "groups": [{"kernel": "kernel_0", "points": [0, 1]}],
        }
    }


def test_kernel_jobs_include_initialization_and_every_parameter_row():
    jobs = offline.kernel_jobs(ParameterizedExperiment(), parameterized_manifest())
    assert jobs == [
        ("initialize_devices", None, []),
        ("kernel_0", 0, [[1, 2]]),
        ("kernel_0", 1, [[3, 4]]),
    ]


def test_kernel_jobs_reject_incomplete_manifest():
    manifest = parameterized_manifest()
    manifest["code_generation"]["groups"] = []
    with pytest.raises(ValueError, match="every parameter row"):
        offline.kernel_jobs(ParameterizedExperiment(), manifest)


def test_kernel_jobs_reject_missing_parameter_row():
    manifest = parameterized_manifest()
    manifest["code_generation"]["groups"][0]["points"] = [0]
    with pytest.raises(ValueError, match="every parameter row"):
        offline.kernel_jobs(ParameterizedExperiment(), manifest)


def test_kernel_jobs_reject_dispatch_mismatch():
    experiment = ParameterizedExperiment()
    experiment._point_groups = (0, 1)
    with pytest.raises(ValueError, match="dispatch differs"):
        offline.kernel_jobs(experiment, parameterized_manifest())


def test_unrolled_kernel_jobs_cover_every_point():
    class UnrolledExperiment:
        initialize_devices = ParameterizedExperiment.initialize_devices

        @kernel
        def point_0(self):
            raise AssertionError("Planning must not execute kernels")

        @kernel
        def point_1(self):
            raise AssertionError("Planning must not execute kernels")

    manifest = {"code_generation": {"mode": "unrolled", "kernel_count": 2}}
    assert offline.kernel_jobs(UnrolledExperiment(), manifest) == [
        ("initialize_devices", None, []),
        ("point_0", 0, []),
        ("point_1", 1, []),
    ]


@pytest.mark.parametrize("version", ["8.0", "10.0+dev", "unknown"])
def test_rejects_incompatible_artiq_version(version):
    with pytest.raises(ValueError, match="requires ARTIQ 9"):
        offline.validate_toolchain_version(version)


def test_accepts_artiq_9_version():
    offline.validate_toolchain_version("9.9698+a7303a7")


def test_rejects_driver_clock_mismatch():
    manifest = {
        "tick_seconds": 1e-9,
        "devices": {"dds": {"kind": "dds", "sysclk_hz": 1e9}},
    }
    devices = {
        "core": SimpleNamespace(ref_period=1e-9),
        "dds": SimpleNamespace(sysclk=800e6),
    }
    manager = SimpleNamespace(get=devices.__getitem__)
    with pytest.raises(ValueError, match="DDS clock differs"):
        offline.validate_driver_clocks(manager, manifest)
    devices["dds"].sysclk = 1e9
    offline.validate_driver_clocks(manager, manifest)
