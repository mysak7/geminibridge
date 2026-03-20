import time
import requests

API_URL = "http://10.77.77.11:8011/v1/chat/completions"
API_KEY = "test"

payload = {"prompt": "Say hello and tell me a joke."}

print(f"Sending request to {API_URL}...")
start = time.time()

try:
    response = requests.post(
        API_URL,
        json=payload,
        headers={"Authorization": f"Bearer {API_KEY}"},
        timeout=120,
    )
    elapsed = time.time() - start

    response.raise_for_status()
    data = response.json()
    content = data["choices"][0]["message"]["content"]

    print(f"\n--- Response ---\n{content}")
    print(f"\nTime: {elapsed:.2f}s")

except requests.exceptions.ConnectionError:
    print("Error: Cannot connect. Is the server running?")
except requests.exceptions.Timeout:
    print(f"Timeout after {time.time() - start:.0f}s")
except Exception as e:
    print(f"Error: {e}")
