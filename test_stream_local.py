import httpx
import time
import json

def test():
    print("Testing local chat_stream...")
    with httpx.Client() as client:
        # We need to simulate a real request. Since there's no auth, we'll hit an endpoint that just streams if possible, or we'll bypass auth.
        # Actually, let's just check the headers of the 401 response, maybe it has our headers if it was an SSE endpoint? No, 401 is JSON.
        pass

# Instead of full auth, let's just write a test script that tests if Starlette sets headers correctly on StreamingResponse.
import asyncio
from starlette.responses import StreamingResponse

async def gen():
    yield "a"
    await asyncio.sleep(1)
    yield "b"

resp = StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})
print(resp.headers)
