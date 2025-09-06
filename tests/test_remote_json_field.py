import io
import json
import re
from typing import Any
from unittest import mock

import pytest

pytestmark = pytest.mark.django_db

from django_remote_json import RemoteJSONField, RemoteJSONProxy
from .models import SampleModel

class MemoryStorage:
    def __init__(self):
        self.files: dict[str, str] = {}
        self.save_count = 0
        self.delete_count = 0

    def save(self, path, content_file):
        data = content_file.read()
        if isinstance(data, bytes):
            data = data.decode("utf-8")
        self.files[path] = data
        self.save_count += 1
        return path

    def delete(self, path):
        self.files.pop(path, None)
        self.delete_count += 1

    def open(self, path, mode="r"):
        return io.StringIO(self.files[path])


@pytest.fixture()
def storage_patched(monkeypatch):
    storage = MemoryStorage()
    monkeypatch.setattr("django_remote_json.remote_json_field.default_storage", storage)
    monkeypatch.setattr("django_remote_json.remote_json_proxy.default_storage", storage)
    return storage


def create():
    return SampleModel.objects.create()


def assert_file_path(value: Any):
    assert isinstance(value, RemoteJSONProxy)
    assert value._file_path is not None
    assert value._file_path.endswith('.json')


def last_json(storage: MemoryStorage):
    if not storage.files:
        return None, None
    path = list(storage.files.keys())[-1]
    return path, json.loads(storage.files[path])


def test_save_and_reload_dict(storage_patched):
    storage = storage_patched
    obj = create()
    obj.remote_data = {"foo": "bar"}
    obj.save()
    assert_file_path(obj.remote_data)
    path, data = last_json(storage)
    assert data == {"foo": "bar"}
    proxy = obj.remote_data
    assert path == proxy._file_path
    obj.refresh_from_db()
    proxy2 = obj.remote_data
    assert proxy2.get() == {"foo": "bar"}


def test_dirty_mutation_only_writes_when_dirty(storage_patched):
    storage = storage_patched
    obj = create()
    obj.remote_data = {"a": 1}
    obj.save()
    initial = storage.save_count
    obj.refresh_from_db()
    proxy = obj.remote_data
    obj.save()
    assert storage.save_count == initial
    proxy["b"] = 2
    assert proxy.needs_save
    obj.save()
    assert storage.save_count == initial + 1
    obj.refresh_from_db()
    proxy = obj.remote_data
    assert proxy.get() == {"a": 1, "b": 2}


def test_assign_none_deletes_file(storage_patched):
    storage = storage_patched
    obj = create()
    obj.remote_data = {"x": 1}
    obj.save()
    deletes_before = storage.delete_count
    obj.remote_data = None
    obj.save()
    assert obj.remote_data is None
    assert storage.delete_count > deletes_before


def test_various_value_types(storage_patched):
    storage = storage_patched
    values = [123, 1.23, True, False, "s", [1, 2], {"a": 1}]
    for v in values:
        obj = create()
        obj.remote_data = v
        obj.save()
        assert_file_path(obj.remote_data)
        proxy = obj.remote_data
        assert proxy.get() == v
        obj.refresh_from_db()
        proxy2 = obj.remote_data
        assert proxy2.get() == v


def test_type_transitions(storage_patched):
    storage = storage_patched
    obj = create()
    seq = [{"a": 1}, [1, 2, 3], "txt", 42, None, {"b": 2}]
    for v in seq:
        obj.remote_data = v
        obj.save()
        if v is None:
            assert obj.remote_data is None
        else:
            assert_file_path(obj.remote_data)
            proxy = obj.remote_data
            assert proxy.get() == v
            obj.refresh_from_db()
            proxy2 = obj.remote_data
            assert proxy2.get() == v


def test_duplicate_save_optimization(storage_patched):
    storage = storage_patched
    obj = create()
    obj.remote_data = {"dup": True}
    obj.save()
    first = storage.save_count
    obj.refresh_from_db(); obj.save()
    assert storage.save_count == first
    obj.refresh_from_db(); proxy = obj.remote_data
    proxy["dup"] = False
    obj.save()
    assert storage.save_count == first + 1


def test_reassign_raw_always_writes(storage_patched):
    storage = storage_patched
    obj = create()
    for _ in range(3):
        obj.remote_data = {"foo": "bar"}
        obj.save()
    assert storage.save_count == 3


def test_assign_proxy_directly(storage_patched):
    storage = storage_patched
    proxy = RemoteJSONProxy({"x": 1})
    obj = create()
    obj.remote_data = proxy
    obj.save()
    assert_file_path(obj.remote_data)
    proxy2 = obj.remote_data
    assert proxy2.get() == {"x": 1}
    obj.refresh_from_db(); proxy3 = obj.remote_data
    assert proxy3.get() == {"x": 1}


def test_proxy_add_and_numeric(storage_patched):
    storage = storage_patched
    obj = create()
    obj.remote_data = 10
    obj.save(); proxy = obj.remote_data
    assert proxy + 5 == 15
    assert 5 + proxy == 15


def test_unary_and_comparisons(storage_patched):
    obj = create(); obj.remote_data = 10; obj.save(); proxy = obj.remote_data
    assert -proxy == -10
    assert +proxy == +10
    assert abs(proxy) == 10
    assert proxy < 20 and proxy <= 10 and proxy >= 10 and proxy > 5


def test_inplace_numeric_ops_dirty(storage_patched):
    storage = storage_patched
    obj = create(); obj.remote_data = 5; obj.save(); proxy = obj.remote_data
    initial = storage.save_count
    proxy += 1  # type: ignore[operator]
    assert proxy.needs_save
    obj.save(); assert storage.save_count == initial + 1
    obj.refresh_from_db(); proxy = obj.remote_data; assert proxy.get() == 6
    proxy *= 3  # type: ignore[operator]
    assert proxy.needs_save
    obj.save(); obj.refresh_from_db(); proxy = obj.remote_data
    assert proxy.get() == 18


def test_list_mutators_dirty(storage_patched):
    storage = storage_patched
    obj = create(); obj.remote_data = [1, 2]; obj.save(); proxy = obj.remote_data
    initial = storage.save_count
    proxy.append(3)
    assert proxy.needs_save
    obj.save(); assert storage.save_count == initial + 1
    obj.refresh_from_db(); proxy = obj.remote_data; assert proxy.get() == [1, 2, 3]
    proxy.extend([4]); proxy.insert(0, 0)
    assert proxy.needs_save
    obj.save(); obj.refresh_from_db(); proxy = obj.remote_data
    assert proxy.get() == [0, 1, 2, 3, 4]


def test_dict_mutators_dirty(storage_patched):
    storage = storage_patched
    obj = create(); obj.remote_data = {"a": 1}; obj.save(); proxy = obj.remote_data
    initial = storage.save_count
    proxy.update({"b": 2}); assert proxy.needs_save
    obj.save(); obj.refresh_from_db(); proxy = obj.remote_data; assert proxy.get() == {"a": 1, "b": 2}
    proxy.setdefault("a", 99); proxy.setdefault("c", 3); proxy.pop("b")
    assert proxy.needs_save
    obj.save(); obj.refresh_from_db(); proxy = obj.remote_data; assert proxy.get() == {"a": 1, "c": 3}


def test_nested_mutation_limitation(storage_patched):
    obj = create(); obj.remote_data = {"outer": {"inner": 1}}; obj.save(); obj.refresh_from_db(); proxy = obj.remote_data
    inner = proxy["outer"]; inner["inner"] = 2  # type: ignore[index]
    assert not proxy.needs_save  # document current limitation


def test_assign_none_cleans_dirty_state(storage_patched):
    storage = storage_patched
    obj = create(); obj.remote_data = {"a": 1}; obj.save(); obj.refresh_from_db(); proxy = obj.remote_data
    proxy["b"] = 2; assert proxy.needs_save
    obj.remote_data = None; obj.save()
    assert obj.remote_data is None
    # ensure file deleted
    assert storage.delete_count >= 1


def test_unsupported_type_raises(storage_patched):
    obj = create()
    with pytest.raises(TypeError):  # json.dumps on object raises TypeError
        obj.remote_data = object()
        obj.save()


# ---- Missing tests from original suite added for parity ----
def test_to_python_existing_path(storage_patched):
    storage = storage_patched
    field = RemoteJSONField()
    # Simulate persisted file
    path = "2025-01-01T00:00:00.000000-deadbeef-dead-beef-dead-beefdeadbeef.json"
    storage.files[path] = json.dumps({"loaded": True})
    proxy = field.to_python(path)
    assert isinstance(proxy, RemoteJSONProxy)
    assert proxy.get() == {"loaded": True}


def test_is_file_path():
    field = RemoteJSONField()
    good = "dir/2025-09-05T12:34:56.123456-deadbeef-dead-beef-dead-beefdeadbeef.json"
    bad = "bad.json"
    assert field.is_file_path(good)
    assert not field.is_file_path(bad)


def test_dirty_cycle_mark_saved(storage_patched):
    obj = create(); obj.remote_data = {"a": 1}; obj.save(); obj.refresh_from_db(); proxy = obj.remote_data
    proxy["b"] = 2; assert proxy.needs_save
    obj.save(); obj.refresh_from_db(); proxy = obj.remote_data; assert not proxy.needs_save
    proxy["c"] = 3; assert proxy.needs_save
    proxy.mark_saved(); assert not proxy.needs_save


def test_proxy_dunder_and_equality():
    p = RemoteJSONProxy({"a": 1, "b": 2})
    assert bool(p)
    assert p == {"a": 1, "b": 2}
    assert "keys" in dir(p)
    assert p.__class__ is dict


def test_proxy_add_concatenation_string(storage_patched):
    obj = create(); obj.remote_data = "hello"; obj.save(); proxy = obj.remote_data
    assert proxy + " world" == "hello world"
    # Left-add with literal not asserted (str.__add__ won't accept proxy); use interpolation.
    assert f"say: {proxy}" == "say: hello"


def test_list_container_protocol_and_wrapper_caching(storage_patched):
    obj = create(); obj.remote_data = [1, 2]; obj.save(); proxy = obj.remote_data
    assert len(proxy) == 2
    assert 1 in proxy
    assert list(iter(proxy)) == [1, 2]
    first = proxy.append
    second = proxy.append
    assert first is second  # cached wrapper


# ---------------- Additional coverage tests -----------------

def test_generate_file_path_contains_uuid(storage_patched):
    obj = create()
    field = SampleModel._meta.get_field('remote_data')  # type: ignore[assignment]
    path = field.generate_file_path(obj)  # type: ignore[attr-defined]
    # Expect ISO timestamp + '-' + uuid5(namespace, pk) + .json
    import uuid as _uuid
    expected_uuid = _uuid.uuid5(field.namespace, str(obj.pk))  # type: ignore[attr-defined]
    assert path.endswith('.json')
    assert str(expected_uuid) in path


def test_raw_value_unsaved_and_saved(storage_patched):
    obj = SampleModel()
    field: RemoteJSONField = SampleModel._meta.get_field('remote_data')  # type: ignore[assignment]
    assert field.raw_value(obj) is None
    obj.save()
    obj.remote_data = {"a": 1}
    obj.save()
    assert field.raw_value(obj) is not None


def test_to_python_invalid_type():
    field = RemoteJSONField()
    class Foo: ...
    with pytest.raises(ValueError):
        field.to_python(Foo())  # type: ignore[arg-type]


def test_get_prep_value_variants(storage_patched):
    field = RemoteJSONField()
    # String path passes through
    assert field.get_prep_value('some/path.json') == 'some/path.json'
    # None -> None
    assert field.get_prep_value(None) is None
    # Proxy returns its file path
    # Provide an in-memory value so proxy does not attempt lazy load
    p = RemoteJSONProxy(value={}, file_path='abc.json')
    assert field.get_prep_value(p) == 'abc.json'
    # Unsupported type
    with pytest.raises(TypeError):
        field.get_prep_value(123)


def test_lazy_load_from_file_path(storage_patched):
    storage = storage_patched
    path = '2025-01-01T00:00:00-aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa.json'
    storage.files[path] = json.dumps({'lazy': True})
    proxy = RemoteJSONProxy(file_path=path)
    # Not loaded yet
    assert proxy._loaded is False
    assert proxy.get() == {'lazy': True}
    assert proxy._loaded is True


def test_proxy_set_marks_dirty_and_persists(storage_patched):
    storage = storage_patched
    obj = create(); obj.remote_data = {"a": 1}; obj.save(); storage.save_count = 0
    proxy = obj.remote_data
    proxy.set({"b": 2})
    assert proxy.needs_save
    obj.save()
    assert storage.save_count == 1
    obj.refresh_from_db(); assert obj.remote_data.get() == {"b": 2}


def test_setitem_initializes_when_none(storage_patched):
    storage = storage_patched
    path = '2025-01-02T00:00:00-deadbeef-dead-beef-dead-beefdeadbeef.json'
    storage.files[path] = 'null'
    proxy = RemoteJSONProxy(file_path=path)
    proxy['a'] = 1
    assert proxy.get() == {'a': 1}
    assert proxy.needs_save


def test_getitem_on_none_raises_keyerror(storage_patched):
    storage = storage_patched
    path = '2025-01-03T00:00:00-deadbeef-dead-beef-dead-beefdeadbeef.json'
    storage.files[path] = 'null'
    proxy = RemoteJSONProxy(file_path=path)
    with pytest.raises(KeyError):
        _ = proxy['missing']


def test_unsupported_set_type_raises(storage_patched):
    obj = create()
    # Assigning a set should raise during serialization (not JSON serializable)
    obj.remote_data = {1, 2}
    with pytest.raises(TypeError):
        obj.save()


def test_inplace_op_fallback_path(storage_patched):
    # For types without __iadd__ returning self (e.g., tuple) ensure fallback still works
    obj = create(); obj.remote_data = (1, 2); obj.save(); proxy = obj.remote_data
    proxy += (3,)  # type: ignore[operator]
    assert proxy.needs_save
    obj.save(); obj.refresh_from_db();
    # JSON will deserialize tuple as list
    assert obj.remote_data.get() == [1, 2, 3]


def test_proxy_ne_and_repr_str():
    p = RemoteJSONProxy([1, 2])
    assert p != [1]
    assert repr(p) == repr([1, 2])
    assert str(p) == str([1, 2])


def test_is_file_path_bare_filename():
    field = RemoteJSONField()
    assert field.is_file_path('2025-09-05T12:34:56-deadbeef.json')
