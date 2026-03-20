import time
import requests

API_URL = "http://gemini:8011/v1/chat/completions"
API_KEY = "test"

payload = {
    "messages": [
        {"role": "user", "content": "What model are you? Please tell me your exact model name and version."}
    ]
}

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

    model = data.get("model", "N/A")
    content = data["choices"][0]["message"]["content"]

    print(f"\n--- Response ---\n{content}")
    print(f"\nModel field in response: {model}")
    print(f"Time: {elapsed:.2f}s")

except requests.exceptions.ConnectionError:
    print("Error: Cannot connect. Is the server running at http://gemini:8011/?")
except requests.exceptions.Timeout:
    print(f"Timeout after {time.time() - start:.0f}s")
except Exception as e:
    print(f"Error: {e}")
    if hasattr(e, 'response') and e.response is not None:
        print(f"Response body: {e.response.text}")
