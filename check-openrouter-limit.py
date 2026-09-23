import requests
import os
import json
from dotenv import load_dotenv

load_dotenv()

response = requests.get(
    url="https://openrouter.ai/api/v1/key",
    headers={
        "Authorization": f"Bearer {os.getenv('OPENROUTER_API_KEY')}"
    }
)

data = response.json()

print(json.dumps(data, indent=2))