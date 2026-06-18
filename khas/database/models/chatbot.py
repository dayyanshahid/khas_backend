# from django.db import models


# class ChatBot(models.Model):
#     conv_id = models.CharField(max_length=255, db_index=True)
#     message = models.TextField()
#     answer = models.TextField(blank=True)
#     route = models.CharField(max_length=16, default="general")
#     generated_sql = models.TextField(null=True, blank=True)
#     sql_result_raw = models.JSONField(null=True, blank=True)
#     clone_verdict = models.CharField(max_length=32, default="skipped")
#     clone_feedback = models.TextField(null=True, blank=True)
#     regeneration_count = models.IntegerField(default=0)
#     duration_ms = models.IntegerField(default=0)
#     created_at = models.DateTimeField(auto_now_add=True)

#     class Meta:
#         app_label = "database"
#         db_table = "chatbot"
#         ordering = ["created_at"]









"""
ChatBot model — one record per chat turn.
"""

from django.db import models


class ChatBot(models.Model):
    conv_id        = models.CharField(max_length=255, db_index=True)
    message        = models.TextField()
    answer         = models.TextField(blank=True)
    route          = models.CharField(max_length=16, default="general")
    generated_sql  = models.TextField(null=True, blank=True)
    sql_result_raw = models.JSONField(null=True, blank=True)
    visualizations = models.JSONField(null=True, blank=True)
    duration_ms    = models.IntegerField(default=0)
    created_at     = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = "database"
        db_table  = "chatbot"
        ordering  = ["created_at"]