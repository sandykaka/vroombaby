from django.http import HttpResponse
from django.urls import path

from . import views

app_name = 'vroombaby'
urlpatterns = [
    path('', views.index, name='index'),
    path('oauth/callback/', views.oauth_callback, name='oauth_callback'),
    path('googleeb914ff572b518f7', views.googleeb914ff572b518f7),
]