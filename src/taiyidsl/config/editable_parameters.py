"""Validated live views used by editable Qubits, not by experiment snapshots.

Every write first edits a detached plain dictionary. The owning Qubit validates
the complete candidate and only then publishes its parameters and revision.
Retained nested views resolve against the owner, so they never edit stale values.
"""

from collections.abc import Mapping, MutableMapping, MutableSequence


def _read(owner, path):
    value = owner.params
    for key in path:
        value = (
            value[key] if isinstance(value, (Mapping, tuple)) else getattr(value, key)
        )
    return value


def _parent(data, path):
    for key in path:
        data = data[key]
    return data


def _view(owner, path, value):
    from .role_parameters import RoleDriveParameters

    if isinstance(value, (Mapping, RoleDriveParameters)):
        return ParameterNamespace(owner, path)
    if isinstance(value, tuple):
        return ParameterList(owner, path)
    return value


class ParameterNamespace(MutableMapping):
    """Attribute or dictionary access to an editable Qubit's current parameters."""

    __slots__ = ("_owner", "_path")

    def __init__(self, owner, path=()):
        object.__setattr__(self, "_owner", owner)
        object.__setattr__(self, "_path", path)

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        try:
            return self[name]
        except KeyError as error:
            raise AttributeError(name) from error

    def __setattr__(self, name, value):
        if name.startswith("_"):
            raise AttributeError(name)
        self[name] = value

    def __getitem__(self, key):
        value = _read(self._owner, self._path)
        item = value[key] if isinstance(value, Mapping) else getattr(value, key)
        return _view(self._owner, (*self._path, key), item)

    def __setitem__(self, key, value):
        def change(data):
            _parent(data, self._path)[key] = value

        self._owner._edit_parameters(change)

    def __delitem__(self, key):
        def change(data):
            del _parent(data, self._path)[key]

        self._owner._edit_parameters(change)

    def __iter__(self):
        return iter(self.to_dict())

    def __len__(self):
        return len(self.to_dict())

    def to_dict(self):
        from .models import _plain

        value = _read(self._owner, self._path)
        return value.to_dict() if hasattr(value, "to_dict") else _plain(value)

    def __repr__(self):
        return repr(self.to_dict())

    def update(self, *args, **kwargs):
        values = dict(*args, **kwargs)

        def change(data):
            _parent(data, self._path).update(values)

        self._owner._edit_parameters(change)


class ParameterList(MutableSequence):
    """List edits inside extensions also pass through full parameter validation."""

    __slots__ = ("_owner", "_path")

    def __init__(self, owner, path):
        self._owner, self._path = owner, path

    def __len__(self):
        return len(_read(self._owner, self._path))

    def __getitem__(self, index):
        if isinstance(index, slice):
            return [self[i] for i in range(*index.indices(len(self)))]
        value = _read(self._owner, self._path)[index]
        return _view(self._owner, (*self._path, index), value)

    def __setitem__(self, index, value):
        def change(data):
            _parent(data, self._path)[index] = value

        self._owner._edit_parameters(change)

    def __delitem__(self, index):
        def change(data):
            del _parent(data, self._path)[index]

        self._owner._edit_parameters(change)

    def insert(self, index, value):
        def change(data):
            _parent(data, self._path).insert(index, value)

        self._owner._edit_parameters(change)

    def __eq__(self, other):
        return list(self) == other

    def __repr__(self):
        return repr(list(self))
