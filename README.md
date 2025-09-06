# django-remote-json

A Django model field (`RemoteJSONField`) that stores JSON data in remote storage (e.g. S3) while persisting only the generated file path in the database. Runtime access returns a `RemoteJSONProxy` behaving like the underlying JSON value (dict/list/primitive) with lazy loading and dirty tracking.

## Features
- Store large JSON blobs outside your DB; only keep a path in a text column.
- Lazy load on first access when reloading from DB.
- Seamless usage: item access, iteration, truthiness, many operators, string/numeric concatenation.
- Dirty tracking: mutations (assignment, common list/dict mutators, in-place ops) trigger a file rewrite on the next model `.save()`.
- Works with primitives (str/int/float/bool), dicts, lists.
- Assign `None` to delete the remote file and store NULL.

## Installation
```bash
pip install django-remote-json
```

## Quick Start
```python
from django.db import models
from django_remote_json import RemoteJSONField

class Report(models.Model):
    payload = RemoteJSONField()

# Create
r = Report.objects.create(payload={"a": 1, "b": [1,2,3]})
print(r.payload["a"])  # 1
r.payload["c"] = 42     # marks dirty
r.save()                # persists updated JSON remotely
```

## How It Works
1. On assignment and model save, the JSON is serialized and written to the configured Django storage backend (`default_storage`).
2. The DB column stores only the file path.
3. On model reload, the path is converted into a `RemoteJSONProxy` that loads JSON lazily.
4. Mutations set a dirty flag so subsequent saves reserialize and overwrite the file.

## Configuration
Provide a custom `upload_to` function to control the path:
```python
def upload_to(instance, filename):
    return f"reports/{instance.pk}/{filename}"

class Report(models.Model):
    payload = RemoteJSONField(upload_to=upload_to)
```

## Limitations / Roadmap
- Nested mutation detection (e.g. `payload['a']['b'] = 2`) will not mark dirty unless the top-level container is mutated directly.
- No special handling for distinguishing JSON `null` vs deletion semantics.
- `isinstance(proxy, dict)` works via `__class__` override but some static type checkers may still flag it.

## Testing
You can unit test by patching `django_remote_json.remote_json_field.default_storage` with an in-memory fake implementing `save`, `delete`, and `open`.

## License
MIT
