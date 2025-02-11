from django.http import HttpResponse
from django.urls import path
from django.contrib.auth import views as auth_views
from . import views

app_name = 'business'
urlpatterns = [
    path('', views.index, name='index'),
    path('index.html', views.index, name='index'),
    path('products.html',views.products, name='products'),
    path('support.html', views.support_view, name='support'),
    path('googleeb914ff572b518f7', views.googleeb914ff572b518f7),
    path('create-meeting/', views.create_zoom_meeting, name='create_zoom_meeting'),
    path("get-meetings/", views.get_meetings, name="get_meetings"),
    path('delete-meeting/<int:meeting_id>/', views.delete_meeting, name='delete_meeting'),
    path('login/', auth_views.LoginView.as_view(), name='login'),
    path('logout/', auth_views.LogoutView.as_view(), name='logout'),
]
