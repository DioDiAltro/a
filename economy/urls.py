from django.urls import path

from economy import views

app_name = "economy"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
]
