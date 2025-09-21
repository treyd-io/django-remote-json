# django-remote-json

`RemoteJSONField` is a Django model field that persists JSON in your configured Django storage backend (e.g. S3, GCS, local filesystem) while storing only a generated file path in the database (TEXT column). Model attribute access returns a rich `RemoteJSONProxy` that:

* Lazily loads the underlying JSON only when first accessed.
* Behaves like the wrapped value (dict / list / primitive) for most common Python operations & mutators.
* Tracks dirty state so you only rewrite the remote file when data really changed.

> Ideal for large or frequently-mutated JSON blobs you don't want to keep inside Postgres/ MySQL row storage (or when you want cheaper object storage + lower DB bloat).

---

## Features
* Offload large JSON blobs to object storage; DB stores only a path (simple `TextField`).
* Lazy on-demand load via `RemoteJSONProxy` (no I/O until you actually touch the value).
* Dirty tracking: only write again if you mutated the proxy (item assignment, list/dict mutators, in-place numeric ops, `set()` helper, etc.).
* Works with: `dict`, `list`, `str`, `int`, `float`, `bool` (and nested mixes thereof) as long as they are JSON-serialisable.
* Arithmetic / container protocol support (addition, `len()`, iteration, membership, comparisons, unary ops, many in-place ops) flows through to the underlying value.
* Reassigning a brand new raw Python value always writes a fresh file (even if identical) – useful for explicit overwrite semantics.
* Assigning `None` deletes the previously stored remote file and stores `NULL` in the DB column.
* Simple pluggable path strategy via an `upload_to(instance, filename)` callable (same spirit as Django's `FileField`).
* Heuristic detection of existing stored values when loading from DB (regex checks if the string looks like a generated JSON file path) producing a proxy without incurring an early load.
* MIT licensed, typed (`py.typed` included) and tested.

---

## Supported Versions
* Python: 3.10 – 3.12
* Django: 4.2+ (Should work with later versions; open an issue if you hit incompatibilities.)

---

## Installation
```bash
pip install django-remote-json
```

## Quick Start
```python
from django.db import models
from django_remote_json import RemoteJSONField

class Report(models.Model):
    payload = RemoteJSONField(null=True)

# Create with an initial JSON object
r = Report.objects.create(payload={"a": 1, "b": [1, 2, 3]})

print(r.payload["a"])     # 1 (lazy load occurs here on first access)
r.payload["c"] = 42        # mutation marks proxy dirty
r.save()                   # remote file re-written with new JSON

# Later / new process
fresh = Report.objects.get(pk=r.pk)
fresh.payload  # still a proxy; file not read yet
print(fresh.payload["b"]) # triggers load from storage
```

---

## How It Works (Lifecycle)
1. Attribute assignment stores a `RemoteJSONProxy` on the instance immediately (wrapping raw value or referencing a path).
2. On `model.save()`, if the proxy is dirty (or the value is a raw JSON-serialisable object), JSON is serialised and written to `default_storage` at a generated path: `ISO_TIMESTAMP-uuid5(namespace, instance.pk).json` (you can override directory/prefix via `upload_to`).
3. The database column stores only that path (string) – never the raw JSON payload.
4. On later loads from the ORM, the stored path is converted to a lazy `RemoteJSONProxy` (still no JSON read).
5. First meaningful interaction (`__getitem__`, attribute/method usage, iteration, etc.) triggers a storage read and caches the Python object.
6. Mutations mark the proxy dirty; next `save()` rewrites the file (overwriting previous path rather than orphaning).
7. Setting the field to `None` deletes the prior file (if any) and writes `NULL` in the DB column.

---

## Field Declaration & Custom Path Strategy
```python
def upload_to(instance, filename: str) -> str:
    return f"reports/{instance.pk}/{filename}"  # you can nest by date, etc.

class Report(models.Model):
    payload = RemoteJSONField(upload_to=upload_to, null=True, blank=True)
```
If you omit `upload_to`, the file name is just the generated `<timestamp>-<uuid>.json` at the storage root.

---

## Using a Non-Default Storage
The field always uses `django.core.files.storage.default_storage`. Configure that in settings (e.g. with `django-storages`). Example (S3):
```python
INSTALLED_APPS += ["storages"]
AWS_STORAGE_BUCKET_NAME = "my-bucket"
DEFAULT_FILE_STORAGE = "storages.backends.s3boto3.S3Boto3Storage"
```
Nothing else required – the field will transparently read/write via that backend.

---

## Working With The Proxy
```python
r = Report.objects.create(payload={"numbers": [1, 2]})
r.payload["numbers"].append(3)  # nested mutation limitation (see below) – this mutates inner list only
r.payload["numbers"] = r.payload["numbers"] + [4]  # marks top-level dict dirty via __setitem__
r.save()

# Numeric primitives
r.payload = 10
r.save()
assert (r.payload + 5) == 15

# Replace entire value conveniently
r.payload.set({"replaced": True})  # marks dirty
r.save()
```

### Dirty Tracking Semantics
The proxy marks itself dirty when any of the following happen:
* Item assignment: `payload["k"] = v`
* Mutator methods: `append`, `extend`, `insert`, `pop`, `popitem`, `remove`, `clear`, `update`, `setdefault`, `reverse`, `sort`, `discard`, `add`
* In-place numeric / bitwise ops: `+=`, `*=`, etc.
* Calling `proxy.set(new_value)`
* Direct `__setitem__` for lists/dicts

Reassigning the model field (e.g. `instance.payload = {...}`) replaces the proxy and will write on next save even if identical contents (explicit intent to rewrite).

### Known Limitation: Nested Mutations
Mutating a nested object retrieved from the proxy does NOT mark the top-level proxy dirty:
```python
data = Report.objects.get(pk=1).payload
inner = data["outer"]
inner["leaf"] = 2  # Does NOT mark data dirty (no save)
```
Workarounds:
* Reassign the modified subtree: `tmp = data["outer"]; tmp["leaf"] = 2; data["outer"] = tmp`
* Or call `data.set({...})` with a fully updated object.
Future enhancement could include deep instrumentation; contributions welcome.

---

## Validation & Errors
Serialization uses Python's `json.dumps`. Anything not JSON-serialisable (e.g. sets, custom objects) will raise a `TypeError` during `save()`. Catch or pre-normalise as needed.

---

## Database / Migration Footprint
The underlying column is just a `TextField` (plus whatever null/blank/options you add). Example migration snippet auto-generated:
```python
('payload', models.TextField(null=True))
```
There is no separate model or join table; remote file lifecycle is entirely managed in `pre_save`.

---

## Regex Heuristic For Existing Paths
If the DB value looks like:
```
[optional prefix dirs]/YYYY-MM-DDTHH:MM:SS(.microseconds)?-<uuid-like-suffix>.json
```
it is treated as a file path and wrapped in a lazy proxy. Otherwise, if it is a plain string not matching the pattern, it is treated as raw JSON string content and proxied directly.

---

## Testing Tips
Patch `default_storage` with an in-memory fake (pattern used in this project's own test suite):
```python
import io
from unittest import mock

class MemoryStorage:
    def __init__(self):
        self.files = {}
    def save(self, path, content_file):
        data = content_file.read(); self.files[path] = data.decode() if isinstance(data, bytes) else data; return path
    def delete(self, path): self.files.pop(path, None)
    def open(self, path, mode='r'): return io.StringIO(self.files[path])

def test_something(monkeypatch):
    storage = MemoryStorage()
    monkeypatch.setattr('django_remote_json.remote_json_field.default_storage', storage)
    monkeypatch.setattr('django_remote_json.remote_json_proxy.default_storage', storage)
    # proceed with creating models & assertions
```

---

## Type Hints
The distribution ships a `py.typed` marker. The proxy uses a dynamic `__class__` property so `isinstance(proxy, dict)` works at runtime, but static type checkers will not infer the inner structure. You can write small helpers or cast explicitly if needed.

---

## Performance Considerations
* Reads only happen on first actual use of the value in a given process lifecycle.
* Repeated saves without mutation do NOT trigger re-serialization (optimization in tests: duplicate saves keep `save_count` constant).
* Large JSON writes overwrite (delete then save) the existing file to avoid orphaning.

---

## Concurrency Notes
Writes are not atomic across multiple concurrent processes modifying the same field; last save wins. If you need merge semantics, load, modify, and compare externally or add optimistic locking (e.g., a version field) to your model.

---

## FAQ
**Q: Can I use a custom storage just for this field?**  Yes – temporarily swap `default_storage` via context manager or subclass & override references.

**Q: How do I force a rewrite even if unchanged?**  Reassign the raw value: `instance.payload = instance.payload.get(); instance.save()`.

**Q: How do I completely remove the remote file?**  `instance.payload = None; instance.save()`.

---

## Roadmap / Ideas
* Optional deep mutation tracking.
* Optional compression / encryption hooks.
* Async storage support (when Django supports it more broadly for storages).
* Admin widget shortcuts for preview / download.

PRs welcome – open an issue to discuss substantial changes first.

---

## Development
```bash
git clone https://github.com/treyd-io/django-remote-json.git
cd django-remote-json
python -m venv .venv && source .venv/bin/activate
pip install -e .[dev]  # if you later add optional extras
pytest
```

---

## License
This project is released under the MIT License – see the `LICENSE` file for full text.

---

## Security / Disclosure
No known security-sensitive components; nevertheless, if you discover a vulnerability, please contact the maintainers privately before disclosure.

---

Happy building! If this saves you DB space or simplifies handling large JSON blobs, a star on GitHub is appreciated.
