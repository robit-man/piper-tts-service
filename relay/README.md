# NKN Relay (Python) — Direct-Message ⇄ HTTP(S) Bridge

A tiny, production-minded bridge that lets any NKN client call HTTP(S) services behind NAT/firewalls using **direct messages (DMs)**. It spins up a minimal Node sidecar for `nkn-sdk` networking, while Python handles HTTP, streaming, queuing, and configuration.

> Works great with the TTS frontend you’ve built: choose **Transport: NKN Relay (DM)** and point `Base URL` to the service as seen **from the relay host** (e.g. `http://127.0.0.1:8123`).

---

## Highlights

* **Two delivery modes**

  * **Single-shot** (fits in one DM):

    * `relay.response` — JSON and/or base64 body, capped by `RELAY_MAX_BODY_B`
  * **Streaming** (many small DMs):

    * `relay.response.begin` → repeated `relay.response.chunk` (base64 slices) → `relay.response.end`
    * Periodic `relay.response.keepalive` frames to avoid idle timeouts
* **Chunk sizing tuned for reliability**
  Splits large bodies into **\~12 KB raw** slices (\~16 KB base64) to avoid NKN timeouts and relay rejection.
* **Order-preserving** with sequence numbers
  The bridge emits `seq` for each chunk; the sample client buffers out-of-order frames and plays them in order.
* **Acked DMs for streams**
  Streaming uses `noReply: false` (acked messages) to reduce loss across relay peers.
* **Queue + worker pool**
  `queue.Queue` + `RELAY_WORKERS` HTTP workers. Robust task accounting (no more `task_done()` mismatches).
* **TLS controls**
  Default verification (`RELAY_VERIFY_DEFAULT`) with per-request overrides (`verify: false` or `insecure_tls: true`).
* **Target routing**
  Use a URL **or** `service` + `path` with `RELAY_TARGETS` mapping.
* **Minimal dependencies**
  Python `requests` + `python-dotenv`, Node sidecar with `nkn-sdk`. The script auto-creates a venv and installs deps.
* **First-run bootstrap**
  Writes a `.env` with a fresh `NKN_SEED_HEX` if missing.
* **Curses TUI (optional)**
  `--debug` shows a live dashboard (IN/OUT/ERR counts, queue depth, event stream).

---

## Quick Diagram

```
Browser/Client  ←NKN DM→  Node MultiClient  ←stdio→  Python relay  →HTTP(S)→  Your Service
                            (nkn-sdk)
```

---

## Requirements

* **Python** 3.8+ (tested on 3.12)
* **Node.js + npm** (for the `nkn-sdk` sidecar)
* Outbound connectivity to NKN seed WS (optionally override with `NKN_BRIDGE_SEED_WS`)

> On Windows, the debug TUI requires `windows-curses` (auto-installed when `--debug` is used).

---

## Installation & First Run

```bash
# Place the script in its folder (e.g., nkn-replay/)
cd nkn-relay/

# Run it. The script will:
#  - create a venv and pip-install deps
#  - write .env with defaults and a new NKN_SEED_HEX if missing
#  - create a Node sidecar folder and npm-install nkn-sdk
python3 nkn_relay.py
```

You should see:

```
→ wrote .env with defaults
→ NKN ready: <your-address.identifier>
```

Run with a live TUI:

```bash
python3 nkn_relay.py --debug
```

---

## Configuration (.env)

The script writes this on first run (edit and re-start to apply):

```ini
NKN_SEED_HEX=<64 hex chars>       # generated if absent; keep it secret
NKN_NUM_SUBCLIENTS=2              # MultiClient fan-out (improves reliability)
NKN_TOPIC_PREFIX=relay            # reserved for future pub/sub use (informational)
RELAY_WORKERS=4                   # HTTP concurrency
RELAY_MAX_BODY_B=1048576          # single-shot body cap (base64 source is truncated after this)
RELAY_VERIFY_DEFAULT=1            # 1 (True) or 0 (False) for TLS verification
RELAY_TARGETS={"tts":"https://127.0.0.1:8123"}  # optional service map
NKN_BRIDGE_SEED_WS=               # CSV of seedWsAddr (e.g. wss://seed-0001.nkn.org:30003,...)
```

### Runtime knobs (source constants)

* `CHUNK_RAW_B = 12 * 1024`
  Raw bytes per **chunk DM** (≈16 KB base64). Lower to be extra conservative; raise cautiously.
* `HEARTBEAT_S = 10`
  Sends `relay.response.keepalive` if the HTTP stream is quiet for this many seconds.

> You can change these in the script; environment overrides are not (yet) wired in.

---

## Protocols

### Event Aliases (incoming)

The relay accepts any of these, normalizing to `relay.http`:

* `"relay.http"`, `"http.request"`, `"relay.fetch"`

### DM — Request (from peer)

Send **one** direct message to the relay:

```json
{
  "event": "relay.http",
  "id": "abc123",
  "req": {
    "url": "https://127.0.0.1:8123/speak",
    "method": "POST",
    "headers": {"Content-Type":"application/json","X-API-Key":"secret"},
    "json": {"text":"Hello","mode":"stream","format":"raw"},
    "timeout_ms": 90000,
    "verify": true,            // optional
    "insecure_tls": false,     // synonym for verify:false
    "stream": "chunks"         // "chunks"|"dm"|true → enable streaming (many small DMs)
  }
}
```

**Alternative routing** with service map:

```json
{
  "event": "relay.http",
  "id": "abc123",
  "req": { "service": "tts", "path": "/speak", "method": "GET" }
}
```

> If you can’t supply `json`, you can use `body_b64` (base64 of the raw request body).

### DM — Response (to peer)

#### Single-shot (non-streaming)

```json
{
  "event": "relay.response",
  "id": "abc123",
  "ok": true,
  "status": 200,
  "headers": { "content-type": "application/json", ... },
  "json": { ... },             // present if response looked like JSON
  "body_b64": "....",          // otherwise (may be truncated by RELAY_MAX_BODY_B)
  "truncated": false,
  "error": null
}
```

#### Streaming (chunked)

```json
// First frame
{"event":"relay.response.begin","id":"abc123","ok":true,"status":200,"headers":{...}}

// Zero or more keepalives (no body, keeps relays alive)
{"event":"relay.response.keepalive","id":"abc123","ts":1716239645123}

// Many chunks (ordered by `seq`, body is base64 of raw bytes)
{"event":"relay.response.chunk","id":"abc123","seq":1,"b64":"..."}
{"event":"relay.response.chunk","id":"abc123","seq":2,"b64":"..."}
// ...

// Last frame
{"event":"relay.response.end","id":"abc123","ok":true,"bytes":123456,"truncated":false,"error":null}
```

> Streaming uses **acked DMs** (`noReply: false`) to improve delivery; the relay lightly paces bursts to play nicely with relays.

---

## Using It

### 1) From the provided web frontend

* **Transport:** `NKN Relay (DM)`
* **NKN Relay Address:** paste the relay’s **full NKN address** (with identifier)
* **Base URL:** the service as seen **from the relay** (e.g. `http://127.0.0.1:8123`)
* For streaming, the frontend automatically sets `X-Relay-Stream: chunks` and reassembles `begin/chunk/end`.

### 2) From a Node script (example)

```js
const nkn = require('nkn-sdk');
const client = new nkn.MultiClient({ identifier: 'tester' });

client.on('connect', async () => {
  const to = '<relay-address.identifier>';   // paste from relay stdout
  const id = 'demo-' + Date.now();

  // Ask relay to stream from /speak
  const req = {
    event: 'http.request',
    id,
    req: {
      url: 'http://127.0.0.1:8123/speak',
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Relay-Stream': 'chunks' },
      json: { text: 'Hello via NKN!', mode: 'stream', format: 'raw' },
      timeout_ms: 120000
    }
  };

  await client.send(to, JSON.stringify(req), { noReply: true });

  client.on('message', (src, payload) => {
    const m = JSON.parse(payload.toString());
    if (m.id !== id) return;
    if (m.event === 'relay.response.begin') console.log('BEGIN', m.status);
    if (m.event === 'relay.response.chunk') console.log('CHUNK', m.seq);
    if (m.event === 'relay.response.end')   console.log('END', m.bytes);
  });
});
```

---

## Operational Notes

### Worker pool & queue

* `RELAY_WORKERS` controls how many concurrent HTTP requests the relay performs.
* Each incoming `relay.http` DM becomes a queue job. Workers run forever and **safely** call `task_done()` exactly once per job.

### TLS verification

* Global default from `RELAY_VERIFY_DEFAULT` (`1` = on).
* Override **per request** via:

  * `"verify": false` **or** `"insecure_tls": true`

> Avoid disabling verification over the public internet unless you know what you’re doing (e.g., local self-signed lab).

### Body caps and truncation

* Single-shot responses are capped at `RELAY_MAX_BODY_B`. Larger payloads must use **streaming**.
* If a response is truncated, you’ll see `"truncated": true` and a toast in the sample frontend.

### Streaming details

* **Chunk size:** raw `CHUNK_RAW_B` (default 12 KB) → base64 per DM.
* **Ordering:** each chunk has `seq`; clients should buffer out-of-order frames and flush in order.
* **Keepalive:** every `HEARTBEAT_S` seconds of inactivity, a keepalive DM is sent to keep relay peers engaged.
* **Backpressure:** the relay lightly **paces** chunk sends to avoid flooding NKN relays.

---

## Debugging & Observability

### Logs

* Standard out:

  * `→ NKN ready: <address>` once the sidecar connects
* With `--debug` (curses TUI):

  * **IN/OUT/ERR** counters, **queue size**, your NKN address, and a rolling event list
  * Lines like:

    * `SYS queue id=... POST http://127.0.0.1:8123/speak`
    * `OUT ... begin id=...`
    * `OUT ... end id=... bytes=39540`
    * `ERR ... id=... <Exception>: <message>`

### Common issues

* **No audio on NKN streaming**
  Ensure your client sends `stream: "chunks"` (or header `X-Relay-Stream: chunks`) and that you reassemble `begin/chunk/end`. The provided frontend does this automatically.
* **“NKN relay timeout”**
  Network hiccup or too-large chunks. Keep `CHUNK_RAW_B` at or below the default; consider setting `NKN_NUM_SUBCLIENTS` to 4 and using `NKN_BRIDGE_SEED_WS`.
* **“Relay truncated body”**
  Increase `RELAY_MAX_BODY_B` or switch to streaming.
* **Self-signed TLS**
  Set `"verify": false` or `"insecure_tls": true` on the request (or set `RELAY_VERIFY_DEFAULT=0` globally).
* **Can’t reach service**
  Remember: `url` is resolved **from the relay host**. Use `127.0.0.1` or LAN IPs as seen locally by the relay.

---

## Security Considerations

* Treat `.env` as **secret** — it contains your `NKN_SEED_HEX`.
* This relay simply **forwards** headers (e.g., `Authorization`, `X-API-Key`) to your HTTP service. Use TLS verification and rate limits in your service.
* If exposing the relay publicly (not recommended), consider adding allowlists and auth at the relay layer.

---

## Configuration Reference

| Key                    | Type        | Default                            | Description                                                            |
| ---------------------- | ----------- | ---------------------------------- | ---------------------------------------------------------------------- |
| `NKN_SEED_HEX`         | hex string  | *(generated)*                      | NKN account seed.                                                      |
| `NKN_NUM_SUBCLIENTS`   | int         | `2`                                | MultiClient sub-connections (higher = more reliability, more sockets). |
| `NKN_TOPIC_PREFIX`     | string      | `relay`                            | Reserved; informational in this script.                                |
| `RELAY_WORKERS`        | int         | `4`                                | HTTP concurrency for the worker pool.                                  |
| `RELAY_MAX_BODY_B`     | int         | `1048576`                          | Single-shot body cap (bytes).                                          |
| `RELAY_VERIFY_DEFAULT` | `0/1`       | `1`                                | Default TLS verification.                                              |
| `RELAY_TARGETS`        | JSON object | `{"tts":"https://127.0.0.1:8123"}` | Service name → base URL map.                                           |
| `NKN_BRIDGE_SEED_WS`   | CSV         | *(empty)*                          | Custom NKN seed ws endpoints (e.g., corporate mirrors).                |

**Source constants** (edit in code):

* `CHUNK_RAW_B = 12 * 1024`
* `HEARTBEAT_S = 10`

---

## Example Workflows

### A) Single-shot JSON API (small response)

1. Client sends:

```json
{
  "event": "relay.http",
  "id": "small-json-1",
  "req": {
    "service": "tts",
    "path": "/health",
    "method": "GET",
    "timeout_ms": 10000
  }
}
```

2. Relay returns one `relay.response` with `json` populated.

### B) Streaming raw PCM from TTS

1. Client sends:

```json
{
  "event": "http.request",
  "id": "stream-tts-1",
  "req": {
    "url": "http://127.0.0.1:8123/speak",
    "method": "POST",
    "headers": { "Content-Type": "application/json", "X-Relay-Stream": "chunks" },
    "json": { "text": "Hello!", "mode": "stream", "format": "raw" },
    "timeout_ms": 120000
  }
}
```

2. Relay emits `begin`, many `chunk`s, optional `keepalive`s, then `end`.

3. Client reassembles bytes in sequence order and plays/handles them.

---

## How It Works (Under the Hood)

* **Node sidecar** (`nkn-sdk` MultiClient) owns NKN networking.
  It normalizes `message` callback signature and pipes DM payloads over stdout; Python sends commands back over stdin.
* **Python core**:

  * **Bridge pumps:** threads read sidecar stdout and stderr (for logs).
  * **Dispatcher:** normalizes events and enqueues `relay.http` jobs.
  * **Worker pool:** makes HTTP(S) calls with `requests`, either:

    * **Single-shot**: gather full body (respecting `RELAY_MAX_BODY_B`) and reply once, or
    * **Streaming**: `stream=True` + `iter_content(CHUNK_RAW_B)`; for each piece, send `chunk` DM with base64 of raw bytes. Also send periodic `keepalive`s when idle.
  * **Reliability tweaks:** streaming DMs are sent with `noReply: false`, tiny pacing between chunks reduces burst loss.

---

## Roadmap / Ideas

* Env overrides for `CHUNK_RAW_B` & `HEARTBEAT_S`
* Optional gzip base64 chunks for texty streams
* Allowlist / ACL and basic auth on the relay
* Prometheus metrics endpoint

---

## FAQ

**Q: Why a Node sidecar?**
A: `nkn-sdk` is stable and well-maintained in Node. The sidecar keeps Python simple and focused on HTTP and orchestration.

**Q: Can I route multiple services?**
A: Yes — add entries to `RELAY_TARGETS` and send DMs with `{ "service": "name", "path": "/..." }`.

**Q: How big can a single response be?**
A: Up to `RELAY_MAX_BODY_B` bytes. For anything larger or for low-latency playback, use **streaming**.

**Q: How do I handle out-of-order chunks?**
A: Use the `seq` field. The provided frontend buffers until the next expected sequence arrives, then flushes in order.

---

## Getting Help

* Run with `--debug` to get the live TUI (queue size, events, errors).
* Turn up logging around your client’s message handling to verify `begin/chunk/end` framing.
* If you see timeouts, reduce `CHUNK_RAW_B`, increase `timeout_ms`, add more `NKN_NUM_SUBCLIENTS`, or configure `NKN_BRIDGE_SEED_WS`.
