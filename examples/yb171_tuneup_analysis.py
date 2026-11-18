"""Small offline models for the Yb171 tune-up notebook, never hardware fits.

All angular arguments use radians. These illustrative models omit spontaneous
emission, motional dynamics, AC Stark shifts and experimental SPAM. The projected
CZ diagonal is not normalized: its missing norm is excitation leakage.
"""

from functools import lru_cache
from itertools import product

import numpy as np
from scipy.linalg import expm
from scipy.optimize import least_squares

ALLXY_PAIRS = (
    ("I", "I"),
    ("X", "X"),
    ("Y", "Y"),
    ("X", "Y"),
    ("Y", "X"),
    ("x", "I"),
    ("y", "I"),
    ("x", "y"),
    ("y", "x"),
    ("x", "Y"),
    ("y", "X"),
    ("X", "y"),
    ("Y", "x"),
    ("x", "X"),
    ("X", "x"),
    ("y", "Y"),
    ("Y", "y"),
    ("X", "I"),
    ("Y", "I"),
    ("x", "x"),
    ("y", "y"),
)


def _model_arrays(*values):
    """Validate real finite model inputs and apply NumPy broadcasting."""
    if any(np.iscomplexobj(value) for value in values):
        raise ValueError("Model inputs must be real and finite")
    arrays = [np.asarray(value, dtype=float) for value in values]
    if any(not np.isfinite(value).all() for value in arrays):
        raise ValueError("Model inputs must be real and finite")
    return np.broadcast_arrays(*arrays)


def rabi_probability(duration_s, detuning_hz, rabi_hz):
    """Model P(1) from |0> under one finite rectangular two-level pulse.

    Frequencies are cycles/s, not angular frequencies. Define
    ``delta = f_drive - f_transition`` and positive resonant Rabi frequency f_R.
    The rotating-frame Hamiltonian is H/hbar = pi * (f_R X - delta Z).
    NumPy broadcasting supports duration/frequency grids. Model duration may
    be zero; a DSL pulse still requires strictly positive duration.
    No relaxation, SPAM or actual measurement is included.

    >>> float(rabi_probability(0, 0, 1_000))
    0.0
    >>> bool(np.isclose(rabi_probability(0.0005, 0, 1_000), 1))
    True
    """
    duration, delta, rate = _model_arrays(duration_s, detuning_hz, rabi_hz)
    if np.any(duration < 0) or np.any(rate <= 0):
        raise ValueError("Require nonnegative duration and positive Rabi frequency")
    effective = np.hypot(rate, delta)
    with np.errstate(over="raise", invalid="raise"):
        try:
            angle = np.pi * (effective * duration)
            return (rate / effective * np.sin(angle)) ** 2
        except FloatingPointError as exc:
            raise ValueError("Inputs exceed the finite Rabi model range") from exc


def ramsey_probability(
    delay_s, detuning_hz, rabi_hz, pulse_duration_s, analysis_phase_rad=0
):
    """Model P(1) for square pulse -> free delay -> square pulse, from |0>.

    Both pulses have the same positive duration and positive resonant Rabi
    frequency f_R. Detuning ``delta = f_drive - f_transition`` acts during
    the finite pulses as well as the nonnegative free delay. The first pulse
    phase is zero; ``analysis_phase_rad`` is the second pulse's drive phase.
    In the drive frame, H/hbar = pi * (f_R(cos(phi)X + sin(phi)Y) - delta Z),
    and free evolution is diag(exp(+i*pi*delta*tau), exp(-i*pi*delta*tau)).
    Thus the ideal instantaneous pi/2 limit is
    (1 + cos(2*pi*delta*tau + analysis_phase_rad))/2, with this phase convention.
    All inputs broadcast as NumPy arrays. This is a model, not acquired data.

    >>> bool(np.isclose(ramsey_probability(0.001, 0, 1_000, 0.00025), 1))
    True
    >>> bool(np.isclose(
    ...     ramsey_probability(0.001, 0, 1_000, 0.00025, np.pi), 0))
    True
    """
    delay, delta, rate, duration, phase = _model_arrays(
        delay_s, detuning_hz, rabi_hz, pulse_duration_s, analysis_phase_rad
    )
    if np.any(delay < 0) or np.any(rate <= 0) or np.any(duration <= 0):
        raise ValueError(
            "Require nonnegative delay, positive Rabi frequency and pulse duration"
        )
    effective = np.hypot(rate, delta)
    with np.errstate(over="raise", invalid="raise"):
        try:
            angle = np.pi * (effective * duration)
            sine, cosine = np.sin(angle), np.cos(angle)
            longitudinal = delta / effective * sine
            transverse = rate / effective * sine
            # First pulse U(0)|0>, followed by detuned free precession.
            free_phase = np.pi * (delta * delay)
            state_0 = (cosine + 1j * longitudinal) * np.exp(1j * free_phase)
            state_1 = -1j * transverse * np.exp(-1j * free_phase)
            # Row 1 of U(analysis_phase) applied to that state.
            final_1 = (
                -1j * transverse * np.exp(1j * phase) * state_0
                + (cosine - 1j * longitudinal) * state_1
            )
            return np.clip(np.abs(final_1) ** 2, 0, 1)
        except FloatingPointError as exc:
            raise ValueError("Inputs exceed the finite Ramsey model range") from exc


def rotation(theta_rad, phase_rad=0):
    """Return exp[-i theta (cos(phi) X + sin(phi) Y)/2]."""
    axis = np.array([[0, np.exp(-1j * phase_rad)], [np.exp(1j * phase_rad), 0]])
    return np.cos(theta_rad / 2) * np.eye(2) - 1j * np.sin(theta_rad / 2) * axis


def allxy_expected():
    """Ideal P(1), starting in |0>, for the chronological 21-pair sequence."""
    gates = {
        "I": np.eye(2),
        "X": rotation(np.pi),
        "Y": rotation(np.pi, np.pi / 2),
        "x": rotation(np.pi / 2),
        "y": rotation(np.pi / 2, np.pi / 2),
    }
    return np.array([abs((gates[b] @ gates[a])[1, 0]) ** 2 for a, b in ALLXY_PAIRS])


def _xy_data(x, probabilities, minimum):
    x = np.asarray(x, dtype=float)
    y = np.asarray(probabilities, dtype=float)
    if x.ndim != 1 or y.shape != x.shape or len(x) < minimum:
        raise ValueError(f"Require matching 1D arrays with >= {minimum} points")
    if not np.isfinite(x).all() or not np.isfinite(y).all():
        raise ValueError("Data must be finite")
    if np.any((y < 0) | (y > 1)):
        raise ValueError("Probabilities must lie in [0, 1]")
    return x, y


def fit_fringe(phases_rad, probabilities):
    """Unweighted diagnostic fit to offset + amplitude*cos(phi + phase).

    Contrast means peak-to-peak 2*amplitude, not visibility divided by offset.
    Real count data need a binomial likelihood and shot/sequence resampling.
    """
    x, y = _xy_data(phases_rad, probabilities, 3)
    design = np.column_stack([np.ones_like(x), np.cos(x), np.sin(x)])
    if np.linalg.matrix_rank(design) != 3:
        raise ValueError("Phase samples cannot identify offset and both quadratures")
    a, b, c = np.linalg.lstsq(design, y, rcond=None)[0]
    amplitude = np.hypot(b, c)
    return {
        "offset": float(a),
        "amplitude": float(amplitude),
        "contrast": float(2 * amplitude),
        "phase_rad": float(np.arctan2(-c, b)) if amplitude > 1e-12 else np.nan,
        "residual_rms": float(np.sqrt(np.mean((design @ [a, b, c] - y) ** 2))),
    }


def fit_decay(depths, probabilities):
    """Fit decreasing A*p**m+B within the probability range, no fidelity claim.

    This least-squares illustration has no confidence interval. In particular,
    a global mirror decay is not the four-dimensional depolarizing parameter
    assumed by a full two-qubit Clifford RB fidelity conversion.
    """
    x, y = _xy_data(depths, probabilities, 4)
    if np.any(x < 0) or len(np.unique(x)) < 4:
        raise ValueError("Require at least four distinct nonnegative depths")
    if np.ptp(y) < 1e-10:
        raise ValueError("Constant data do not identify a decay rate")

    def residual(parameters):
        offset, fraction, p = parameters
        return offset + (1 - offset) * fraction * p**x - y

    candidates = [
        least_squares(
            residual,
            [min(y) * 0.8, 0.8, p],
            bounds=(0, 1),
            ftol=1e-12,
            xtol=1e-12,
            gtol=1e-12,
        )
        for p in (0.8, 0.95, 0.995)
    ]
    result = min(candidates, key=lambda item: np.sum(item.fun**2))
    offset, fraction, p = result.x
    return {
        "A": float((1 - offset) * fraction),
        "p": float(p),
        "B": float(offset),
        "residual_rms": float(np.sqrt(np.mean(result.fun**2))),
        "at_boundary": bool(np.any((result.x < 1e-6) | (result.x > 1 - 1e-6))),
    }


def _two_pulse_state(delta, duration, phase, *, blockade=None, coupling=1):
    def propagator(phi):
        if blockade is None:
            hamiltonian = np.array(
                [
                    [0, coupling * np.exp(-1j * phi) / 2],
                    [coupling * np.exp(1j * phi) / 2, -delta],
                ]
            )
        else:
            # Symmetric basis |cc>, (|cr>+|rc>)/sqrt(2), |rr>.
            # Collective drive cannot populate the antisymmetric dark state.
            g = np.exp(-1j * phi) / np.sqrt(2)
            hamiltonian = np.array(
                [
                    [0, g, 0],
                    [g.conjugate(), -delta, g],
                    [0, g.conjugate(), blockade - 2 * delta],
                ]
            )
        return expm(-1j * duration * hamiltonian)

    return (propagator(phase) @ propagator(0))[:, 0]


def collective_cz_model(
    delta_over_omega, duration_omega, phase_rad, *, blockade_over_omega=np.inf
):
    """Projected two-pulse CZ model with an explicit optical-phase convention.

    H/Omega = [[0, exp(-i phi)/2], [exp(i phi)/2, -Delta/Omega]].
    Both Rydberg pulses have duration duration_omega/Omega. A single atom sees
    coupling Omega; two blockaded clock atoms see sqrt(2)*Omega. A finite
    blockade uses the complete driven symmetric subspace including |rr>.

    logical_diag is [1, -a, -a, b] in |00>,|01>,|10>,|11> order, including
    ideal same-phase pi clock shelving and return pulses. Thus the two clock
    transfers supply minus one per shelved atom. A clock return phase chi
    multiplies it by [1, exp(-i chi), exp(-i chi), exp(-2i chi)].
    """
    values = [delta_over_omega, duration_omega, phase_rad]
    if not np.isfinite(values).all() or duration_omega < 0:
        raise ValueError("Require finite parameters and nonnegative pulse duration")
    if np.isnan(blockade_over_omega):
        raise ValueError("Blockade cannot be NaN")
    single = _two_pulse_state(*values)
    pair = _two_pulse_state(
        *values,
        coupling=np.sqrt(2),
        blockade=None if np.isinf(blockade_over_omega) else blockade_over_omega,
    )
    a, b = single[0], pair[0]
    relative = b * np.conj(a) ** 2
    return {
        "single_return": float(abs(a) ** 2),
        "pair_return": float(abs(b) ** 2),
        "conditional_phase_rad": (
            float(np.angle(relative)) if abs(relative) > 1e-12 else np.nan
        ),
        "logical_diag": np.array([1, -a, -a, b]),
        "single_state": single,
        "pair_state": pair,
        "clock_return_phase_rad": float(np.angle(-a)),
    }


def solve_collective_cz_seed():
    """Find an ideal infinite-blockade starting point, not a hardware optimum.

    The often quoted positive 3.902-rad phase uses the opposite phase convention.
    Here the equivalent solution is -3.902 rad, returned modulo 2*pi.
    """

    def residual(values):
        single = _two_pulse_state(*values)
        pair = _two_pulse_state(*values, coupling=np.sqrt(2))
        conditional = pair[0] * np.conj(single[0]) ** 2
        return [
            single[1].real,
            single[1].imag,
            pair[1].real,
            pair[1].imag,
            conditional.real + 1,
            conditional.imag,
        ]

    result = least_squares(
        residual,
        [0.377, 4.293, -3.902],
        bounds=([0.2, 3.5, -4.5], [0.6, 5, -3]),
        ftol=1e-13,
        xtol=1e-13,
        gtol=1e-13,
    )
    if not result.success or np.linalg.norm(result.fun) > 1e-8:
        raise RuntimeError("Ideal CZ seed solver failed its closure/phase checks")
    delta, duration, phase = result.x
    return {
        "delta_over_omega": float(delta),
        "duration_omega": float(duration),
        "phase_rad": float(phase % (2 * np.pi)),
    }


def global_mirror_word(depth, seed=0):
    """Return a reproducible common-RF/CZ forward-and-inverse stress test.

    Depth counts forward CZ layers; the complete word contains 2*depth CZs.
    This is not uniform Clifford RB. Common rotations preserve exchange
    symmetry, and forward/inverse structure can cancel coherent errors.
    """
    if type(depth) is not int or depth < 0:
        raise ValueError("Depth must be a nonnegative integer")
    rng = np.random.default_rng(seed)
    choices = ("x90", "xm90", "y90", "ym90")
    inverse = {"x90": "xm90", "xm90": "x90", "y90": "ym90", "ym90": "y90", "cz": "cz"}
    forward = tuple(
        gate for _ in range(depth) for gate in (str(rng.choice(choices)), "cz")
    )
    return forward + tuple(inverse[gate] for gate in reversed(forward))


def global_word_unitary(word):
    """Independent ideal matrix for a common-RF/CZ word; no DSL execution."""
    unitary = np.eye(4, dtype=complex)
    angles = {
        "x90": (np.pi / 2, 0),
        "xm90": (-np.pi / 2, 0),
        "y90": (np.pi / 2, np.pi / 2),
        "ym90": (-np.pi / 2, np.pi / 2),
        "x180": (np.pi, 0),
        "y180": (np.pi, np.pi / 2),
    }
    for name in word:
        if name == "cz":
            gate = np.diag([1, 1, 1, -1])
        elif name in angles:
            local = rotation(*angles[name])
            gate = np.kron(local, local)
        else:
            raise ValueError(f"Unknown collective gate {name!r}")
        unitary = gate @ unitary
    return unitary


def _state_key(state):
    """Remove global phase only; retain relative phases of all amplitudes."""
    nonzero = np.flatnonzero(abs(state) > 1e-9)
    normalized = state * np.exp(-1j * np.angle(state[nonzero[0]]))
    return tuple(np.round(np.r_[normalized.real, normalized.imag], 9))


@lru_cache(maxsize=1)
def symmetric_state_templates():
    """Twelve symmetric stabilizer states, each with fixed-length init/recovery.

    Init: R1 R2 CZ R3 R4 R5. Recovery: R1 R2 CZ R3 R4. Each R is a
    common +/-pi/2 pulse about X or Y. Returned tuples are immutable. The state
    labels are local deterministic catalogue indices, not a physical ordering.
    """
    choices = ("x90", "xm90", "y90", "ym90")
    initial = {}
    recovery = {}
    for rotations in product(choices, repeat=5):
        word = rotations[:2] + ("cz",) + rotations[2:]
        state = global_word_unitary(word)[:, 0]
        initial.setdefault(_state_key(state), word)
    for rotations in product(choices, repeat=4):
        word = rotations[:2] + ("cz",) + rotations[2:]
        # U |state> = |00> means |state> = U.dagger |00>.
        state = global_word_unitary(word).conj().T[:, 0]
        recovery.setdefault(_state_key(state), word)
    if len(initial) != 12 or initial.keys() != recovery.keys():
        raise RuntimeError("SSB templates must cover the same twelve symmetric states")
    return tuple((key, initial[key], recovery[key]) for key in sorted(initial))


def ssb_sequences(
    total_cz_counts=(2, 4, 6), random_rf_count=24, repeats_per_state=1, seed=171
):
    """Fixed-RF symmetric-state return sequences, balanced over twelve inputs.

    Each sequence starts with five RF pulses and one CZ, applies L random common
    RF pulses with a CZ after the first n-2 of them, and ends with four RF pulses
    and one CZ selected to recover |00>. Thus n counts every CZ, including init
    and recovery; the RF pulse count is always L+9. This is a symmetric-subspace
    benchmark, not full two-qubit Clifford RB. In particular it does not probe
    arbitrary antisymmetric input errors. No echo block is forced into the word.
    """
    depths = tuple(total_cz_counts)
    if not depths or any(type(n) is not int or n < 2 for n in depths):
        raise ValueError("Total CZ counts must be integers >= 2")
    if len(set(depths)) != len(depths):
        raise ValueError("Total CZ counts must not repeat")
    if type(random_rf_count) is not int or random_rf_count < max(depths) - 2:
        raise ValueError("Random RF count must be an integer >= max CZ count - 2")
    if type(repeats_per_state) is not int or repeats_per_state < 1:
        raise ValueError("Repeats per state must be a positive integer")
    templates = symmetric_state_templates()
    recoveries = {key: recovery for key, _, recovery in templates}
    rng = np.random.default_rng(seed)
    choices = ("x90", "xm90", "y90", "ym90")
    sequences = []
    for n in depths:
        for initial_state, (_, init, _) in enumerate(templates):
            for repeat in range(repeats_per_state):
                body = []
                for index in range(random_rf_count):
                    body.append(str(rng.choice(choices)))
                    if index < n - 2:
                        body.append("cz")
                before_recovery = init + tuple(body)
                state = global_word_unitary(before_recovery)[:, 0]
                recovery = recoveries[_state_key(state)]
                word = before_recovery + recovery
                sequences.append(
                    {
                        "word": word,
                        "initial_state": initial_state,
                        "total_cz_count": n,
                        "rf_count": random_rf_count + 9,
                        "seed": seed,
                        "repeat": repeat,
                    }
                )
    return sequences


def fit_ssb_short(depths, probabilities):
    """Short-depth diagnostic A*F_ssb**n fit, with no 3/4 conversion or offset.

    A zero-asymptote short-depth model is an explicit approximation, not a
    universal RB law. Inspect residuals and depth-window stability; long-depth
    mixing, leakage, SPAM changes and imperfect randomization can invalidate it.
    F_ssb describes this protocol, not general four-dimensional gate fidelity.
    """
    x, y = _xy_data(depths, probabilities, 3)
    if np.any(x < 0) or len(np.unique(x)) < 3:
        raise ValueError("Require three distinct nonnegative CZ counts")
    if np.ptp(y) < 1e-10:
        raise ValueError("Constant data do not identify an SSB decay")
    result = least_squares(
        lambda p: p[0] * p[1] ** x - y,
        [min(0.99, max(y)), 0.995],
        bounds=(0, 1),
        ftol=1e-13,
        xtol=1e-13,
        gtol=1e-13,
    )
    return {
        "A": float(result.x[0]),
        "F_ssb": float(result.x[1]),
        "residual_rms": float(np.sqrt(np.mean(result.fun**2))),
    }


def global_echo_word(block_count, seed=171, variant="cz"):
    """Diagnostic R_i G X180_global G blocks followed by common RF recovery.

    G is CZ, same-phase ideal clock roundtrip (Z tensor Z), or idle. For CZ,
    G X180_global G is Y180_global up to a global phase. For clock/idle it is
    X180_global. Recovery inverts this known common rotation word. Because the
    echo suppresses single-qubit phases, it is a complementary diagnostic and
    must not replace un-echoed phase calibration or symmetric-state benchmarking.
    """
    if type(block_count) is not int or block_count < 0:
        raise ValueError("Block count must be a nonnegative integer")
    if variant not in {"cz", "clock", "idle"}:
        raise ValueError("Variant must be cz, clock or idle")
    rng = np.random.default_rng(seed)
    choices = ("x90", "xm90", "y90", "ym90")
    inverse = {
        "x90": "xm90",
        "xm90": "x90",
        "y90": "ym90",
        "ym90": "y90",
        "x180": "x180",
        "y180": "y180",
    }
    actual, effective = [], []
    for _ in range(block_count):
        gate = str(rng.choice(choices))
        actual.extend([gate, "G", "x180", "G"])
        effective.extend([gate, "y180" if variant == "cz" else "x180"])
    return tuple(actual) + tuple(inverse[gate] for gate in reversed(effective))


def global_echo_unitary(word, variant="cz"):
    """Evaluate ideal diagnostic echo including explicit G placeholders."""
    if variant not in {"cz", "clock", "idle"}:
        raise ValueError("Variant must be cz, clock or idle")
    g = {
        "cz": np.diag([1, 1, 1, -1]),
        "clock": np.diag([1, -1, -1, 1]),
        "idle": np.eye(4),
    }[variant]
    unitary = np.eye(4, dtype=complex)
    for gate in word:
        unitary = (g if gate == "G" else global_word_unitary((gate,))) @ unitary
    return unitary
