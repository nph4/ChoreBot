from django.urls import path

from . import views

app_name = 'chores'

urlpatterns = [
    path('', views.dashboard, name='dashboard'),
    path('chores/<int:pk>/done/', views.mark_done, name='mark_done'),
    path('pickup/', views.pickup_setup, name='pickup_setup'),
]
