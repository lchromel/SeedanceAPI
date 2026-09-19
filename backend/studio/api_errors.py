from rest_framework.response import Response
from rest_framework.views import exception_handler


def handle(exc, context):
    response = exception_handler(exc, context)
    if response is not None:
        return response
    # Never expose provider payloads, secrets or stack traces to clients.
    return Response({"detail": "The operation could not be completed."}, status=500)
