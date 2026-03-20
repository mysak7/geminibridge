import time
import requests

API_URL = "http://10.77.77.11:8003/v1/chat/completions"
API_KEY = "test"

payload = {"prompt": "Pozdrav mě česky a řekni mi vtip."}

print(f"Odesílám požadavek na {API_URL}...")
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

    print(f"\n--- Odpověď ---\n{content}")
    print(f"\nCas: {elapsed:.2f}s")

except requests.exceptions.ConnectionError:
    print("Chyba: Nelze se připojit. Běží server?")
except requests.exceptions.Timeout:
    print(f"Timeout po {time.time() - start:.0f}s")
except Exception as e:
    print(f"Chyba: {e}")
