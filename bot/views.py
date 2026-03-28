import json
import logging
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from telegram import Update
from bot.bot_app import get_ptb_application

logger = logging.getLogger(__name__)


@csrf_exempt
async def telegram_webhook(request):
    """
    Async Django view — processes Telegram webhook updates.
    Requires running under an ASGI server (uvicorn) so the
    event loop is persistent and PTB's HTTP client stays alive.
    """
    if request.method != "POST":
        return JsonResponse({"status": "only POST allowed"}, status=405)

    try:
        app = get_ptb_application()
        if not app:
            return JsonResponse({"status": "error", "message": "Bot not configured"}, status=500)

        # Initialize once — safe to call repeatedly, PTB is idempotent here.
        if not app._initialized:
            await app.initialize()

        payload = json.loads(request.body.decode("utf-8"))
        update = Update.de_json(payload, app.bot)
        await app.process_update(update)

        return JsonResponse({"status": "ok"})
    except Exception as exc:
        logger.error(f"Error handling webhook update: {exc}")
        return JsonResponse({"status": "error"}, status=500)
