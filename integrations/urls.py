from django.urls import path

from . import views

app_name = "integrations"

urlpatterns = [
    path("settings/", views.settings_page, name="settings"),
    path("settings/sources/", views.save_sources, name="save_sources"),
    path("settings/<str:service>/", views.save_service, name="save_service"),
    path("settings/<str:service>/test/", views.test_service, name="test_service"),
    path("push/<int:post_pk>/<str:service>/", views.push, name="push"),
]
