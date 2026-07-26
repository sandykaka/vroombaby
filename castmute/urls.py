from django.urls import path

from . import views

app_name = 'castmute'

urlpatterns = [
    path('', views.marketing, name='marketing'),
    path('support/', views.support, name='support'),
    path('privacy/', views.privacy, name='privacy'),
]
