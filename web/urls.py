from django.urls import path

from web import views

app_name = "web"

urlpatterns = [
    path("", views.home, name="home"),
    path("bacheca/", views.dashboard, name="dashboard"),
    path("bacheca/riscatta/<uuid:credit_id>/sicuro/", views.claim_credit_safe, name="claim_safe"),
    path("bacheca/riscatta/<uuid:credit_id>/rischio/", views.claim_credit_risky, name="claim_risky"),
    path("beneficenza/", views.charity, name="charity"),
    path("equita/", views.fairness, name="fairness"),
]
