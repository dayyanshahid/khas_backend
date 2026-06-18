from django.urls import path
from api.controllers.chatbot import views

urlpatterns = [
    path("reply/", views.reply, name="chatbot-reply"),

    path("conversations/",                       views.list_conversations,  name="chatbot-conversations-list"),
    path("conversations/<str:conv_id>/",         views.get_conversation,    name="chatbot-conversation-detail"),
    path("conversations/<str:conv_id>/delete/",  views.delete_conversation, name="chatbot-conversation-delete"),
]
