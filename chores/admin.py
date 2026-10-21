from django.contrib import admin

from .models import Chore, ChoreTemplate, NotificationLog, PickupSchedule


@admin.register(ChoreTemplate)
class ChoreTemplateAdmin(admin.ModelAdmin):
    list_display = ['title', 'area', 'every_days', 'weekdays', 'every_weeks', 'season_start', 'season_end', 'estimated_minutes', 'default_assignee', 'active']
    list_filter = ['active', 'area', 'default_assignee']
    search_fields = ['title', 'area', 'done_when']


@admin.register(Chore)
class ChoreAdmin(admin.ModelAdmin):
    list_display = ['template', 'assignee', 'due_date', 'completed_at', 'completed_by']
    list_filter = ['assignee', ('completed_at', admin.EmptyFieldListFilter), 'template__area']
    date_hierarchy = 'due_date'


@admin.register(PickupSchedule)
class PickupScheduleAdmin(admin.ModelAdmin):
    list_display = ['stream', 'every_weeks', 'next_pickup', 'am_pickup', 'template']


@admin.register(NotificationLog)
class NotificationLogAdmin(admin.ModelAdmin):
    list_display = ['date', 'kind', 'chore_count', 'delivered', 'created_at']
    list_filter = ['kind', 'delivered']
