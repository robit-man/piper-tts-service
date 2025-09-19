#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TTS Service (Piper) — single-file local network API
---------------------------------------------------

First run:
  - Bootstraps .venv (once), installs pip deps
  - Downloads Piper binary for your OS/arch
  - Downloads default voice ONNX + JSON
  - Generates .env with a server API key (keep it!)

Subsequent runs:
  - Fast start (re-enters existing venv, no re-install)

HTTPS support:
  - CLI:  --ssl[=generate|adhoc]   (default "generate" if no value)
  - .env: TTS_SSL=0|1|generate|adhoc
          TTS_SSL_CERT=path/to/cert.pem   (when TTS_SSL=1)
          TTS_SSL_KEY=path/to/key.pem     (when TTS_SSL=1)
          TTS_SSL_EXTRA_DNS_SANS=host1,host2  (optional)
          TTS_SSL_REFRESH=0|1  (re-generate self-signed on startup)

HTTP API (default bind 0.0.0.0:8123)
------------------------------------
Auth:
  - If TTS_REQUIRE_AUTH=1 (default 0), endpoints require either:
      * Header:  X-API-Key: <TTS_API_KEY>
      * Or      Authorization: Bearer <session_key>  (after /handshake)
  - /handshake exchanges your API key for a short-lived session key.

Endpoints:
  GET  /health
      → {"status":"ok","piper":"ready|missing","voices":[...]}
  GET  /models
      → {"models":[{"name","onnx","json","size_bytes","mtime"}...]}

  POST /handshake  (body: {"api_key":"..."} )
      → {"session_key":"<uuid>","expires_in":<sec>}

  POST /speak
      JSON body:
        {
          "text": "Hello world.",
          "model": "glados_piper_medium",   # optional; base name or path; defaults to config
          "mode":  "file"|"play"|"stream",  # default "file"
          "format":"ogg"|"wav"|"raw",       # for file/stream; default "ogg"
          "volume": 0.2,                    # 0..1 (server-side gain)
          "split": "auto"|"none"            # default "auto" (split into natural chunks)
        }

      mode="file" → returns JSON with a downloadable URL:
        {"ok":true,"files":[{"filename","url","path"}], "chunks": N}

      mode="play" → plays locally on the host (aplay/ffplay required)
        {"ok":true,"played_chunks": N}

      mode="stream" → returns a streaming HTTP response of audio bytes
        headers: Content-Type: audio/ogg (or audio/L16 for raw; audio/wav for wav)

  POST /models/pull    (requires auth; for adding more voices)
      JSON:
        {
          "name": "myvoice",  # optional; base name
          "onnx_model_url": "https://.../voice.onnx",
          "onnx_json_url":  "https://.../voice.onnx.json"
        }
      → downloads into ./voices/, makes it available via /models

Static:
  GET /files/<filename>  (serves generated audio files from ./tts_out)

Quick CLI tests
---------------
# list models
curl -s http://localhost:8123/models | jq

# synthesize to file (returns a download URL)
curl -s -X POST http://localhost:8123/speak \
  -H 'Content-Type: application/json' \
  -d '{"text":"Hello from Piper!","mode":"file"}' | jq

# stream live OGG (listen in a player that accepts stdin)
curl -s -X POST http://localhost:8123/speak \
  -H 'Content-Type: application/json' \
  -d '{"text":"Streaming test.","mode":"stream","format":"ogg"}' \
  | ffplay -autoexit -nodisp -i pipe:0

# play through the server's speakers
curl -s -X POST http://localhost:8123/speak \
  -H 'Content-Type: application/json' \
  -d '{"text":"This will play on the server host.","mode":"play"}' | jq
"""
import os, sys, platform, shutil, subprocess, json, time, uuid, threading, re, math, ssl, socket
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ─────────────────────────────────────────────────────────────────────────────
# 0) Minimal re-exec into a local venv (create once, then fast-start)
# ─────────────────────────────────────────────────────────────────────────────
VENV_DIR = Path.cwd() / ".venv"

def _in_venv() -> bool:
    base = getattr(sys, "base_prefix", None)
    return base is not None and sys.prefix != base

def _ensure_venv_and_reexec():
    if sys.version_info < (3, 9):
        print("ERROR: Python 3.9+ required.", file=sys.stderr)
        sys.exit(1)

    if not _in_venv():
        python = sys.executable
        if not VENV_DIR.exists():
            print(f"[PROCESS] Creating virtualenv at {VENV_DIR}…")
            subprocess.check_call([python, "-m", "venv", str(VENV_DIR)])
            pip_bin = str(VENV_DIR / ("Scripts/pip.exe" if os.name == "nt" else "bin/pip"))
            subprocess.check_call([pip_bin, "install", "--upgrade", "pip"])
        py_bin = str(VENV_DIR / ("Scripts/python.exe" if os.name == "nt" else "bin/python"))
        new_env = os.environ.copy()
        new_env["VIRTUAL_ENV"] = str(VENV_DIR)
        if os.name != "nt":
            new_env["PATH"] = f"{VENV_DIR}/bin:{new_env.get('PATH','')}"
        os.execve(py_bin, [py_bin] + sys.argv, new_env)

_ensure_venv_and_reexec()

# From here on, we are inside the venv.
# ─────────────────────────────────────────────────────────────────────────────
# 1) First-run pip deps, .env, folders, Piper + default voice setup
# ─────────────────────────────────────────────────────────────────────────────
SETUP_MARKER = Path(".tts_setup_complete")
SCRIPT_DIR   = Path(__file__).resolve().parent
OUT_DIR      = SCRIPT_DIR / "tts_out"
VOICES_DIR   = SCRIPT_DIR / "voices"
PIPER_DIR    = SCRIPT_DIR / "piper"
TLS_DIR      = SCRIPT_DIR / "tls"   # default location for generated certs

def _pip(*pkgs):
    subprocess.check_call([sys.executable, "-m", "pip", "install", *pkgs])

if not SETUP_MARKER.exists():
    print("[PROCESS] Installing Python dependencies…")
    _pip("--upgrade", "pip")
    # Include cryptography for self-signed cert generation
    _pip("flask", "flask-cors", "numpy", "python-dotenv", "cryptography")

    # Create .env on first run (with a default API key)
    env_path = SCRIPT_DIR / ".env"
    if not env_path.exists():
        default_key = uuid.uuid4().hex
        env_path.write_text(
            "TTS_API_KEY={key}\n"
            "TTS_BIND=0.0.0.0\n"
            "TTS_PORT=8123\n"
            "TTS_REQUIRE_AUTH=0\n"
            "TTS_SESSION_TTL=1800\n"
            "TTS_ALLOW_PULL=1\n"
            "TTS_FORCE_LOCAL=0\n"
            "\n"
            "# HTTPS options: 0|1|generate|adhoc\n"
            "TTS_SSL=0\n"
            "TTS_SSL_CERT=tls/cert.pem\n"
            "TTS_SSL_KEY=tls/key.pem\n"
            "TTS_SSL_EXTRA_DNS_SANS=\n"
            "TTS_SSL_REFRESH=0\n"
            "\n"
            "# Defaults for Piper + default voice\n"
            "PIPER_BASE_URL=https://github.com/rhasspy/piper/releases/download/2023.11.14-2/\n"
            "PIPER_EXE=piper\n"
            "PIPER_RELEASE_LINUX_X86_64=piper_linux_x86_64.tar.gz\n"
            "PIPER_RELEASE_LINUX_ARM64=piper_linux_aarch64.tar.gz\n"
            "PIPER_RELEASE_LINUX_ARMV7L=piper_linux_armv7l.tar.gz\n"
            "PIPER_RELEASE_MACOS_X64=piper_macos_x64.tar.gz\n"
            "PIPER_RELEASE_MACOS_ARM64=piper_macos_aarch64.tar.gz\n"
            "PIPER_RELEASE_WINDOWS=piper_windows_amd64.zip\n"
            "\n"
            "ONNX_JSON_FILENAME=glados_piper_medium.onnx.json\n"
            "ONNX_MODEL_FILENAME=glados_piper_medium.onnx\n"
            "ONNX_JSON_URL=https://raw.githubusercontent.com/robit-man/EGG/main/voice/glados_piper_medium.onnx.json\n"
            "ONNX_MODEL_URL=https://raw.githubusercontent.com/robit-man/EGG/main/voice/glados_piper_medium.onnx\n"
        .format(key=default_key)
        )
        print(f"[SUCCESS] Wrote .env (TTS_API_KEY={default_key}).")

    # Prepare folders
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    VOICES_DIR.mkdir(parents=True, exist_ok=True)
    PIPER_DIR.mkdir(parents=True, exist_ok=True)
    TLS_DIR.mkdir(parents=True, exist_ok=True)

    SETUP_MARKER.write_text("ok")
    print("[SUCCESS] Base Python setup complete. Restarting…")
    os.execv(sys.executable, [sys.executable] + sys.argv)

# ─────────────────────────────────────────────────────────────────────────────
# 2) Now load runtime deps & env
# ─────────────────────────────────────────────────────────────────────────────
from flask import Flask, request, send_from_directory, Response, jsonify
from flask_cors import CORS
import numpy as np
from dotenv import load_dotenv

load_dotenv(SCRIPT_DIR / ".env")

# Parse CLI --ssl (optional value)
def _parse_cli_ssl():
    mode = None
    args = sys.argv[1:]
    for i, a in enumerate(args):
        if a == "--ssl":
            mode = "generate"  # default if no value
            break
        if a.startswith("--ssl="):
            mode = a.split("=", 1)[1].strip() or "generate"
            break
    return (mode or "").lower()

CLI_SSL_MODE = _parse_cli_ssl()

# Env/config
TTS_API_KEY       = os.getenv("TTS_API_KEY", "").strip()
TTS_BIND          = os.getenv("TTS_BIND", "0.0.0.0")
TTS_PORT          = int(os.getenv("TTS_PORT", "8123"))
TTS_REQUIRE_AUTH  = os.getenv("TTS_REQUIRE_AUTH", "0") == "1"
TTS_SESSION_TTL   = int(os.getenv("TTS_SESSION_TTL", "1800"))  # seconds
TTS_ALLOW_PULL    = os.getenv("TTS_ALLOW_PULL", "1") == "1"
TTS_FORCE_LOCAL   = os.getenv("TTS_FORCE_LOCAL", "0") == "1"

# TLS env
TTS_SSL_MODE_ENV  = os.getenv("TTS_SSL", "0").lower()
TTS_SSL_MODE      = (CLI_SSL_MODE or TTS_SSL_MODE_ENV or "0").lower()
TTS_SSL_CERT      = os.getenv("TTS_SSL_CERT", str(TLS_DIR / "cert.pem")).strip()
TTS_SSL_KEY       = os.getenv("TTS_SSL_KEY",  str(TLS_DIR / "key.pem")).strip()
TTS_SSL_SANS      = [h.strip() for h in os.getenv("TTS_SSL_EXTRA_DNS_SANS","").split(",") if h.strip()]
TTS_SSL_REFRESH   = os.getenv("TTS_SSL_REFRESH","0") == "1"

PIPER_BASE_URL    = os.getenv("PIPER_BASE_URL", "https://github.com/rhasspy/piper/releases/download/2023.11.14-2/")
PIPER_EXE_NAME    = os.getenv("PIPER_EXE", "piper")
REL_LINUX_X64     = os.getenv("PIPER_RELEASE_LINUX_X86_64", "piper_linux_x86_64.tar.gz")
REL_LINUX_ARM64   = os.getenv("PIPER_RELEASE_LINUX_ARM64",  "piper_linux_aarch64.tar.gz")
REL_LINUX_ARMV7L  = os.getenv("PIPER_RELEASE_LINUX_ARMV7L", "piper_linux_armv7l.tar.gz")
REL_MAC_X64       = os.getenv("PIPER_RELEASE_MACOS_X64",    "piper_macos_x64.tar.gz")
REL_MAC_ARM64     = os.getenv("PIPER_RELEASE_MACOS_ARM64",  "piper_macos_aarch64.tar.gz")
REL_WIN64         = os.getenv("PIPER_RELEASE_WINDOWS",      "piper_windows_amd64.zip")

DEF_JSON_NAME     = os.getenv("ONNX_JSON_FILENAME",  "glados_piper_medium.onnx.json")
DEF_MODEL_NAME    = os.getenv("ONNX_MODEL_FILENAME", "glados_piper_medium.onnx")
DEF_JSON_URL      = os.getenv("ONNX_JSON_URL",  "")
DEF_MODEL_URL     = os.getenv("ONNX_MODEL_URL", "")

# Logging helpers
CLR = {
    "RESET":"\033[0m","INFO":"\033[94m","SUCCESS":"\033[92m",
    "WARNING":"\033[93m","ERROR":"\033[91m","PROCESS":"\033[96m"
}
def log(msg, cat="INFO"):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    color = CLR.get(cat.upper(),"")
    end = CLR["RESET"] if color else ""
    print(f"{color}[{ts}] {cat}: {msg}{end}", flush=True)

# ─────────────────────────────────────────────────────────────────────────────
# 3) Piper + default voice setup (download if missing)
# ─────────────────────────────────────────────────────────────────────────────
def _run(cmd):
    return subprocess.check_output(cmd, stderr=subprocess.STDOUT, text=True).strip()

def _dl(url: str, dest: Path):
    if shutil.which("wget"):
        subprocess.check_call(["wget", "-O", str(dest), url])
    elif shutil.which("curl"):
        subprocess.check_call(["curl", "-L", "-o", str(dest), url])
    else:
        raise RuntimeError("Need wget or curl for downloads.")

def _ensure_piper():
    piper_exe = PIPER_DIR / PIPER_EXE_NAME
    if piper_exe.exists():
        return piper_exe

    os_name = platform.system()
    arch    = platform.machine().lower()
    if os_name == "Linux":
        if arch in ("x86_64","amd64"):
            rel = REL_LINUX_X64
        elif arch in ("arm64","aarch64"):
            rel = REL_LINUX_ARM64
        else:
            rel = REL_LINUX_ARMV7L
    elif os_name == "Darwin":
        rel = REL_MAC_ARM64 if arch in ("arm64","aarch64") else REL_MAC_X64
    elif os_name == "Windows":
        rel = REL_WIN64
    else:
        raise RuntimeError(f"Unsupported OS: {os_name}")

    url = PIPER_BASE_URL + rel
    archive = SCRIPT_DIR / rel
    log(f"Downloading Piper archive {rel}", "PROCESS")
    _dl(url, archive)

    PIPER_DIR.mkdir(parents=True, exist_ok=True)
    if rel.endswith(".tar.gz"):
        subprocess.check_call(["tar", "-xzvf", str(archive), "-C", str(PIPER_DIR), "--strip-components=1"])
    else:
        subprocess.check_call(["unzip", "-o", str(archive), "-d", str(PIPER_DIR)])
    try:
        archive.unlink(missing_ok=True)
    except Exception:
        pass

    if os_name != "Windows":
        piper_exe.chmod(0o755)
    log("Piper installed.", "SUCCESS")
    return piper_exe

def _ensure_default_voice():
    json_path  = SCRIPT_DIR / DEF_JSON_NAME
    onnx_path  = SCRIPT_DIR / DEF_MODEL_NAME
    if not json_path.exists() and DEF_JSON_URL:
        log(f"Downloading default voice JSON → {DEF_JSON_NAME}", "PROCESS")
        _dl(DEF_JSON_URL, json_path)
    if not onnx_path.exists() and DEF_MODEL_URL:
        log(f"Downloading default voice ONNX → {DEF_MODEL_NAME}", "PROCESS")
        _dl(DEF_MODEL_URL, onnx_path)
    return onnx_path, json_path

def _scan_models():
    models = []
    for p in SCRIPT_DIR.glob("*.onnx"):
        base = p.stem
        j = SCRIPT_DIR / f"{base}.onnx.json"
        if j.exists():
            st = p.stat()
            models.append({"name": base, "onnx": str(p), "json": str(j),
                           "size_bytes": st.st_size, "mtime": st.st_mtime})
    for p in VOICES_DIR.glob("*.onnx"):
        base = p.stem
        j = VOICES_DIR / f"{base}.onnx.json"
        if j.exists():
            st = p.stat()
            models.append({"name": base, "onnx": str(p), "json": str(j),
                           "size_bytes": st.st_size, "mtime": st.st_mtime})
    onnx_path, json_path = _ensure_default_voice()
    if onnx_path.exists() and json_path.exists():
        base = onnx_path.stem
        if not any(m["onnx"] == str(onnx_path) for m in models):
            st = onnx_path.stat()
            models.append({"name": base, "onnx": str(onnx_path), "json": str(json_path),
                           "size_bytes": st.st_size, "mtime": st.st_mtime})
    models.sort(key=lambda m: m["name"])
    return models

def _resolve_model(spec: str|None):
    if not spec:
        onnx_path, json_path = _ensure_default_voice()
        if onnx_path.exists() and json_path.exists():
            return str(onnx_path), str(json_path)
        raise FileNotFoundError("Default ONNX voice not found.")
    p = Path(spec)
    if p.suffix.lower() == ".onnx" and p.exists():
        j = p.with_suffix(".onnx.json")
        candidate = p.parent / (p.stem + ".onnx.json")
        if not j.exists() and candidate.exists():
            j = candidate
        if not j.exists():
            raise FileNotFoundError(f"Missing JSON sidecar for {p.name} (.onnx.json).")
        return str(p), str(j)
    for m in _scan_models():
        if m["name"] == spec:
            return m["onnx"], m["json"]
    raise FileNotFoundError(f"Voice '{spec}' not found. See /models.")

PIPER_EXE = _ensure_piper()
_ensure_default_voice()

# ─────────────────────────────────────────────────────────────────────────────
# 4) HTTP server, auth, sessions, and TTS plumbing
# ─────────────────────────────────────────────────────────────────────────────
app = Flask(__name__)
# Allow CORS broadly (including auth headers) for cross-port HTTPS calls
CORS(app, resources={r"/*": {"origins": "*"}},
     supports_credentials=False,
     expose_headers=["Content-Disposition"],
     allow_headers=["Content-Type", "Authorization", "X-API-Key"])

# simple in-memory session store
_sessions = {}  # {session_key: expiry_datetime}
_sessions_lock = threading.Lock()

def _auth_ok(req) -> bool:
    if not TTS_REQUIRE_AUTH:
        return True
    api = req.headers.get("X-API-Key", "").strip()
    if api and TTS_API_KEY and api == TTS_API_KEY:
        return True
    auth = req.headers.get("Authorization","").strip()
    if auth.lower().startswith("bearer "):
        tok = auth.split(None,1)[1].strip()
        with _sessions_lock:
            exp = _sessions.get(tok)
            if exp and datetime.utcnow() < exp:
                return True
    return False

def _ensure_tools():
    if shutil.which("ffmpeg") is None:
        log("ffmpeg not found on PATH (required for OGG/WAV encode & stream).", "WARNING")
    if shutil.which("aplay") is None and shutil.which("ffplay") is None:
        log("Neither 'aplay' nor 'ffplay' found; /speak mode=play may fail.", "WARNING")

_ensure_tools()

_SENT_END = re.compile(r'([\.!?])')
def _split_text(text: str, max_len: int = 500):
    emoji_pat = re.compile("[" "\U0001F600-\U0001F64F" "\U0001F300-\U0001F5FF" "\U0001F680-\U0001F6FF" "\U0001F1E0-\U0001F1FF" "]+", re.UNICODE)
    clean = emoji_pat.sub('', text).replace("*"," ").strip()
    if not clean:
        return []
    paras = [p.strip() for p in re.split(r'\n\s*\n', clean) if p.strip()]
    chunks = []
    for para in paras:
        parts = re.split(r'(?<=[\.!?])\s+|\n+', para)
        buf = ""
        for part in parts:
            part = part.strip()
            if not part: continue
            if buf and len(buf)+1+len(part) > max_len:
                chunks.append(buf); buf = ""
            if len(part) <= max_len:
                buf = (buf+" "+part).strip() if buf else part
            else:
                if buf:
                    chunks.append(buf); buf = ""
                for i in range(0, len(part), max_len):
                    s = part[i:i+max_len].strip()
                    if s: chunks.append(s)
        if buf: chunks.append(buf)
    return chunks

def _piper_cmd(json_path: str, onnx_path: str, debug=False):
    cmd = [str(PIPER_EXE), "-m", onnx_path, "--json-input", "--output_raw"]
    if debug:
        cmd.insert(3, "--debug")
    payload = json.dumps({"text":"","config":json_path,"model":onnx_path}).encode("utf-8")
    return cmd, payload

def _encode_cmd(fmt="ogg"):
    fmt = (fmt or "ogg").lower()
    if fmt == "ogg":
        return ["ffmpeg","-y","-f","s16le","-ar","22050","-ac","1","-i","pipe:0","-c:a","libopus","-f","ogg","pipe:1"], "audio/ogg"
    if fmt == "wav":
        return ["ffmpeg","-y","-f","s16le","-ar","22050","-ac","1","-i","pipe:0","-f","wav","pipe:1"], "audio/wav"
    if fmt == "raw":
        return None, "audio/L16"
    raise ValueError("format must be one of: ogg, wav, raw")

_play_lock = threading.Lock()

def _play_pcm_stream(raw_iter):
    if shutil.which("aplay"):
        cmd_play = ["aplay","-r","22050","-f","S16_LE","-c","1"]
    elif shutil.which("ffplay"):
        cmd_play = ["ffplay","-autoexit","-nodisp","-f","s16le","-ar","22050","-ac","1","-i","pipe:0"]
    else:
        raise RuntimeError("Need 'aplay' or 'ffplay' to play audio on host.")
    with _play_lock:
        p = subprocess.Popen(cmd_play, stdin=subprocess.PIPE)
        try:
            for chunk in raw_iter:
                if not chunk: break
                p.stdin.write(chunk); p.stdin.flush()
        finally:
            try: p.stdin.close()
            except: pass
            p.wait()

def _generate_pcm_chunks(text: str, json_path: str, onnx_path: str, volume: float = 1.0):
    chunks = _split_text(text, max_len=500)
    if not chunks:
        return
    debug = False
    for c in chunks:
        cmd, _ = _piper_cmd(json_path, onnx_path, debug=debug)
        payload = json.dumps({"text": c, "config": json_path, "model": onnx_path}).encode("utf-8")
        p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=(subprocess.PIPE if debug else subprocess.DEVNULL))
        try:
            p.stdin.write(payload); p.stdin.close()
            while True:
                raw = p.stdout.read(4096)
                if not raw: break
                if volume is not None and abs(volume-1.0) > 1e-3:
                    arr = np.frombuffer(raw, dtype=np.int16).astype(np.float32) * float(volume)
                    raw = np.clip(arr, -32768, 32767).astype(np.int16).tobytes()
                yield raw
        finally:
            p.wait()
            if debug and p.stderr:
                err = p.stderr.read().decode(errors="ignore").strip()
                if err: log(f"[Piper STDERR] {err}", "WARNING")

def _pcm_to_file(pcm_iter, out_path: Path, fmt="ogg"):
    fmt = fmt.lower()
    if fmt == "raw":
        with open(out_path, "wb") as f:
            for b in pcm_iter:
                f.write(b)
        return
    cmd, _ctype = _encode_cmd(fmt)
    p2 = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        for b in pcm_iter:
            p2.stdin.write(b)
        p2.stdin.close()
        with open(out_path, "wb") as f:
            while True:
                o = p2.stdout.read(4096)
                if not o: break
                f.write(o)
    finally:
        try: p2.stdin.close()
        except: pass
        p2.wait()

# ─────────────────────────────────────────────────────────────────────────────
# 5) Routes
# ─────────────────────────────────────────────────────────────────────────────
@app.route("/health", methods=["GET"])
def health():
    try:
        ready = Path(PIPER_EXE).exists()
        models = [m["name"] for m in _scan_models()]
        return jsonify({"status":"ok","piper":"ready" if ready else "missing","voices":models})
    except Exception as e:
        return jsonify({"status":"error","message":str(e)}), 500

@app.route("/models", methods=["GET"])
def models():
    if not _auth_ok(request):
        return jsonify({"error":"unauthorized"}), 401
    try:
        return jsonify({"models": _scan_models()})
    except Exception as e:
        return jsonify({"error":str(e)}), 500

@app.route("/handshake", methods=["POST"])
def handshake():
    try:
        data = request.get_json(force=True, silent=True) or {}
        api_key = str(data.get("api_key","")).strip()
        if not api_key or not TTS_API_KEY or api_key != TTS_API_KEY:
            return jsonify({"error":"invalid api key"}), 401
        key = uuid.uuid4().hex
        exp = datetime.utcnow() + timedelta(seconds=TTS_SESSION_TTL)
        with _sessions_lock:
            _sessions[key] = exp
        return jsonify({"session_key": key, "expires_in": TTS_SESSION_TTL})
    except Exception as e:
        return jsonify({"error":str(e)}), 500

@app.route("/models/pull", methods=["POST"])
def models_pull():
    if not TTS_ALLOW_PULL or not _auth_ok(request):
        return jsonify({"error":"unauthorized"}), 401
    try:
        data = request.get_json(force=True, silent=True) or {}
        name = str(data.get("name","")).strip() or None
        u_onnx = str(data.get("onnx_model_url","")).strip()
        u_json = str(data.get("onnx_json_url","")).strip()
        if not u_onnx or not u_json:
            return jsonify({"error":"onnx_model_url and onnx_json_url required"}), 400
        if not name:
            name = Path(u_onnx).name.replace(".onnx","")
        onnx_path = VOICES_DIR / f"{name}.onnx"
        json_path = VOICES_DIR / f"{name}.onnx.json"
        VOICES_DIR.mkdir(parents=True, exist_ok=True)
        log(f"Pulling voice '{name}'", "PROCESS")
        _dl(u_onnx, onnx_path)
        _dl(u_json, json_path)
        return jsonify({"ok":True, "name":name, "onnx":str(onnx_path), "json":str(json_path)})
    except Exception as e:
        return jsonify({"error":str(e)}), 500

@app.route("/files/<path:filename>", methods=["GET"])
def files(filename):
    if not _auth_ok(request):
        return jsonify({"error":"unauthorized"}), 401
    try:
        return send_from_directory(str(OUT_DIR), filename, as_attachment=True)
    except Exception as e:
        return jsonify({"error":str(e)}), 404

@app.route("/speak", methods=["POST"])
def speak():
    if not _auth_ok(request):
        return jsonify({"error":"unauthorized"}), 401
    try:
        data = request.get_json(force=True, silent=True) or {}
        text   = (data.get("text") or "").strip()
        if not text:
            return jsonify({"error":"text is required"}), 400
        model  = (data.get("model") or "").strip() or None
        mode   = (data.get("mode")  or "file").strip().lower()
        fmt    = (data.get("format") or "ogg").strip().lower()
        volume = float(data.get("volume", 1.0))
        split  = (data.get("split") or "auto").strip().lower()

        onnx_path, json_path = _resolve_model(model)

        if split == "none":
            def one_chunk():
                cmd, _ = _piper_cmd(json_path, onnx_path, debug=False)
                payload = json.dumps({"text": text, "config": json_path, "model": onnx_path}).encode("utf-8")
                p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                p.stdin.write(payload); p.stdin.close()
                while True:
                    raw = p.stdout.read(4096)
                    if not raw: break
                    if volume is not None and abs(volume-1.0) > 1e-3:
                        arr = np.frombuffer(raw, dtype=np.int16).astype(np.float32) * volume
                        raw = np.clip(arr, -32768, 32767).astype(np.int16).tobytes()
                    yield raw
                p.wait()
            pcm_iter = one_chunk()
            chunk_count = 1
        else:
            pcm_iter = _generate_pcm_chunks(text, json_path, onnx_path, volume=volume)
            chunk_count = len(_split_text(text, max_len=500))

        if mode == "file":
            OUT_DIR.mkdir(parents=True, exist_ok=True)
            ext = {"ogg":".ogg","wav":".wav","raw":".raw"}.get(fmt, ".ogg")
            fname = f"{uuid.uuid4().hex}{ext}"
            out_path = OUT_DIR / fname
            _pcm_to_file(pcm_iter, out_path, fmt=fmt)
            url = f"/files/{fname}"
            return jsonify({"ok":True,"files":[{"filename":fname,"url":url,"path":str(out_path)}], "chunks":chunk_count})

        elif mode == "play":
            _play_pcm_stream(pcm_iter)
            return jsonify({"ok":True,"played_chunks":chunk_count})

        elif mode == "stream":
            enc_cmd, ctype = _encode_cmd(fmt)
            if enc_cmd is None:
                def gen_raw():
                    try:
                        for b in pcm_iter:
                            if not b: break
                            yield b
                    except GeneratorExit:
                        return
                return Response(gen_raw(), mimetype="audio/L16")
            def gen_encoded():
                p = subprocess.Popen(enc_cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                try:
                    def feeder():
                        try:
                            for b in pcm_iter:
                                if not b: break
                                p.stdin.write(b)
                        except Exception:
                            pass
                        finally:
                            try: p.stdin.close()
                            except: pass
                    t = threading.Thread(target=feeder, daemon=True)
                    t.start()
                    while True:
                        o = p.stdout.read(4096)
                        if not o: break
                        yield o
                    t.join()
                except GeneratorExit:
                    try: p.kill()
                    except: pass
                    return
                finally:
                    try: p.wait(timeout=2)
                    except: pass
            return Response(gen_encoded(), mimetype=ctype)
        else:
            return jsonify({"error":"mode must be one of: file, play, stream"}), 400

    except Exception as e:
        log(f"/speak error: {e}", "ERROR")
        return jsonify({"error":str(e)}), 500

# ─────────────────────────────────────────────────────────────────────────────
# 6) TLS helpers + Serve (TLS optional + LAN FORCE + PORT AUTO + GRACEFUL)
# ─────────────────────────────────────────────────────────────────────────────
from werkzeug.serving import make_server, generate_adhoc_ssl_context
import atexit, signal

def _list_local_ips():
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ips.add(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    try:
        host = socket.gethostname()
        for info in socket.getaddrinfo(host, None, socket.AF_INET, socket.SOCK_DGRAM):
            ips.add(info[4][0])
    except Exception:
        pass
    return sorted(i for i in ips if not i.startswith("127."))

def _get_all_sans():
    sans_dns = set(["localhost"])
    sans_ip  = set(["127.0.0.1"])
    for ip in _list_local_ips():
        sans_ip.add(ip)
    for h in TTS_SSL_SANS:
        sans_dns.add(h)
    return sorted(sans_dns), sorted(sans_ip)

def _generate_self_signed(cert_file: Path, key_file: Path):
    # Generate a self-signed cert with SANs including local IPs/localhost
    from cryptography.hazmat.primitives import serialization, hashes
    from cryptography.hazmat.primitives.asymmetric import rsa
    import cryptography.x509 as x509
    from cryptography.x509 import NameOID, SubjectAlternativeName, DNSName, IPAddress
    import ipaddress as ipa

    keyobj = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    dns_sans, ip_sans = _get_all_sans()
    san_list = [DNSName(d) for d in dns_sans]
    for ip in ip_sans:
        try:
            san_list.append(IPAddress(ipa.ip_address(ip)))
        except ValueError:
            pass
    san = SubjectAlternativeName(san_list)

    cn = (ip_sans[0] if ip_sans else "localhost")
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    not_before = datetime.now(timezone.utc) - timedelta(minutes=5)
    not_after  = not_before + timedelta(days=365)

    cert = (
        x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(keyobj.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(not_before)
            .not_valid_after(not_after)
            .add_extension(san, critical=False)
            .sign(keyobj, hashes.SHA256())
    )

    cert_file.parent.mkdir(parents=True, exist_ok=True)
    with open(key_file, "wb") as f:
        f.write(keyobj.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption()
        ))
    with open(cert_file, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    log(f"Generated self-signed TLS cert: {cert_file}", "SUCCESS")

# --- add this helper (after _get_all_sans) ---
def _mkcert_generate(cert_file: Path, key_file: Path):
    """
    Generate a cert with mkcert that's trusted by the local OS trust store.
    Requires mkcert installed and its root CA installed (mkcert -install).
    """
    if shutil.which("mkcert") is None:
        raise RuntimeError("mkcert not found on PATH")

    # ensure the local CA is present (may prompt for password on macOS/Windows)
    try:
        subprocess.check_call(["mkcert", "-install"])
    except Exception as e:
        log(f"mkcert -install warning: {e}", "WARNING")

    dns_sans, ip_sans = _get_all_sans()
    names = dns_sans + ip_sans  # mkcert accepts both hostnames and IPs

    cert_file.parent.mkdir(parents=True, exist_ok=True)
    # mkcert will create both when using -cert-file / -key-file
    cmd = ["mkcert", "-cert-file", str(cert_file), "-key-file", str(key_file)] + names
    log(f"Generating mkcert certificate for: {', '.join(names)}", "PROCESS")
    subprocess.check_call(cmd)
    log(f"mkcert certificate written to {cert_file}", "SUCCESS")


# --- replace your _build_ssl_context() with this version ---
def _build_ssl_context():
    """
    Build an SSL context according to TTS_SSL mode.
      - "0"/off: HTTP only
      - "adhoc": ephemeral dev cert (may still warn)
      - "mkcert": generate a locally trusted cert via mkcert
      - "1": use provided TTS_SSL_CERT/TTS_SSL_KEY
      - "generate": self-signed with SANs (will warn until user trusts it)
    """
    mode = TTS_SSL_MODE

    # HTTP only
    if mode in ("0", "off", "false", ""):
        return None, "http"

    # Werkzeug adhoc (quick dev, still usually untrusted)
    if mode == "adhoc":
        try:
            return generate_adhoc_ssl_context(), "https"
        except Exception as e:
            log(f"Adhoc SSL failed: {e}", "ERROR")
            return None, "http"

    # mkcert (best local DX)
    if mode == "mkcert":
        cert_p = Path(TTS_SSL_CERT or (TLS_DIR / "cert.pem"))
        key_p  = Path(TTS_SSL_KEY  or (TLS_DIR / "key.pem"))
        try:
            if TTS_SSL_REFRESH or (not cert_p.exists() or not key_p.exists()):
                _mkcert_generate(cert_p, key_p)
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(certfile=str(cert_p), keyfile=str(key_p))
            return ctx, "https"
        except Exception as e:
            log(f"mkcert mode failed ({e}); falling back to self-signed.", "WARNING")
            mode = "generate"  # fall through

    # Use provided cert/key (production or your own CA)
    if mode in ("1", "true", "yes", "on"):
        cert_p = Path(TTS_SSL_CERT)
        key_p  = Path(TTS_SSL_KEY)
        if cert_p.exists() and key_p.exists():
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(certfile=str(cert_p), keyfile=str(key_p))
            return ctx, "https"
        else:
            log("TTS_SSL=1 but cert/key not found; generating self-signed instead.", "WARNING")
            mode = "generate"

    # Self-signed (with IP/DNS SANs)
    if mode in ("generate", "gen", "selfsigned"):
        cert_p = Path(TTS_SSL_CERT or (TLS_DIR / "cert.pem"))
        key_p  = Path(TTS_SSL_KEY  or (TLS_DIR / "key.pem"))
        if TTS_SSL_REFRESH or (not cert_p.exists() or not key_p.exists()):
            _generate_self_signed(cert_p, key_p)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile=str(cert_p), keyfile=str(key_p))
        return ctx, "https"

    # Fallback: try provided paths anyway
    try:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile=TTS_SSL_CERT, keyfile=TTS_SSL_KEY)
        return ctx, "https"
    except Exception as e:
        log(f"TLS config error ({mode}): {e}. Serving over HTTP.", "WARNING")
        return None, "http"


def _print_startup(actual_port: int, scheme: str):
    log(f"TTS service listening on {scheme}://{TTS_BIND}:{actual_port}", "SUCCESS")
    auth_note = "ON" if TTS_REQUIRE_AUTH else "OFF"
    log(f"Auth required: {auth_note}", "INFO")
    if TTS_REQUIRE_AUTH:
        log("Provide X-API-Key or Bearer session_key (via /handshake).", "INFO")
    try_host = "localhost" if TTS_BIND == "0.0.0.0" else TTS_BIND
    curl_flag = "-k " if scheme == "https" else ""
    log(f"Try:  curl {curl_flag}-s {scheme}://{try_host}:{actual_port}/health | jq", "INFO")
    if TTS_BIND == "0.0.0.0":
        for ip in _list_local_ips():
            log(f"LAN URL: {scheme}://{ip}:{actual_port}/health", "INFO")

def _port_is_free(host: str, port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind((host, port))
        s.close()
        return True
    except OSError:
        try: s.close()
        except: pass
        return False

def _find_free_port(host: str, preferred: int, tries: int = 100) -> int:
    for p in range(preferred, preferred + tries + 1):
        if _port_is_free(host, p):
            return p
    raise RuntimeError(f"No free port in range {preferred}..{preferred+tries}")

class _ServerThread(threading.Thread):
    def __init__(self, app, host, port, ssl_context=None):
        super().__init__(daemon=True)
        self._srv = make_server(host, port, app, ssl_context=ssl_context)
        self.port = port
    def run(self):
        self._srv.serve_forever()
    def shutdown(self):
        try:
            self._srv.shutdown()
        except Exception:
            pass

_server_thread = None

def _start_server():
    global _server_thread, TTS_BIND
    if TTS_BIND in ("127.0.0.1", "localhost", "::1") and not TTS_FORCE_LOCAL:
        log("TTS_BIND was localhost; switching to 0.0.0.0 for LAN access. "
            "Set TTS_FORCE_LOCAL=1 to keep localhost-only.", "WARNING")
        TTS_BIND = "0.0.0.0"

    ssl_ctx, scheme = _build_ssl_context()

    try:
        actual_port = _find_free_port(TTS_BIND, TTS_PORT, tries=100)
    except Exception as e:
        log(f"Port selection failed: {e}", "ERROR")
        raise

    _server_thread = _ServerThread(app, TTS_BIND, actual_port, ssl_context=ssl_ctx)
    _server_thread.start()
    _print_startup(actual_port, scheme)
    return actual_port

def _graceful_exit(signum=None, frame=None):
    signame = {
        signal.SIGINT:  "SIGINT",
        getattr(signal, "SIGTSTP", 20): "SIGTSTP",
        signal.SIGTERM: "SIGTERM"
    }.get(signum, str(signum))
    log(f"Received {signame} — shutting down server…", "INFO")
    try:
        if _server_thread:
            _server_thread.shutdown()
    except Exception as e:
        log(f"Shutdown error: {e}", "WARNING")
    os._exit(0)

atexit.register(_graceful_exit)
signal.signal(signal.SIGINT,  _graceful_exit)
signal.signal(signal.SIGTERM, _graceful_exit)
if hasattr(signal, "SIGTSTP"):
    signal.signal(signal.SIGTSTP, _graceful_exit)

if __name__ == "__main__":
    _start_server()
    try:
        while True:
            try:
                signal.pause()
            except AttributeError:
                time.sleep(3600)
    except KeyboardInterrupt:
        _graceful_exit(signal.SIGINT, None)
