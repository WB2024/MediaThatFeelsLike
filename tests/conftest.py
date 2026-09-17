import pytest


@pytest.fixture(autouse=True)
def plain_static_storage(settings):
    """The manifest storage needs collectstatic to have run; tests don't care."""
    settings.STORAGES = {
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }
