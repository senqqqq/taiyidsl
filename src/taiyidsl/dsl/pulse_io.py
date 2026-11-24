"""Read normalized complex envelopes for sampled pulses."""

import cmath
import csv
import math
from array import array
from pathlib import Path

import numpy as np

from taiyidsl.sample_assets import validate_sample_values


def _sample_columns(header, i, q, amplitude, phase):
    direct = i is not None or q is not None
    polar = amplitude is not None or phase is not None
    if direct and polar:
        raise ValueError("Choose either i and q or amplitude and phase")
    if direct or polar:
        pair = (amplitude, phase) if polar else (i, q)
        if any(not isinstance(name, str) or not name for name in pair):
            raise ValueError("Supply both column names for the selected pair")
    else:
        defaults = [
            pair
            for pair in (("I", "Q"), ("A", "phi"))
            if all(name in header for name in pair)
        ]
        if len(defaults) != 1:
            raise ValueError(
                "Specify column names or provide exactly one I,Q or A,phi header pair"
            )
        pair = defaults[0]
        polar = pair == ("A", "phi")
    if pair[0] == pair[1] or any(header.count(name) != 1 for name in pair):
        raise ValueError(f"Expected two distinct, uniquely named columns {pair!r}")
    return header.index(pair[0]), header.index(pair[1]), polar


def load_iq_csv(
    path, *, i=None, q=None, amplitude=None, phase=None, phase_unit="rad"
) -> np.ndarray:
    """Load a headered UTF-8 CSV as a one-dimensional complex128 envelope.

    An I,Q pair gives I + 1j*Q. An amplitude,phase pair gives A*exp(1j*phi)
    for a cosine carrier. Polar phases default to radians; use phase_unit="deg"
    for degrees. Specify column names for a custom header or to select a pair
    when both I,Q and A,phi are present. Each CSV row supplies one sample,
    in file order. Values must already have magnitude at most one.
    """
    path = Path(path)
    if phase_unit not in ("rad", "deg"):
        raise ValueError("phase_unit must be 'rad' or 'deg'")
    # Interleaved float64s can be viewed directly as one complex128 array.
    components = array("d")
    lines = array("Q")
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.reader(stream, strict=True)
        try:
            header = [name.strip() for name in next(reader, ())]
            if not header:
                raise ValueError(f"{path}: missing CSV header")
            try:
                left, right, polar = _sample_columns(header, i, q, amplitude, phase)
            except ValueError as error:
                raise ValueError(f"{path}: {error}") from error
            if not polar and phase_unit != "rad":
                raise ValueError(
                    "phase_unit applies only to amplitude and phase columns"
                )
            for row in reader:
                if not row:
                    continue
                if len(row) != len(header):
                    raise ValueError(
                        f"{path}: line {reader.line_num}: "
                        f"expected {len(header)} fields, got {len(row)}"
                    )
                try:
                    first, second = float(row[left]), float(row[right])
                except ValueError as error:
                    raise ValueError(
                        f"{path}: line {reader.line_num}: expected numbers in "
                        f"{header[left]!r} and {header[right]!r}"
                    ) from error
                if not math.isfinite(first) or not math.isfinite(second):
                    raise ValueError(
                        f"{path}: line {reader.line_num}: sample values must be finite"
                    )
                if polar:
                    angle = (
                        math.radians(math.remainder(second, 360))
                        if phase_unit == "deg"
                        else second
                    )
                    sample = cmath.rect(first, angle)
                    first, second = sample.real, sample.imag
                components.extend((first, second))
                lines.append(reader.line_num)
        except csv.Error as error:
            raise ValueError(f"{path}: line {reader.line_num}: {error}") from error
        except UnicodeDecodeError as error:
            raise ValueError(f"{path}: expected UTF-8 CSV text") from error
    if not lines:
        raise ValueError(f"{path}: CSV has no sample rows")
    samples = np.frombuffer(components, dtype=np.complex128)
    with np.errstate(over="ignore"):
        try:
            validate_sample_values(samples)
        except ValueError as error:
            line = lines[int(np.argmax(np.abs(samples)))]
            raise ValueError(
                f"{path}: line {line}: sample magnitude exceeds one"
            ) from error
    return samples
