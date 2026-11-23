"""Pinned native programs are audited without importing their ARTIQ modules."""

import hashlib
import textwrap

import pytest

from taiyidsl.ir import CompileError
from taiyidsl.native.audit import audit_native_sources


def _package(tmp_path, sources, **options):
    pins = []
    for module, text in sources.items():
        path = tmp_path / f"{module.replace('.', '_')}.py"
        raw = textwrap.dedent(text).encode()
        path.write_bytes(raw)
        pins.append(
            {
                "module": module,
                "path": str(path),
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
        )
    return {
        "module": "adapter",
        "class_name": "Stages",
        "sources": pins,
        "resources": ["ttl0", "zotino0", "urukul0_ch0"],
        **options,
    }


def _one_method(tmp_path, body, *, timing="prefix", helpers="", **options):
    package = _package(
        tmp_path,
        {
            "adapter": (
                "from artiq.experiment import *\n"
                "class Stages(EnvExperiment):\n"
                "    def build(self):\n"
                "        self.setattr_device('core')\n"
                "        self.ttl = self.get_device('ttl0')\n"
                "    @kernel\n"
                "    def execute(self):\n"
                + textwrap.indent(textwrap.dedent(body), "        ")
                + "\n"
                + textwrap.indent(textwrap.dedent(helpers), "    ")
            )
        },
        **options,
    )
    return package, [{"method": "execute", "args": [], "timing": timing}]


def test_pinned_inherited_kernels_and_devices_are_resolved_without_import(tmp_path):
    package = _package(
        tmp_path,
        {
            "lib.mot": """
                from artiq.experiment import *
                raise RuntimeError('This module must not be executed during export')
                class GreenMOT(EnvExperiment):
                    def build(self):
                        self.setattr_device('core')
                        self.setattr_device('zotino0')
                        self.dds = self.get_device('urukul0_ch0')
                    @kernel
                    def init_hardware(self):
                        self.core.reset()
                        self.zotino0.init()
                    @kernel
                    def prepare_GreenMOT(self):
                        self.core.break_realtime()
                        self.compress()
                    @kernel
                    def compress(self):
                        for i in range(80):
                            self.zotino0.set_dac([0.1], [0])
                            self.dds.cpld.set_profile(0, 7)
                            delay(200*us)
                    @kernel
                    def cleanup(self):
                        self.core.wait_until_mu(now_mu())
                        self.core.break_realtime()
            """,
            "adapter": """
                from artiq.experiment import *
                from lib.mot import GreenMOT as Base
                class Stages(Base):
                    def build(self):
                        Base.build(self)
                        self.camera = self.get_device('ttl0')
                    @kernel
                    def prepare_MOT(self):
                        self.prepare_GreenMOT()
                    @kernel
                    def readout(self, duration):
                        self.camera.on()
                        delay(duration)
                        self.camera.off()
            """,
        },
        initialize_method="init_hardware",
        finalize_method="cleanup",
    )
    result = audit_native_sources(
        package,
        [
            {"method": "prepare_MOT", "timing": "prefix", "args": []},
            {"method": "readout", "timing": "suffix", "args": [10e-6]},
        ],
    )
    assert result["status"] == "passed"
    assert result["methods"]["lib.mot.GreenMOT.compress"]["timing_roles"] == ["prefix"]
    assert "self.dds.cpld.set_profile" in result["driver_calls"]
    assert result["device_bindings"]["camera"] == "ttl0"
    assert result["entries"][0]["timing"] == "initialize"
    assert result["entries"][1]["timing"] == "finalize"


def test_changed_source_pin_fails_before_inspection(tmp_path):
    package, calls = _one_method(tmp_path, "self.ttl.on()")
    path = package["sources"][0]["path"]
    with open(path, "a", encoding="utf-8") as stream:
        stream.write("\n# changed\n")
    with pytest.raises(CompileError, match="Pinned source changed"):
        audit_native_sources(package, calls)


@pytest.mark.parametrize(
    "operation", ["reset", "break_realtime", "wait_until_mu", "get_rtio_counter_mu"]
)
@pytest.mark.parametrize("timing", ["timed", "suffix"])
def test_strict_calls_reject_indirect_cursor_mutations(tmp_path, operation, timing):
    args = "now_mu()" if operation == "wait_until_mu" else ""
    package, calls = _one_method(
        tmp_path,
        "self.helper()",
        timing=timing,
        helpers=f"@kernel\ndef helper(self):\n    self.core.{operation}({args})\n",
    )
    with pytest.raises(CompileError, match=operation):
        audit_native_sources(package, calls)


def test_prefix_allows_recovery_but_not_reset_through_local_alias(tmp_path):
    package, calls = _one_method(
        tmp_path,
        "core = self.core\ncore.break_realtime()\ncore.wait_until_mu(now_mu())",
    )
    assert audit_native_sources(package, calls)["status"] == "passed"
    package, calls = _one_method(tmp_path, "core = self.core\ncore.reset()")
    with pytest.raises(CompileError, match="reset"):
        audit_native_sources(package, calls)


def test_finalize_may_wait_but_cannot_reset(tmp_path):
    package, _ = _one_method(tmp_path, "self.core.reset()", finalize_method="execute")
    with pytest.raises(CompileError, match="reset"):
        audit_native_sources(package, [])


@pytest.mark.parametrize("decorator", ["", "@rpc\n", "@host_only\n"])
def test_python_or_rpc_helpers_are_rejected(tmp_path, decorator):
    package, calls = _one_method(
        tmp_path,
        "self.helper()",
        helpers=f"{decorator}def helper(self):\n    pass\n",
    )
    with pytest.raises(CompileError, match="RPC|host-only"):
        audit_native_sources(package, calls)


@pytest.mark.parametrize("body", ["print('hello')", "getattr(self.core, 'reset')()"])
def test_implicit_rpc_and_dynamic_calls_are_rejected(tmp_path, body):
    package, calls = _one_method(tmp_path, body)
    with pytest.raises(CompileError, match="Host/dynamic|Dynamic native"):
        audit_native_sources(package, calls)


def test_portable_pinned_free_helper_is_audited(tmp_path):
    package = _package(
        tmp_path,
        {
            "helpers": """
                from artiq.experiment import portable
                import math
                @portable
                def angle(value):
                    return math.sin(value)
            """,
            "adapter": """
                from artiq.experiment import *
                from helpers import angle
                class Stages(EnvExperiment):
                    @kernel
                    def execute(self):
                        delay(abs(angle(0.3))*us)
            """,
        },
    )
    result = audit_native_sources(package, [{"method": "execute", "timing": "suffix"}])
    assert result["methods"]["adapter.Stages.execute"]["calls"] == ["helpers.angle"]


def test_unpinned_base_and_unresolved_helpers_fail(tmp_path):
    package = _package(
        tmp_path,
        {"adapter": "from missing import Base\nclass Stages(Base):\n    pass\n"},
    )
    with pytest.raises(CompileError, match="Unpinned native base"):
        audit_native_sources(package, [])
    package, calls = _one_method(tmp_path, "self.missing()")
    with pytest.raises(CompileError, match="Unresolved native self helper"):
        audit_native_sources(package, calls)


def test_dynamic_dispatch_and_super_follow_actual_inheritance(tmp_path):
    package = _package(
        tmp_path,
        {
            "adapter": """
                from artiq.experiment import *
                class Base(EnvExperiment):
                    @kernel
                    def execute(self):
                        self.helper()
                    @kernel
                    def helper(self):
                        self.core.reset()
                class Stages(Base):
                    @kernel
                    def execute(self):
                        super().execute()
                    @kernel
                    def helper(self):
                        delay(10*us)
            """
        },
    )
    result = audit_native_sources(package, [{"method": "execute", "timing": "suffix"}])
    assert "adapter.Base.helper" not in result["methods"]
    assert "adapter.Stages.helper" in result["methods"]


def test_device_resources_and_argument_counts_are_explicit(tmp_path):
    package, calls = _one_method(tmp_path, "self.ttl.on()")
    package["resources"] = []
    with pytest.raises(CompileError, match="missing from package resources"):
        audit_native_sources(package, calls)
    package["resources"] = ["ttl0"]
    calls[0]["args"] = [1]
    with pytest.raises(CompileError, match="argument count"):
        audit_native_sources(package, calls)


def test_recursive_native_helpers_are_rejected(tmp_path):
    package, calls = _one_method(tmp_path, "self.execute()")
    with pytest.raises(CompileError, match="Recursive native"):
        audit_native_sources(package, calls)


def test_kernel_flags_are_not_mistaken_for_a_runtime_call(tmp_path):
    package = _package(
        tmp_path,
        {
            "adapter": """
                from artiq.experiment import *
                class Stages(EnvExperiment):
                    @kernel(flags={'fast-math'})
                    def execute(self):
                        delay(10*us)
            """
        },
    )
    assert (
        audit_native_sources(package, [{"method": "execute", "timing": "suffix"}])[
            "status"
        ]
        == "passed"
    )


def test_shadowed_builtin_is_audited_as_user_code(tmp_path):
    package = _package(
        tmp_path,
        {
            "adapter": """
                from artiq.experiment import *
                def abs(value):
                    print(value)
                    return value
                class Stages(EnvExperiment):
                    @kernel
                    def execute(self):
                        delay(abs(10)*us)
            """
        },
    )
    with pytest.raises(CompileError, match="host RPC"):
        audit_native_sources(package, [{"method": "execute", "timing": "suffix"}])


@pytest.mark.parametrize("timing", ["timed", "suffix"])
def test_while_loop_cannot_claim_a_strict_timeline(tmp_path, timing):
    body = "while self.ready:\n    delay(10*us)"
    package, calls = _one_method(tmp_path, body, timing=timing)
    with pytest.raises(CompileError, match="while loops"):
        audit_native_sources(package, calls)
    calls[0]["timing"] = "prefix"
    assert audit_native_sources(package, calls)["status"] == "passed"


def test_timed_cooling_keeps_prefix_recovery_outside_its_call_graph(tmp_path):
    package = _package(
        tmp_path,
        {
            "adapter": """
                from artiq.experiment import *
                class Stages(EnvExperiment):
                    @kernel
                    def prepare_MOT(self):
                        self.core.break_realtime()
                        delay(100*ms)
                    @kernel
                    def cooling(self, duration):
                        self.cooling_tail(duration)
                    @kernel
                    def cooling_tail(self, duration):
                        for i in range(4):
                            delay(duration / 4)
            """
        },
    )
    report = audit_native_sources(
        package,
        [
            {"method": "prepare_MOT", "timing": "prefix"},
            {"method": "cooling", "timing": "timed", "args": [0.001]},
        ],
    )
    assert report["status"] == "passed"
    assert report["methods"]["adapter.Stages.prepare_MOT"]["timing_roles"] == ["prefix"]
    assert report["methods"]["adapter.Stages.cooling_tail"]["timing_roles"] == ["timed"]


def test_a_prefix_audit_cannot_hide_recovery_in_reused_timed_cooling(tmp_path):
    package, calls = _one_method(tmp_path, "self.core.break_realtime()")
    calls.append({"method": "execute", "timing": "timed"})
    with pytest.raises(CompileError, match="break_realtime.*strict native timed"):
        audit_native_sources(package, calls)


@pytest.mark.parametrize(
    "body", ["print('cooling')", "getattr(self.core, 'break_realtime')()"]
)
def test_timed_cooling_rejects_rpc_and_dynamic_helpers(tmp_path, body):
    package, calls = _one_method(tmp_path, body, timing="timed")
    with pytest.raises(CompileError, match="Host/dynamic|Dynamic native"):
        audit_native_sources(package, calls)
