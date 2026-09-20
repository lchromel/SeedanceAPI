from django.urls import path
from studio import auth, views

urlpatterns = [
    path("health", views.health),
    path("api/session", auth.session),
    path("api/login", auth.sign_in),
    path("api/logout", auth.sign_out),
    path("api/bootstrap", views.bootstrap),
    path("api/projects", views.projects),
    path("api/projects/<uuid:pk>", views.project_detail),
    path("api/projects/<uuid:pk>/generate", views.generate),
    path("api/projects/<uuid:pk>/export", views.export),
    path("api/chunks/<uuid:pk>/retry", views.retry),
    path("api/assets", views.upload),
    path("api/characters/sync", views.characters),
    path("api/characters/<uuid:pk>/preview", views.character_preview),
    path("api/media/<path:key>", views.local_media),
    path("", views.index),
]
