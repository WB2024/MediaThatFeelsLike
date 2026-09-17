from django.urls import path

from . import views

app_name = "vibes"

urlpatterns = [
    path("", views.home, name="home"),
    path("post/<int:pk>/", views.post_detail, name="post_detail"),
    path("post/<int:post_pk>/recs/add/", views.rec_add, name="rec_add"),
    path("post/<int:post_pk>/recs/bulk/", views.rec_bulk, name="rec_bulk"),
    path("post/<int:post_pk>/reparse/", views.post_reparse, name="post_reparse"),
    path("post/<int:post_pk>/refresh/", views.post_refresh, name="post_refresh"),
    path("post/<int:post_pk>/flag/<str:flag>/", views.post_flag, name="post_flag"),
    path("rec/<int:pk>/toggle/", views.rec_toggle, name="rec_toggle"),
    path("rec/<int:pk>/edit/", views.rec_edit, name="rec_edit"),
    path("rec/<int:pk>/delete/", views.rec_delete, name="rec_delete"),
    path("<str:kind>/", views.section, name="section"),
]
