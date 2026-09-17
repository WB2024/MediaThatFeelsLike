from django.urls import path

from . import views

app_name = "exports"

urlpatterns = [
    path("post/<int:post_pk>/<str:fmt>/", views.download, name="download"),
]
