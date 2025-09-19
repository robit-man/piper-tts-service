# TTS Service

Automatic Deployment with Piper and ONNX models

## Quickstart

Run in terminal
```
git clone --depth=1 https://github.com/robit-man/piper-tts-service.git && cd piper-tts-service && python3 tts_service.py
```
Then, in another terminal, test with
```
curl -s -X POST "http://127.0.0.1:8123/speak"   -H 'Content-Type: application/json'   -d '{"text":"Example playback over.","mode":"play"}' | jq
```

This assumes you’ve saved and started the service:

```bash
python3 tts_service.py
# Service default: http://0.0.0.0:8123
````

In a second terminal, set a couple of helpers (adjust host/port as needed):

```bash
BASE="http://localhost:8123"
API_KEY="$(grep -E '^TTS_API_KEY=' .env | cut -d= -f2)"   # same dir as the service
AUTH_KEY_H=("X-API-Key: $API_KEY")                        # header array usable with curl -H
```

> If `TTS_REQUIRE_AUTH=0` (default), you can omit `-H "${AUTH_KEY_H[@]}"` from examples.

---

## 0) Health & Models

### Health

```bash
curl -s "$BASE/health" | jq
```

### List locally available voices/models

```bash
curl -s -H "${AUTH_KEY_H[@]}" "$BASE/models" | jq
```

---

## 1) Authentication (optional but recommended)

### Handshake → short-lived session token

```bash
TOKEN="$(
  curl -s -X POST "$BASE/handshake" \
    -H 'Content-Type: application/json' \
    -d "{\"api_key\":\"$API_KEY\"}" | jq -r .session_key
)"
echo "$TOKEN"
```

Use it like:

```bash
AUTH_B=("Authorization: Bearer $TOKEN")
# Example:
curl -s -H "${AUTH_B[@]}" "$BASE/models" | jq
```

> You can use either API key header or Bearer session; both shown throughout.

---

## 2) Synthesize to a File (returns a download URL)

### Minimal (OGG output)

```bash
curl -s -X POST "$BASE/speak" \
  -H 'Content-Type: application/json' \
  -H "${AUTH_KEY_H[@]}" \
  -d '{"text":"Hello from Piper!","mode":"file"}' | jq
```

### Save the returned file locally (parse URL)

```bash
URL="$(
  curl -s -X POST "$BASE/speak" \
    -H 'Content-Type: application/json' \
    -H "${AUTH_KEY_H[@]}" \
    -d '{"text":"Saving to file example.","mode":"file"}' \
  | jq -r --arg b "$BASE" '$b + (.files[0].url)'
)"
curl -L "$URL" -o out.ogg && echo "Saved → out.ogg"
```

### WAV instead of OGG

```bash
URL="$(
  curl -s -X POST "$BASE/speak" \
    -H 'Content-Type: application/json' \
    -H "${AUTH_KEY_H[@]}" \
    -d '{"text":"WAV example.","mode":"file","format":"wav"}' \
  | jq -r --arg b "$BASE" '$b + (.files[0].url)'
)"
curl -L "$URL" -o out.wav && echo "Saved → out.wav"
```

### Use a specific model (by base name from `/models`)

```bash
curl -s -X POST "$BASE/speak" \
  -H 'Content-Type: application/json' -H "${AUTH_KEY_H[@]}" \
  -d '{"text":"Specific voice.","mode":"file","model":"glados_piper_medium"}' | jq
```

### Use a specific model by **path** to .onnx

```bash
curl -s -X POST "$BASE/speak" \
  -H 'Content-Type: application/json' -H "${AUTH_KEY_H[@]}" \
  -d '{"text":"Path-based voice.","mode":"file","model":"voices/myvoice.onnx"}' | jq
```

### Disable sentence splitting (single pass)

```bash
curl -s -X POST "$BASE/speak" \
  -H 'Content-Type: application/json' -H "${AUTH_KEY_H[@]}" \
  -d '{"text":"One big chunk.","mode":"file","split":"none"}' | jq
```

### Adjust volume server-side (0..1)

```bash
curl -s -X POST "$BASE/speak" \
  -H 'Content-Type: application/json' -H "${AUTH_KEY_H[@]}" \
  -d '{"text":"Quieter speech.","mode":"file","volume":0.3}' | jq
```

---

## 3) Stream Live Audio over HTTP

### Stream OGG to **ffplay**

```bash
curl -s -X POST "$BASE/speak" \
  -H 'Content-Type: application/json' \
  -H "${AUTH_KEY_H[@]}" \
  -d '{"text":"This is a streaming OGG test.","mode":"stream","format":"ogg"}' \
| ffplay -autoexit -nodisp -i pipe:0
```

### Stream WAV to **ffplay**

```bash
curl -s -X POST "$BASE/speak" \
  -H 'Content-Type: application/json' \
  -H "${AUTH_KEY_H[@]}" \
  -d '{"text":"Streaming WAV test.","mode":"stream","format":"wav"}' \
| ffplay -autoexit -nodisp -i pipe:0
```

### Stream **raw PCM** to **aplay** (mono 22050Hz, 16-bit)

```bash
curl -s -X POST "$BASE/speak" \
  -H 'Content-Type: application/json' \
  -H "${AUTH_KEY_H[@]}" \
  -d '{"text":"Raw PCM streaming.","mode":"stream","format":"raw"}' \
| aplay -r 22050 -f S16_LE -c 1
```

### Stream and save to file via `tee` (OGG)

```bash
curl -s -X POST "$BASE/speak" \
  -H 'Content-Type: application/json' -H "${AUTH_KEY_H[@]}" \
  -d '{"text":"Stream & save.","mode":"stream","format":"ogg"}' \
| tee stream_out.ogg >/dev/null
```

---

## 4) Play Through the **Server Host** Speakers

(Requires `aplay` or `ffplay` on the host running the service.)

```bash
curl -s -X POST "$BASE/speak" \
  -H 'Content-Type: application/json' -H "${AUTH_KEY_H[@]}" \
  -d '{"text":"This plays on the server machine.","mode":"play"}' | jq
```

---

## 5) Long Text from a File / STDIN

### From a file (`speech.txt`)

```bash
TEXT="$(< speech.txt)"
jq -n --arg t "$TEXT" '{text:$t, mode:"file"}' \
| curl -s -X POST "$BASE/speak" \
    -H 'Content-Type: application/json' -H "${AUTH_KEY_H[@]}" \
    -d @- | jq
```

### One-liner heredoc

```bash
curl -s -X POST "$BASE/speak" \
  -H 'Content-Type: application/json' -H "${AUTH_KEY_H[@]}" \
  -d @- <<'JSON' | jq
{ "text": "This JSON came from a heredoc.", "mode": "file", "format": "ogg" }
JSON
```

---

## 6) Pull/Add a New Voice (requires auth & `TTS_ALLOW_PULL=1`)

```bash
curl -s -X POST "$BASE/models/pull" \
  -H 'Content-Type: application/json' \
  -H "${AUTH_KEY_H[@]}" \
  -d '{
    "name": "newvoice",
    "onnx_model_url": "https://example.com/path/to/voice.onnx",
    "onnx_json_url":  "https://example.com/path/to/voice.onnx.json"
  }' | jq
```

Then use it:

```bash
curl -s -X POST "$BASE/speak" \
  -H 'Content-Type: application/json' -H "${AUTH_KEY_H[@]}" \
  -d '{"text":"Using the new voice.","mode":"file","model":"newvoice"}' | jq
```

---

## 7) Static File Retrieval

If you got a `url` like `/files/abcd.ogg` back from `/speak`:

```bash
curl -L "$BASE/files/abcd.ogg" -o local_copy.ogg
```

---

## 8) Quick Smoke Tests

```bash
# Health
curl -s "$BASE/health" | jq

# Models
curl -s -H "${AUTH_KEY_H[@]}" "$BASE/models" | jq

# Simple file synth
curl -s -X POST "$BASE/speak" \
  -H 'Content-Type: application/json' -H "${AUTH_KEY_H[@]}" \
  -d '{"text":"Smoke test okay.","mode":"file"}' | jq

# Stream to ffplay
curl -s -X POST "$BASE/speak" \
  -H 'Content-Type: application/json' -H "${AUTH_KEY_H[@]}" \
  -d '{"text":"Smoke stream.","mode":"stream","format":"ogg"}' \
| ffplay -autoexit -nodisp -i pipe:0
```

---

### Notes

* Install helpers if missing:

  * `jq`: parses JSON in these examples.
  * `ffplay`: part of `ffmpeg` (used for playback/encoding).
  * `aplay`: from ALSA (Linux) for raw PCM playback.
* If you set `TTS_REQUIRE_AUTH=1` in `.env`, use either `X-API-Key: $API_KEY` or a Bearer token from `/handshake`.
* For remote machines, change `BASE` to point at your server IP/port.

```
