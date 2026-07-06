import requests
import json
import time

def test_stream():
    url = "http://localhost:7000/api/chat_stream"
    data = {
        "message": "Run this python code to test heartbeat: import time; time.sleep(10); print('done')",
        "mode": "agent",
        "session": "test_session_123"
    }
    
    print("Starting stream request...")
    with requests.post(url, data=data, stream=True, timeout=60) as r:
        for line in r.iter_lines():
            if line:
                print(line.decode('utf-8'))

if __name__ == "__main__":
    test_stream()
