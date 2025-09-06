import json
from django.core.files.storage import default_storage


class RemoteJSONProxy:
    """Proxy around a JSON-serialisable value persisted remotely.

    Creation modes:
      - RemoteJSONProxy(value=<obj>)  -> new unsaved value (marked dirty, needs save)
      - RemoteJSONProxy(file_path=<path>) -> reference to existing stored file (lazy load)
      - RemoteJSONProxy(value=<obj>, file_path=<path>) -> existing value already persisted
    """

    def __init__(self, value=None, file_path=None):
        self._file_path = file_path
        self._loaded = value is not None  # value provided implies already loaded
        self._value = value
        self._dirty = value is not None and file_path is None
        self._mutator_cache = {}

    # ----- Core accessors -----
    def _lazy_load(self):
        if not self._loaded and self._file_path:
            with default_storage.open(self._file_path) as fh:
                self._value = json.load(fh)
            self._loaded = True

    def get(self):
        self._lazy_load()
        return self._value

    def set(self, value):
        self._value = value
        self._loaded = True
        self._dirty = True

    # ----- Item access (dict / list) -----
    def __getitem__(self, key):
        self._lazy_load()
        if self._value is None:
            raise KeyError(key)
        return self._value[key]

    def __setitem__(self, key, value):
        self._lazy_load()
        if self._value is None:
            self._value = {}
        self._value[key] = value
        self._dirty = True

    # ----- Representation & basic comparisons -----
    def __repr__(self):
        self._lazy_load(); return repr(self._value)

    def __str__(self):
        self._lazy_load(); return str(self._value)

    def __eq__(self, other):
        self._lazy_load(); return self._value == other

    def __ne__(self, other):  # explicit though Python falls back to eq
        self._lazy_load(); return self._value != other

    def __bool__(self):
        self._lazy_load(); return bool(self._value)

    # ----- Addition / concatenation (primary ergonomic need) -----
    def __add__(self, other):  # type: ignore[override]
        self._lazy_load(); return self._value + other  # type: ignore[operator]

    def __radd__(self, other):  # type: ignore[override]
        self._lazy_load(); return other + self._value  # type: ignore[operator]

    @property
    def __class__(self):  # type: ignore
        self._lazy_load(); return self._value.__class__

    # ----- Attribute / mutator delegation -----
    def __getattr__(self, item):
        self._lazy_load()
        attr = getattr(self._value, item)
        MUTATING = {
            'append', 'extend', 'insert', 'pop', 'popitem', 'remove', 'clear', 'update',
            'setdefault', 'reverse', 'sort', 'discard', 'add'
        }
        if callable(attr) and item in MUTATING:
            if item in self._mutator_cache:
                return self._mutator_cache[item]
            def wrapper(*args, **kwargs):
                result = attr(*args, **kwargs)
                self._dirty = True
                return result
            self._mutator_cache[item] = wrapper
            return wrapper
        return attr

    def __dir__(self):
        self._lazy_load(); return dir(self._value)

    # ----- Ordering & unary ops -----
    def __lt__(self, other): self._lazy_load(); return self._value < other  # type: ignore[operator]
    def __le__(self, other): self._lazy_load(); return self._value <= other  # type: ignore[operator]
    def __gt__(self, other): self._lazy_load(); return self._value > other  # type: ignore[operator]
    def __ge__(self, other): self._lazy_load(); return self._value >= other  # type: ignore[operator]
    def __neg__(self): self._lazy_load(); return -self._value  # type: ignore[operator]
    def __pos__(self): self._lazy_load(); return +self._value  # type: ignore[operator]
    def __abs__(self): self._lazy_load(); return abs(self._value)  # type: ignore[arg-type]
    def __invert__(self): self._lazy_load(); return ~self._value  # type: ignore[operator]

    # ----- Container protocol -----
    def __len__(self): self._lazy_load(); return len(self._value)  # type: ignore[arg-type]
    def __iter__(self): self._lazy_load(); return iter(self._value)  # type: ignore[arg-type]
    def __contains__(self, item): self._lazy_load(); return item in self._value  # type: ignore[operator]

    # ----- Dirty tracking -----
    @property
    def needs_save(self): return self._dirty
    def mark_saved(self): self._dirty = False

    # Generic operator helpers for extended ops
    def _binary_op(self, other, name):
        self._lazy_load(); method = getattr(self._value, f'__{name}__', None); return NotImplemented if method is None else method(other)
    def _reflected_op(self, other, name):
        self._lazy_load(); method = getattr(self._value, f'__r{name}__', None)
        if method is not None: return method(other)
        if name == 'add':
            try: return other + self._value  # type: ignore[operator]
            except TypeError: return NotImplemented
        return NotImplemented
    def _inplace_op(self, other, name):
        self._lazy_load(); imethod = getattr(self._value, f'__i{name}__', None)
        if imethod is not None:
            result = imethod(other); self._dirty = True
            if result is not self._value: self._value = result
            return self
        bmethod = getattr(self._value, f'__{name}__', None)
        if bmethod is None: return NotImplemented
        self._value = bmethod(other); self._dirty = True; return self


_BINARY_OPS = [
    'add', 'sub', 'mul', 'matmul', 'truediv', 'floordiv', 'mod', 'pow',
    'lshift', 'rshift', 'and', 'xor', 'or'
]
for _name in _BINARY_OPS:
    def _make_bin(n): return lambda self, other, _n=n: RemoteJSONProxy._binary_op(self, other, _n)
    def _make_ref(n): return lambda self, other, _n=n: RemoteJSONProxy._reflected_op(self, other, _n)
    def _make_in(n): return lambda self, other, _n=n: RemoteJSONProxy._inplace_op(self, other, _n)
    setattr(RemoteJSONProxy, f'__{_name}__', _make_bin(_name))
    setattr(RemoteJSONProxy, f'__r{_name}__', _make_ref(_name))
    setattr(RemoteJSONProxy, f'__i{_name}__', _make_in(_name))

del _name, _make_bin, _make_ref, _make_in, _BINARY_OPS
