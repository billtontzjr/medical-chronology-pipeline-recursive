"""Smoke-test that each external service authenticates correctly."""

import sys
from dotenv import load_dotenv
import os

load_dotenv()

results = []

# --- 1. Anthropic ---
try:
    import anthropic

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=10,
        messages=[{"role": "user", "content": "Reply with the word OK."}],
    )
    assert resp.content[0].text, "Empty response"
    print("  ✅ Anthropic: PASS")
    results.append(True)
except Exception as e:
    print(f"  ❌ Anthropic: FAIL — {e}")
    results.append(False)

# --- 2. Google Vision ---
try:
    import httpx

    api_key = os.environ["GOOGLE_VISION_API_KEY"]
    url = f"https://vision.googleapis.com/v1/images:annotate?key={api_key}"
    # Minimal request: a tiny 1x1 white PNG, base64-encoded
    import base64

    # 1x1 white pixel PNG
    tiny_png = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
        b"\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00\x00"
        b"\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18"
        b"\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    body = {
        "requests": [
            {
                "image": {"content": base64.b64encode(tiny_png).decode()},
                "features": [{"type": "LABEL_DETECTION", "maxResults": 1}],
            }
        ]
    }
    r = httpx.post(url, json=body, timeout=15)
    r.raise_for_status()
    data = r.json()
    assert "responses" in data, f"Unexpected response shape: {list(data.keys())}"
    print("  ✅ Google Vision: PASS")
    results.append(True)
except Exception as e:
    print(f"  ❌ Google Vision: FAIL — {e}")
    results.append(False)

# --- 3. Dropbox ---
try:
    import httpx as _httpx

    app_key = os.environ["DROPBOX_APP_KEY"]
    app_secret = os.environ["DROPBOX_APP_SECRET"]
    # Request an app-level token using client_credentials grant.
    # This validates the key/secret pair without needing a user refresh token.
    r = _httpx.post(
        "https://api.dropboxapi.com/oauth2/token",
        data={"grant_type": "client_credentials", "scope": "files.metadata.read"},
        auth=(app_key, app_secret),
        timeout=15,
    )
    if r.status_code == 200:
        print("  ✅ Dropbox: PASS")
        results.append(True)
    else:
        err = r.json().get("error_description", r.text)
        print(f"  ❌ Dropbox: FAIL — {r.status_code}: {err}")
        results.append(False)
except Exception as e:
    print(f"  ❌ Dropbox: FAIL — {e}")
    results.append(False)

# --- Summary ---
passed = sum(results)
total = len(results)
print(f"\n  {passed}/{total} services authenticated successfully.")
sys.exit(0 if all(results) else 1)
