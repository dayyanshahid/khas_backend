from rest_framework import serializers


class ChatMessageSerializer(serializers.Serializer):
    sender = serializers.ChoiceField(
        choices=["user", "ai"],
        read_only=True
    )
    message = serializers.CharField(read_only=True)
    date_time = serializers.DateTimeField(read_only=True)


class ChatBotSerializer(serializers.Serializer):

    conv_id = serializers.CharField(
        required=False,
        allow_blank=True,
        allow_null=True,
        default=None
    )

    message = serializers.CharField(
        write_only=True,
        required=True,
        allow_blank=False
    )

    # Frontend should only send "user"
    sender = serializers.ChoiceField(
        choices=["user"],
        write_only=True,
        required=False,
        default="user"
    )

    # New structured response
    messages = ChatMessageSerializer(
        many=True,
        read_only=True
    )

    route = serializers.CharField(read_only=True)

    generated_sql = serializers.CharField(
        read_only=True,
        allow_blank=True,
        allow_null=True
    )

    sql_result_raw = serializers.JSONField(read_only=True, allow_null=True)

    visualizations = serializers.JSONField(read_only=True, allow_null=True)

    duration_ms = serializers.IntegerField(read_only=True)