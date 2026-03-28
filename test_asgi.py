import asyncio
import os
import django
from asgiref.testing import ApplicationCommunicator

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "tgbot.settings")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "12345:ABCDE")
os.environ.setdefault("ALLOWED_HOSTS", "*")
os.environ.setdefault("DEBUG", "True")
django.setup()

from tgbot.asgi import application

async def run():
    instance = ApplicationCommunicator(
        application,
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "path": "/bot/webhook/",
            "query_string": b"",
            "headers": [(b"content-type", b"application/json"), (b"host", b"otlock.onrender.com")],
        },
    )
    await instance.send_input({"type": "http.request", "body": b'{"update_id": 1234}'})
    response_start = await instance.receive_output()
    response_body = await instance.receive_output()
    print("STATUS:", response_start.get("status"))
    print("BODY:", response_body.get("body", b"").decode())
    
    # Also test the root to make sure no exceptions
    instance2 = ApplicationCommunicator(
        application,
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "path": "/",
            "query_string": b"",
            "headers": [(b"host", b"otlock.onrender.com")],
        },
    )
    await instance2.send_input({"type": "http.request", "body": b''})
    r_start = await instance2.receive_output()
    print("ROOT STATUS:", r_start.get("status"))

asyncio.run(run())
