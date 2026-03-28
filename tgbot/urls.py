from django.contrib import admin
from django.urls import path, include
from django.http import HttpResponse

def health_check(request):
    return HttpResponse("Bot is running!")

urlpatterns = [
    path("", health_check),
    path("admin/", admin.site.urls),
    path("bot/", include("bot.urls")),
]
