from django.urls import path

from charity import views

app_name = "charity"

urlpatterns = [
    path("", views.transparency, name="transparency"),
    path("<slug:slug>/", views.beneficiary_detail, name="beneficiary"),
]
