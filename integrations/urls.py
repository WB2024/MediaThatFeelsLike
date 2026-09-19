from django.urls import path

from . import api, views

app_name = "integrations"

urlpatterns = [
    path("settings/", views.settings_page, name="settings"),
    path("settings/sources/", views.save_sources, name="save_sources"),
    path("settings/<str:service>/", views.save_service, name="save_service"),
    path("settings/<str:service>/test/", views.test_service, name="test_service"),
    path("push/<int:post_pk>/<str:service>/", views.push, name="push"),
    path("push-rec/<int:rec_pk>/<str:service>/", views.push_rec, name="push_rec"),
    path("api/slskd/", api.slskd_status, name="api_slskd"),
    # recommendation detail page panels
    path("rec/<int:rec_pk>/card/<str:service>/", views.rec_card, name="rec_card"),
    path("rec/<int:rec_pk>/artist/", views.rec_artist, name="rec_artist"),
    path("rec/<int:rec_pk>/slskd/search/", views.rec_slskd_search, name="rec_slskd_search"),
    path("rec/<int:rec_pk>/slskd/download/", views.rec_slskd_download, name="rec_slskd_download"),
]
