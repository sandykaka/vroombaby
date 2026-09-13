from django.urls import path

from . import views

app_name = 'castmute'

urlpatterns = [
    path('', views.marketing, name='marketing'),
    path('support/', views.support, name='support'),
    path('privacy/', views.privacy, name='privacy'),

    # Over-the-air updates for the hardware. This URL is compiled into the firmware,
    # so it must not move once units have shipped — a device cannot be told a new
    # address by the mechanism it can no longer reach.
    path('firmware/manifest.json', views.firmware_manifest, name='firmware_manifest'),
]
