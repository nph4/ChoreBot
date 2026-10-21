from django.urls import path

from . import views

app_name = 'chores'

urlpatterns = [
    path('', views.dashboard, name='dashboard'),
    path('chores/<int:pk>/done/', views.mark_done, name='mark_done'),
    path('chores/<int:pk>/assign/', views.assign, name='assign'),
    path('chores/<int:pk>/skip/', views.skip, name='skip'),
    path('chores/<int:pk>/undo/', views.undo, name='undo'),
    path('chores/<int:pk>/move/', views.move, name='move'),
    path('chores/spread/', views.spread, name='spread'),
    path('tasks/<int:pk>/edit/', views.edit_template, name='edit_task'),
    path('tasks/<int:pk>/remove/', views.remove, name='remove_task'),
    path('away/', views.away, name='away'),
    path('away/<int:pk>/delete/', views.delete_away, name='delete_away'),
    path('pickup/', views.pickup_setup, name='pickup_setup'),
    path('calendar/', views.calendar_view, name='calendar'),
    path('stats/', views.stats_view, name='stats'),
    path('tasks/', views.chore_list, name='chore_list'),
    path('tasks/new/', views.new_task, name='new_task'),
    path('tasks/<int:pk>/active/', views.set_active, name='set_active'),
    path('users/new/', views.new_user, name='new_user'),
]
