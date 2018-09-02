from django.urls import path

from . import views

app_name = 'vroombaby'
urlpatterns = [
    path('', views.index, name='index'),
]