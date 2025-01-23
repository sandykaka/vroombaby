from django.http import HttpResponse
from django.urls import path

from . import views

app_name = 'business'
urlpatterns = [
    path('', views.index, name='index'),
    path('index.html', views.index, name='index'),
    path('products.html',views.products, name='products'),
    path('googleeb914ff572b518f7', views.googleeb914ff572b518f7),
    path('oauth/callback/', views.oauth_callback, name='oauth_callback'),

]
