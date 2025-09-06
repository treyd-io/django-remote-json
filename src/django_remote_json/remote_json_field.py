import json
import logging
import re
import uuid
from datetime import datetime
from typing import Any

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import models
from django.db.models import CharField, ExpressionWrapper, F

from .remote_json_proxy import RemoteJSONProxy


class RemoteJSONField(models.TextField):
    """
    A Django model field that stores JSON remotely (e.g. S3) and only a file path in the DB.

    Behaviour:
      * Assign any JSON-serialisable value (dict, list, primitives) or a RemoteJSONProxy.
      * On save: serialize JSON to storage, store path in DB column (TextField), keep a proxy on the instance.
      * Mutations through the proxy (item assignment, common list/dict mutators, in-place ops) mark it dirty; a subsequent save writes changes.
      * Assigning None deletes the remote file and stores NULL in the DB.
    """

    namespace = uuid.UUID("123e4567-e89b-12d3-a456-426614174000")

    def __init__(self, *args, upload_to=None, **kwargs):
        self.upload_to = upload_to or (lambda instance, filename: filename)
        super().__init__(*args, **kwargs)

    # ----------------- Helpers -----------------
    def raw_value(self, model_instance):
        if not model_instance.pk:
            return None
        field_name = self.attname + "_raw"
        annotation = {field_name: ExpressionWrapper(F(self.attname), output_field=CharField())}
        return (
            model_instance.__class__.objects.annotate(**annotation)
            .filter(pk=model_instance.pk)
            .values_list(field_name, flat=True)
            .first()
        )

    def generate_file_path(self, model_instance):
        date = datetime.now().isoformat()
        filename = f"{date}-{uuid.uuid5(self.namespace, str(model_instance.pk))}.json"
        return self.upload_to(model_instance, filename)

    def is_file_path(self, value: str):
        """Heuristic to determine if a string is one of this field's stored file paths.

        Original pattern required a directory component, but the default generator may
        produce a bare filename. Accept either an optional directory prefix or none.
        Pattern parts:
          optional <dir/>
          ISO-like timestamp (YYYY-MM-DDTHH:MM:SS[.microseconds])
          hyphen UUID-ish suffix (we don't strictly validate uuid5 here, just hex/dashes)
          .json extension
        """
        pattern = r"^(?:[\w\-./]+/)?\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?-[0-9a-fA-F-]+\.json$"
        return bool(re.match(pattern, value))

    # ----------------- ORM integration -----------------
    def from_db_value(self, value, *args, **kwargs):  # type: ignore[override]
        return self.to_python(value)

    def to_python(self, value, model_instance=None):  # type: ignore[override]
        if not value:
            return None
        if isinstance(value, RemoteJSONProxy):
            return value
        if isinstance(value, dict):
            return RemoteJSONProxy(value)
        if isinstance(value, (list, int, float, bool)):
            return RemoteJSONProxy(value)
        if isinstance(value, str):
            if self.is_file_path(value):
                return RemoteJSONProxy(file_path=value)
            return RemoteJSONProxy(value)
        raise ValueError(
            f"RemoteJSONField could not convert value {value} - {type(value)} to python"
        )

    def get_prep_value(self, value) -> Any:  # type: ignore[override]
        if isinstance(value, str):
            return value
        if value is None:
            return None
        if isinstance(value, RemoteJSONProxy):
            # Ensure DB always stores the path (not actual JSON string)
            return value._file_path
        raise TypeError(f"Unsupported raw type for RemoteJSONField: {value} - {type(value)}")

    def pre_save(self, model_instance, add):  # type: ignore[override]
        raw_value = self.raw_value(model_instance)
        if raw_value:
            file_path = raw_value
        else:
            file_path = self.generate_file_path(model_instance)

        value = getattr(model_instance, self.attname)

        if value is None:
            # Delete old file if present
            if raw_value:
                default_storage.delete(file_path)
            setattr(model_instance, self.attname, None)
            return None

        if isinstance(value, RemoteJSONProxy):
            if not value._file_path:
                value._file_path = file_path
            if not value.needs_save:
                setattr(model_instance, self.attname, value)
                return value._file_path
            json_data = json.dumps(value.get())
            value.mark_saved()
        else:
            json_data = json.dumps(value)

        # Replace existing file to avoid orphaning
        if raw_value:
            default_storage.delete(file_path)
        default_storage.save(file_path, ContentFile(json_data.encode("utf-8")))

        proxy = value if isinstance(value, RemoteJSONProxy) else RemoteJSONProxy(file_path=file_path)
        setattr(model_instance, self.attname, proxy)
        return file_path

    # ----------------- Debug logging (optional) -----------------
    @staticmethod
    def _debug_logs(message, *args, **kwargs):  # pragma: no cover - optional
        if settings.DEBUG:
            logging.info(message, *args, **kwargs)
