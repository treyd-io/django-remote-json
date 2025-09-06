from django.db import models
from django_remote_json import RemoteJSONField


class SampleModel(models.Model):
    remote_data = RemoteJSONField(null=True)
