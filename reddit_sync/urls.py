from django.urls import path

from . import views

app_name = "reddit_sync"

urlpatterns = [
    path("start/", views.start, name="start"),
    path("status/pill/", views.status_pill, name="status_pill"),
    path("status/panel/", views.status_panel, name="status_panel"),
]
