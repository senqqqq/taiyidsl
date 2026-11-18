"""Neutral-atom pulse-level experiment framework. No hardware is accessed on import."""

__version__ = "0.1.0"

from pathlib import Path as _Path

from taiyidsl.experiments.ramsey import ramsey_experiment, ramsey_gates
from taiyidsl.experiments.rb_sections import rb_sequence_section
from taiyidsl.experiments.rb_sequences import (
    Clifford,
    RBGate,
    RBSequence,
    clifford_catalog,
    generate_rb_sequences,
    sequence_tableau,
)
from taiyidsl.experiments.templates import rabi, ramsey, randomized_benchmarking

from .backends import Backend, BackendCapabilities, get_backend, register_backend
from .compiler.artifact import CompiledExperiment
from .compiler.pipeline import compile_experiment
from .compiler.store import CompilationStore
from .compiler.streaming import compile_stream
from .config.hardware import (
    ControlBinding,
    ControlRecipe,
    Device,
    DeviceCalibration,
    DeviceSetup,
    Protocol,
    ProtocolStep,
    SignalMap,
    TriggerSequence,
    TriggerStep,
)
from .config.models import (
    CalibrationPolicy,
    CalibrationProposal,
    DriveParameters,
    Position,
    Qubit,
    QubitParameters,
    QubitStore,
    ReadoutParameters,
    TransitionParameters,
)
from .config.optics import OpticsConfig
from .config.pair_calibration import PairCalibration
from .config.spectrum import SpectrumCard, SpectrumConfig, SpectrumGroup, SpectrumOutput
from .config.transport import IZ, MoveProfile, SiteRef, TransportConfig
from .dsl.authoring import (
    average,
    case,
    experiment,
    realtime,
    repeat,
    stage,
    sweep,
    triggered_sequence,
)
from .dsl.experiment import Experiment
from .dsl.instructions import (
    Rz,
    acquire,
    barrier,
    delay,
    measure,
    move,
    parallel,
    play,
    prepare,
    reserve,
    reset_frame,
    reset_phase,
    rz,
    serial,
    shift_phase,
    strict_timing,
    trig,
    trigplay,
)
from .dsl.operations import (
    QubitOperations,
    echo_body,
    qubit_operation,
    rabi_body,
    ramsey_body,
    rb_body,
)
from .dsl.pulses import FlatTopGaussian, HalfSinePhase, PolynomialPhase, Square
from .dsl.sections import Section, add, layer, section
from .dsl.values import SweepParameter
from .ir import CompileError, PhysicalPlan, ScheduledEvent
from .lab import LabConfig, initialization_lab, reference_lab, reference_setup
from .native import NativeKernelPackage, NativeModule, NativeProgram, NativeStage
from .reporting.capture_view import compare_capture, write_capture_comparison
from .results import ResultSet, synthesize
from .results.analysis import analyze_rabi, analyze_ramsey, analyze_rb
from .results.imaging import ImageResults
from .results.rb_analysis import RBDecayFit, fit_rb_decay, interleaved_error_bound
from .results.rb_analysis import analyze_rb as analyze_rb_counts
from .results.rb_results import RBCounts, joint_rb_counts
from .runtime.contracts import ObservationCommit, RecoveryRequired, ShotContext
from .runtime.run_store import RunStore
from .runtime.streaming import StreamingRunner
from .units import Hz, MHz, Quantity, deg, kHz, m, mm, ms, ns, rad, s, turn, um, us

# Resolve historical flat imports on demand, after the normal package directory.
__path__.append(str(_Path(__file__).with_name("_compat")))

__all__ = [
    "s",
    "ms",
    "us",
    "ns",
    "Hz",
    "kHz",
    "MHz",
    "rad",
    "deg",
    "turn",
    "Quantity",
    "m",
    "mm",
    "um",
    "Position",
    "IZ",
    "SiteRef",
    "MoveProfile",
    "TransportConfig",
    "TransitionParameters",
    "Qubit",
    "QubitParameters",
    "DriveParameters",
    "ReadoutParameters",
    "QubitStore",
    "CalibrationProposal",
    "CalibrationPolicy",
    "Experiment",
    "Section",
    "section",
    "layer",
    "add",
    "qubit_operation",
    "rabi_body",
    "ramsey_body",
    "ramsey_experiment",
    "ramsey_gates",
    "echo_body",
    "rb_body",
    "experiment",
    "average",
    "sweep",
    "realtime",
    "triggered_sequence",
    "repeat",
    "case",
    "stage",
    "NativeKernelPackage",
    "NativeModule",
    "NativeProgram",
    "NativeStage",
    "QubitOperations",
    "PairCalibration",
    "Square",
    "FlatTopGaussian",
    "HalfSinePhase",
    "PolynomialPhase",
    "SweepParameter",
    "play",
    "move",
    "trigplay",
    "trig",
    "delay",
    "barrier",
    "reserve",
    "shift_phase",
    "reset_frame",
    "reset_phase",
    "rz",
    "Rz",
    "prepare",
    "measure",
    "acquire",
    "serial",
    "parallel",
    "strict_timing",
    "Device",
    "DeviceSetup",
    "SpectrumCard",
    "SpectrumConfig",
    "SpectrumGroup",
    "SpectrumOutput",
    "DeviceCalibration",
    "SignalMap",
    "ControlBinding",
    "ControlRecipe",
    "Protocol",
    "ProtocolStep",
    "TriggerSequence",
    "TriggerStep",
    "OpticsConfig",
    "reference_setup",
    "LabConfig",
    "reference_lab",
    "initialization_lab",
    "Backend",
    "BackendCapabilities",
    "get_backend",
    "register_backend",
    "PhysicalPlan",
    "ScheduledEvent",
    "compile_experiment",
    "CompiledExperiment",
    "CompileError",
    "CompilationStore",
    "compile_stream",
    "StreamingRunner",
    "RunStore",
    "ShotContext",
    "ObservationCommit",
    "RecoveryRequired",
    "compare_capture",
    "write_capture_comparison",
    "ResultSet",
    "ImageResults",
    "synthesize",
    "analyze_rabi",
    "analyze_ramsey",
    "analyze_rb",
    "analyze_rb_counts",
    "RBGate",
    "Clifford",
    "RBSequence",
    "clifford_catalog",
    "generate_rb_sequences",
    "sequence_tableau",
    "rb_sequence_section",
    "RBCounts",
    "joint_rb_counts",
    "RBDecayFit",
    "fit_rb_decay",
    "interleaved_error_bound",
    "rabi",
    "ramsey",
    "randomized_benchmarking",
]
