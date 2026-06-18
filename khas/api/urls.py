from django.urls import path, include

urlpatterns = [
    path("chatbot/", include("api.controllers.chatbot.urls")),
]