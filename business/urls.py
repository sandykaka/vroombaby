from django.http import HttpResponse
from django.urls import path
from . import views

app_name = 'business'
urlpatterns = [
    path('', views.index, name='index'),
    path('index.html', views.index, name='index'),
    path('products.html',views.products, name='products'),
    path('support.html', views.support_view, name='support'),
    path('privacy.html', views.privacy_view, name='privacy'),
    path('googleeb914ff572b518f7', views.googleeb914ff572b518f7),
    path('create-meeting/', views.create_zoom_meeting, name='create_zoom_meeting'),
    path("get-meetings/", views.get_meetings, name="get_meetings"),
    path('delete-meeting/<int:meeting_id>/', views.delete_meeting, name='delete_meeting'),
    path('update-meeting/<int:meeting_id>/', views.update_meeting, name='update_meeting'),
    path('linkedin-login/', views.linkedin_login, name='linkedin_login'),
    path('linkedin-callback/', views.linkedin_callback, name='linkedin_callback'),
]
