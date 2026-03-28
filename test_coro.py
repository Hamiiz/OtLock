import asyncio
from django.views.decorators.csrf import csrf_exempt

@csrf_exempt
async def my_view(request):
    pass

print("IS_CORO:", asyncio.iscoroutinefunction(my_view))
