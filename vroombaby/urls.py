from django.http import HttpResponse
from django.urls import path

from . import views

app_name = 'vroombaby'
urlpatterns = [
    path('', views.index, name='index'),
    path(r'^googleeb914ff572b518f7\.html$', lambda r: HttpResponse("google-site-verification: googleeb914ff572b518f7.html", mimetype="text/plain")),

]