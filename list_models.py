import os
import requests
import urllib3

urllib3.disable_warnings()

key = os.environ["GEMINI_API_KEY"]

url = "https://generativelanguage.googleapis.com/v1beta/models"

r = requests.get(
    url,
    params={"key": key},
    verify=False
)

print("HTTP:", r.status_code)

data = r.json()

if r.ok:
    for model in data.get("models", []):
        name = model.get("name", "")
        methods = model.get("supportedGenerationMethods", [])

        if "generateContent" in methods:
            print(name)

else:
    print(data)