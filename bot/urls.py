from django.urls import path
from . import views

urlpatterns = [
    path("webhook/", views.telegram_webhook, name="telegram_webhook"),
    path("dashboard/", views.dashboard_view, name="dashboard"),
    path("dashboard/create/", views.ot_create_view, name="ot-create"),
    path("dashboard/<int:pk>/edit/", views.ot_edit_view, name="ot-edit"),
    path("dashboard/<int:pk>/close/", views.ot_close_view, name="ot-close"),
    path("dashboard/<int:pk>/signups/", views.ot_detail_view, name="ot-detail"),
    path("dashboard/users/", views.user_management_view, name="user-management"),
    path(
        "dashboard/signups/<int:signup_id>/delete/",
        views.delete_signup_view,
        name="signup-delete",
    ),
    path(
        "dashboard/agents/<int:agent_id>/delete/",
        views.delete_agent_view,
        name="agent-delete",
    ),
]
