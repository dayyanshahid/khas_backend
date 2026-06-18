import json
import logging

from django.http import StreamingHttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from rest_framework.decorators import api_view
from rest_framework.response import Response

from api.controllers.chatbot import services

logger = logging.getLogger(__name__)


@csrf_exempt
def reply(request):
    """
    POST /api/chatbot/reply/

    Streams the chatbot response as Server-Sent Events.
    Each frame is `data: <json>\\n\\n`.
    """
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)

    try:
        body = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    message = (body.get("message") or "").strip()
    if not message:
        return JsonResponse({"error": "`message` is required"}, status=400)

    conv_id = (body.get("conv_id") or "").strip()
    sender  = body.get("sender") or "user"

    def event_stream():
        try:
            for event in services.create_reply(
                conv_id=conv_id, message=message, sender=sender,
            ):
                yield f"data: {json.dumps(event, default=str)}\n\n"
        except GeneratorExit:
            return
        except Exception as exc:
            logger.exception("reply stream crashed")
            payload = {"type": "error", "message": str(exc)}
            yield f"data: {json.dumps(payload)}\n\n"

    response = StreamingHttpResponse(event_stream(), content_type="text/event-stream")
    response["Cache-Control"] = "no-cache"
    response["X-Accel-Buffering"] = "no"  # tell nginx not to buffer
    return response


@api_view(["GET"])
def list_conversations(request):
    data, http_status = services.list_conversations()
    return Response(data, status=http_status)


@api_view(["GET"])
def get_conversation(request, conv_id):
    data, http_status = services.get_conversation(conv_id)
    return Response(data, status=http_status)


@api_view(["DELETE"])
def delete_conversation(request, conv_id):
    data, http_status = services.delete_conversation(conv_id)
    return Response(data, status=http_status)
