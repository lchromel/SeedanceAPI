from django.urls import path
from studio import auth, image_views, views

urlpatterns = [
    path("health", views.health),
    path("api/session", auth.session),
    path("api/login", auth.sign_in),
    path("api/logout", auth.sign_out),
    path("api/bootstrap", views.bootstrap),
    path("api/images", image_views.generations),
    path("api/images/enhance", image_views.enhance_prompt),
    path("api/images/replacements/prompt", image_views.enhance_replacement),
    path("api/images/replacements/generate", image_views.replacement_batch),
    path("api/projects", views.projects),
    path("api/projects/<uuid:pk>", views.project_detail),
    path("api/projects/<uuid:pk>/generate", views.generate),
    path("api/projects/<uuid:pk>/export", views.export),
    path("api/chunks/<uuid:pk>/retry", views.retry),
    path("api/assets", views.upload),
    path("api/assets/<uuid:pk>/preview", views.asset_preview),
    path("api/assets/<uuid:pk>/analyze", views.analyze_asset),
    path("api/assets/<uuid:pk>", views.edit_asset),
    path("api/prompt/references", views.reference_prompt),
    path("api/prompt/enhance", views.enhance_prompt),
    path("api/characters/sync", views.characters),
    path("api/characters/<uuid:pk>/preview", views.character_preview),
    path("api/media/<path:key>", views.local_media),
    path("", views.index),
]
