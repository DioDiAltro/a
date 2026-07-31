from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", include("web.urls", namespace="web")),
    path("account/", include("identity.urls", namespace="identity")),
    path("wallet/", include("wallet.urls", namespace="wallet")),
    path("market/", include("marketplace.urls", namespace="marketplace")),
    path("games/", include("games.urls", namespace="games")),
    path("pay/", include("payments.urls", namespace="payments")),
    path("charity/", include("charity.urls", namespace="charity")),
    path("responsabilita/", include("compliance.urls", namespace="compliance")),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
