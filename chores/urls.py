from django.urls import path

from . import views

app_name = 'chores'

urlpatterns = [
    path('', views.dashboard, name='dashboard'),
    path('chores/<int:pk>/done/', views.mark_done, name='mark_done'),
    path('chores/<int:pk>/assign/', views.assign, name='assign'),
    path('pickup/', views.pickup_setup, name='pickup_setup'),
    path('calendar/', views.calendar_view, name='calendar'),
    path('tasks/new/', views.new_task, name='new_task'),
    path('users/new/', views.new_user, name='new_user'),
]
