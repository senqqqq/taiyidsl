"""Read waveform tables without choosing pulse channels, units or a time grid."""

import csv
from itertools import chain
from pathlib import Path

import numpy as np


def _complex_value(field):
    # Accept engineering i notation as well as Python's j, including (a+bi).
    value = field.strip()
    if value.startswith("(") and value.endswith(")"):
        value = value[1:-1].strip()
    if value.endswith(("i", "I")):
        value = value[:-1] + "j"
    return complex(value)


def _infer_column(fields):
    if not any(field.strip() for field in fields):
        return np.asarray(fields, dtype=str)
    try:
        integers = [int(field) for field in fields]
    except ValueError:
        pass
    else:
        limits = np.iinfo(np.int64)
        dtype = (
            np.int64
            if limits.min <= min(integers) <= max(integers) <= limits.max
            else object
        )
        return np.asarray(integers, dtype=dtype)

    # Missing values only become NaN if the whole column is numeric.
    for convert, dtype in ((float, np.float64), (_complex_value, np.complex128)):
        try:
            return np.asarray(
                [convert(field) if field.strip() else np.nan for field in fields],
                dtype=dtype,
            )
        except ValueError:
            continue
    return np.asarray(fields, dtype=str)


def load_waveform_csv(
    path, *, delimiter=",", header=True, encoding="utf-8-sig", text_columns=()
) -> np.ndarray:
    """Load all rows and columns into a one-dimensional NumPy structured array.

    Use table.dtype.names, table["column"], slices and boolean masks to explore
    the data. Infer types per column. Use text_columns for identifiers that
    must keep their spelling. Headerless files get names col0, col1, etc.
    """
    path = Path(path)
    with path.open(encoding=encoding, newline="") as stream:
        reader = csv.reader(stream, delimiter=delimiter, strict=True)
        rows = (row for row in reader if row)
        try:
            first = next(rows, None)
            if first is None:
                raise ValueError(f"{path}: empty CSV")
            if header:
                names = tuple(first)
                if any(not name.strip() for name in names) or len(set(names)) != len(
                    names
                ):
                    raise ValueError(f"{path}: expected nonempty, unique column names")
            else:
                names = tuple(f"col{index}" for index in range(len(first)))
                rows = chain((first,), rows)
            keep_text = (
                {text_columns} if isinstance(text_columns, str) else set(text_columns)
            )
            unknown = keep_text.difference(names)
            if unknown:
                raise ValueError(f"{path}: unknown text columns {sorted(unknown)!r}")
            columns = [[] for _ in names]
            for row in rows:
                if len(row) != len(names):
                    raise ValueError(
                        f"{path}: line {reader.line_num}: expected {len(names)} "
                        f"fields, got {len(row)}"
                    )
                for column, field in zip(columns, row, strict=True):
                    column.append(field)
        except csv.Error as error:
            raise ValueError(f"{path}: line {reader.line_num}: {error}") from error
    arrays = [
        np.asarray(fields, dtype=str) if name in keep_text else _infer_column(fields)
        for name, fields in zip(names, columns, strict=True)
    ]
    result = np.empty(
        len(arrays[0]),
        dtype=[(name, col.dtype) for name, col in zip(names, arrays, strict=True)],
    )
    for name, col in zip(names, arrays, strict=True):
        result[name] = col
    return result
