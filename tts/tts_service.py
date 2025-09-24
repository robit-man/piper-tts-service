#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TTS Service (Piper) — single-file local network API
Upgrades:
- Concurrency guard held for full synthesis lifetime, including streaming.
- Single Piper subprocess per request (no per-chunk process thrash).
- Optional per-IP rate limiting and queue timeout with 503/Retry-After.
- Periodic output cleanup (TTL) to avoid disk fill.
- /metrics endpoint for basic load signals.
- Exported `app` for gunicorn; built-in server retained for convenience.
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
        print("ERROR: Python 3.9+ required.", file=sys.stderr); sys.exit(1)
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

# ─────────────────────────────────────────────────────────────────────────────
# 1) First-run pip deps, .env, folders, Piper + default voice setup
# ─────────────────────────────────────────────────────────────────────────────
SETUP_MARKER = Path(".tts_setup_complete")
SCRIPT_DIR   = Path(__file__).resolve().parent
OUT_DIR      = SCRIPT_DIR / "tts_out"
VOICES_DIR   = SCRIPT_DIR / "voices"
PIPER_DIR    = SCRIPT_DIR / "piper"
TLS_DIR      = SCRIPT_DIR / "tls"

def _pip(*pkgs):
    subprocess.check_call([sys.executable, "-m", "pip", "install", *pkgs])

if not SETUP_MARKER.exists():
    print("[PROCESS] Installing Python dependencies…")
    _pip("--upgrade", "pip")
    _pip("flask", "flask-cors", "numpy", "python-dotenv", "cryptography", "waitress")
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
            "TTS_MAX_CONCURRENCY=4\n"
            "TTS_QUEUE_TIMEOUT_S=2.0\n"
            "TTS_RATE_LIMIT_RPS=10\n"
            "TTS_RATE_LIMIT_BURST=20\n"
            "TTS_FILE_TTL_S=86400\n"
            "\n"
            "# HTTPS options: 0|1|generate|adhoc|mkcert\n"
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
from flask import Flask, request, send_from_directory, Response, jsonify, g
from flask_cors import CORS
import numpy as np
from dotenv import load_dotenv

load_dotenv(SCRIPT_DIR / ".env")

def _parse_cli_ssl():
    mode = None
    for a in sys.argv[1:]:
        if a == "--ssl": mode = "generate"; break
        if a.startswith("--ssl="): mode = a.split("=",1)[1].strip() or "generate"; break
    return (mode or "").lower()

CLI_SSL_MODE = _parse_cli_ssl()

# Env/config
TTS_API_KEY       = os.getenv("TTS_API_KEY", "").strip()
TTS_BIND          = os.getenv("TTS_BIND", "0.0.0.0")
TTS_PORT          = int(os.getenv("TTS_PORT", "8123"))
TTS_REQUIRE_AUTH  = os.getenv("TTS_REQUIRE_AUTH", "0") == "1"
TTS_SESSION_TTL   = int(os.getenv("TTS_SESSION_TTL", "1800"))
TTS_ALLOW_PULL    = os.getenv("TTS_ALLOW_PULL", "1") == "1"
TTS_FORCE_LOCAL   = os.getenv("TTS_FORCE_LOCAL", "0") == "1"
TTS_MAX_CONCURRENCY = max(1, int(os.getenv("TTS_MAX_CONCURRENCY", "4")))
TTS_QUEUE_TIMEOUT_S = float(os.getenv("TTS_QUEUE_TIMEOUT_S", "2.0"))
TTS_RATE_LIMIT_RPS  = max(1, int(os.getenv("TTS_RATE_LIMIT_RPS", "10")))
TTS_RATE_LIMIT_BURST= max(1, int(os.getenv("TTS_RATE_LIMIT_BURST", "20")))
TTS_FILE_TTL_S      = int(os.getenv("TTS_FILE_TTL_S", "86400"))

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
CLR = {"RESET":"\033[0m","INFO":"\033[94m","SUCCESS":"\033[92m","WARNING":"\033[93m","ERROR":"\033[91m","PROCESS":"\033[96m"}
def log(msg, cat="INFO"):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    color = CLR.get(cat.upper(),""); end = CLR["RESET"] if color else ""
    print(f"{color}[{ts}] {cat}: {msg}{end}", flush=True)

# ─────────────────────────────────────────────────────────────────────────────
# 3) Piper + default voice setup
# ─────────────────────────────────────────────────────────────────────────────
def _dl(url: str, dest: Path):
    if shutil.which("wget"): subprocess.check_call(["wget", "-O", str(dest), url])
    elif shutil.which("curl"): subprocess.check_call(["curl", "-L", "-o", str(dest), url])
    else: raise RuntimeError("Need wget or curl for downloads.")

def _ensure_piper():
    piper_exe = PIPER_DIR / PIPER_EXE_NAME
    if piper_exe.exists(): return piper_exe
    os_name = platform.system(); arch = platform.machine().lower()
    if os_name == "Linux":
        rel = REL_LINUX_X64 if arch in ("x86_64","amd64") else (REL_LINUX_ARM64 if arch in ("arm64","aarch64") else REL_LINUX_ARMV7L)
    elif os_name == "Darwin":
        rel = REL_MAC_ARM64 if arch in ("arm64","aarch64") else REL_MAC_X64
    elif os_name == "Windows":
        rel = REL_WIN64
    else:
        raise RuntimeError(f"Unsupported OS: {os_name}")
    url = PIPER_BASE_URL + rel; archive = SCRIPT_DIR / rel
    log(f"Downloading Piper archive {rel}", "PROCESS"); _dl(url, archive)
    PIPER_DIR.mkdir(parents=True, exist_ok=True)
    if rel.endswith(".tar.gz"): subprocess.check_call(["tar","-xzvf", str(archive), "-C", str(PIPER_DIR), "--strip-components=1"])
    else: subprocess.check_call(["unzip","-o", str(archive), "-d", str(PIPER_DIR)])
    try: archive.unlink(missing_ok=True)
    except: pass
    if os_name != "Windows": (PIPER_DIR / PIPER_EXE_NAME).chmod(0o755)
    log("Piper installed.", "SUCCESS")
    return PIPER_DIR / PIPER_EXE_NAME

def _ensure_default_voice():
    json_path  = SCRIPT_DIR / DEF_JSON_NAME
    onnx_path  = SCRIPT_DIR / DEF_MODEL_NAME
    if not json_path.exists() and DEF_JSON_URL: _dl(DEF_JSON_URL, json_path)
    if not onnx_path.exists() and DEF_MODEL_URL: _dl(DEF_MODEL_URL, onnx_path)
    return onnx_path, json_path

def _scan_models():
    models = []
    for p in SCRIPT_DIR.glob("*.onnx"):
        j = SCRIPT_DIR / f"{p.stem}.onnx.json"
        if j.exists():
            st = p.stat()
            models.append({"name": p.stem, "onnx": str(p), "json": str(j), "size_bytes": st.st_size, "mtime": st.st_mtime})
    for p in VOICES_DIR.glob("*.onnx"):
        j = VOICES_DIR / f"{p.stem}.onnx.json"
        if j.exists():
            st = p.stat()
            models.append({"name": p.stem, "onnx": str(p), "json": str(j), "size_bytes": st.st_size, "mtime": st.st_mtime})
    onnx_path, json_path = _ensure_default_voice()
    if onnx_path.exists() and json_path.exists():
        if not any(m["onnx"] == str(onnx_path) for m in models):
            st = onnx_path.stat()
            models.append({"name": onnx_path.stem, "onnx": str(onnx_path), "json": str(json_path), "size_bytes": st.st_size, "mtime": st.st_mtime})
    models.sort(key=lambda m: m["name"]); return models

def _resolve_model(spec: str|None):
    if not spec:
        onnx_path, json_path = _ensure_default_voice()
        if onnx_path.exists() and json_path.exists(): return str(onnx_path), str(json_path)
        raise FileNotFoundError("Default ONNX voice not found.")
    p = Path(spec)
    if p.suffix.lower() == ".onnx" and p.exists():
        j = p.with_suffix(".onnx.json"); candidate = p.parent / (p.stem + ".onnx.json")
        if not j.exists() and candidate.exists(): j = candidate
        if not j.exists(): raise FileNotFoundError(f"Missing JSON sidecar for {p.name} (.onnx.json).")
        return str(p), str(j)
    for m in _scan_models():
        if m["name"] == spec: return m["onnx"], m["json"]
    raise FileNotFoundError(f"Voice '{spec}' not found. See /models.")

PIPER_EXE = _ensure_piper()
_ensure_default_voice()

# ─────────────────────────────────────────────────────────────────────────────
# 4) HTTP server, auth, sessions, rate limit, and TTS plumbing
# ─────────────────────────────────────────────────────────────────────────────
app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}},
     supports_credentials=False,
     expose_headers=["Content-Disposition"],
     allow_headers=["Content-Type", "Authorization", "X-API-Key"])

_sessions = {}
_sessions_lock = threading.Lock()

def _auth_ok(req) -> bool:
    if not TTS_REQUIRE_AUTH: return True
    api = req.headers.get("X-API-Key","").strip()
    if api and TTS_API_KEY and api == TTS_API_KEY: return True
    auth = req.headers.get("Authorization","").strip()
    if auth.lower().startswith("bearer "):
        tok = auth.split(None,1)[1].strip()
        with _sessions_lock:
            exp = _sessions.get(tok)
            if exp and datetime.utcnow() < exp: return True
    return False

def _ensure_tools():
    if shutil.which("ffmpeg") is None:
        log("ffmpeg not found on PATH (required for OGG/WAV encode & stream).", "WARNING")
    if shutil.which("aplay") is None and shutil.which("ffplay") is None:
        log("Neither 'aplay' nor 'ffplay' found; /speak mode=play may fail.", "WARNING")
_ensure_tools()

# -------- Concurrency guard (prevents system overload) --------
_CONC_SEM = threading.Semaphore(TTS_MAX_CONCURRENCY)

class _slot:
    def __init__(self, timeout: float | None = None):
        self.timeout = float(TTS_QUEUE_TIMEOUT_S if timeout is None else timeout)
        self.acquired = False
    def __enter__(self):
        self.acquired = _CONC_SEM.acquire(timeout=self.timeout)
        if not self.acquired:
            raise TimeoutError("tts at capacity")
    def __exit__(self, exc_type, exc, tb):
        if self.acquired:
            try: _CONC_SEM.release()
            except: pass

# Per-IP token bucket
class _Bucket:
    __slots__=("ts","tokens")
    def __init__(self): self.ts=time.time(); self.tokens=float(TTS_RATE_LIMIT_BURST)
_rlock = threading.Lock()
_buckets: dict[str,_Bucket] = {}

@app.before_request
def _before_req():
    ip = request.headers.get("X-Forwarded-For","").split(",")[0].strip() or request.remote_addr or "0.0.0.0"
    now = time.time()
    with _rlock:
        b = _buckets.get(ip)
        if b is None:
            b=_Bucket(); _buckets[ip]=b
        dt = max(0.0, now - b.ts); b.ts = now
        b.tokens = min(float(TTS_RATE_LIMIT_BURST), b.tokens + dt*TTS_RATE_LIMIT_RPS)
        if b.tokens < 1.0:
            return jsonify({"error":"rate limit"}), 429, {"Retry-After":"1"}
        b.tokens -= 1.0
    g.client_ip = ip

# -------- Text splitting (optional; not used for multiple Piper processes) --------
_SENT_END = re.compile(r'([\.!?])')
def _split_text(text: str, max_len: int = 1500):
    emoji_pat = re.compile("[" "\U0001F600-\U0001F64F" "\U0001F300-\U0001F5FF" "\U0001F680-\U0001F6FF" "\U0001F1E0-\U0001F1FF" "]+", re.UNICODE)
    clean = emoji_pat.sub('', text).replace("*"," ").strip()
    if not clean: return []
    # keep large chunks to favor single Piper invocation
    parts = re.split(r'\n\s*\n', clean)
    chunks = []
    for para in parts:
        para = para.strip()
        if not para: continue
        if len(para) <= max_len:
            chunks.append(para)
        else:
            for i in range(0, len(para), max_len):
                s = para[i:i+max_len].strip()
                if s: chunks.append(s)
    return chunks

def _piper_cmd(json_path: str, onnx_path: str, debug=False):
    cmd = [str(PIPER_EXE), "-m", onnx_path, "--json-input", "--output_raw"]
    if debug: cmd.insert(3, "--debug")
    return cmd

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
        p = subprocess.Popen(cmd_play, stdin=subprocess.PIPE, bufsize=0)
        try:
            for chunk in raw_iter:
                if not chunk: break
                p.stdin.write(chunk); p.stdin.flush()
        finally:
            try: p.stdin.close()
            except: pass
            p.wait()

def _synthesize_one(text: str, json_path: str, onnx_path: str, volume: float = 1.0):
    """Yield raw PCM16 bytes for the whole request using a single Piper process."""
    text = (text or "").strip()
    if not text:
        return
    # Optionally split for very large inputs but reuse one process if possible.
    payload_text = "\n\n".join(_split_text(text, max_len=1500)) or text
    payload = json.dumps({"text": payload_text, "config": json_path, "model": onnx_path}).encode("utf-8")
    cmd = _piper_cmd(json_path, onnx_path, debug=False)
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
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
        try: p.wait(timeout=5)
        except: 
            try: p.kill()
            except: pass

def _pcm_to_file(pcm_iter, out_path: Path, fmt="ogg"):
    """Encode PCM to a file without deadlocking."""
    fmt = (fmt or "ogg").lower()
    if fmt == "raw":
        with open(out_path, "wb") as f:
            for b in pcm_iter:
                if b: f.write(b)
        return
    cmd, _ctype = _encode_cmd(fmt)
    p2 = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)

    def feeder():
        try:
            for b in pcm_iter:
                if not b: continue
                if p2.stdin:
                    p2.stdin.write(b)
        except Exception:
            pass
        finally:
            try:
                if p2.stdin: p2.stdin.close()
            except: pass

    t = threading.Thread(target=feeder, daemon=True); t.start()
    try:
        with open(out_path, "wb") as f:
            while True:
                o = p2.stdout.read(4096)
                if not o: break
                f.write(o)
    finally:
        t.join(timeout=10)
        try: p2.wait(timeout=5)
        except: 
            try: p2.kill()
            except: pass

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

@app.route("/metrics", methods=["GET"])
def metrics():
    # Expose approximate queue depth and concurrency availability
    try:
        # Python Semaphore has no official way to inspect; approximate via non-blocking acquire.
        free = 0
        probes = 0
        while _CONC_SEM.acquire(blocking=False):
            free += 1; probes += 1
        for _ in range(probes):
            _CONC_SEM.release()
        return jsonify({"ok":True, "concurrency_free": free, "concurrency_max": TTS_MAX_CONCURRENCY})
    except Exception as e:
        return jsonify({"ok":False, "error":str(e)}), 500

@app.route("/models", methods=["GET"])
def models():
    if not _auth_ok(request): return jsonify({"error":"unauthorized"}), 401
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
    if not TTS_ALLOW_PULL or not _auth_ok(request): return jsonify({"error":"unauthorized"}), 401
    try:
        data = request.get_json(force=True, silent=True) or {}
        name = (str(data.get("name","")).strip() or None)
        u_onnx = str(data.get("onnx_model_url","")).strip()
        u_json = str(data.get("onnx_json_url","")).strip()
        if not u_onnx or not u_json:
            return jsonify({"error":"onnx_model_url and onnx_json_url required"}), 400
        if not name: name = Path(u_onnx).name.replace(".onnx","")
        onnx_path = VOICES_DIR / f"{name}.onnx"
        json_path = VOICES_DIR / f"{name}.onnx.json"
        VOICES_DIR.mkdir(parents=True, exist_ok=True)
        log(f"Pulling voice '{name}'", "PROCESS")
        _dl(u_onnx, onnx_path); _dl(u_json, json_path)
        return jsonify({"ok":True, "name":name, "onnx":str(onnx_path), "json":str(json_path)})
    except Exception as e:
        return jsonify({"error":str(e)}), 500

@app.route("/files/<path:filename>", methods=["GET"])
def files(filename):
    if not _auth_ok(request): return jsonify({"error":"unauthorized"}), 401
    try:
        return send_from_directory(str(OUT_DIR), filename, as_attachment=True)
    except Exception as e:
        return jsonify({"error":str(e)}), 404

def _stream_with_slot_encoded(pcm_iter, fmt):
    enc_cmd, ctype = _encode_cmd(fmt)
    if enc_cmd is None:
        def gen_raw():
            with _slot():
                try:
                    for b in pcm_iter:
                        if not b: break
                        yield b
                except GeneratorExit:
                    return
        return Response(gen_raw(), mimetype="audio/L16")
    def gen_encoded():
        with _slot():
            p = subprocess.Popen(enc_cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
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
                t = threading.Thread(target=feeder, daemon=True); t.start()
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

@app.route("/speak", methods=["POST"])
def speak():
    if not _auth_ok(request): return jsonify({"error":"unauthorized"}), 401
    try:
        data = request.get_json(force=True, silent=True) or {}
        text   = (data.get("text") or "").strip()
        if not text: return jsonify({"error":"text is required"}), 400
        model  = (data.get("model") or "").strip() or None
        mode   = (data.get("mode")  or "file").strip().lower()
        fmt    = (data.get("format") or "ogg").strip().lower()
        volume = float(data.get("volume", 1.0))
        onnx_path, json_path = _resolve_model(model)

        pcm_iter = _synthesize_one(text, json_path, onnx_path, volume=volume)

        if mode == "file":
            OUT_DIR.mkdir(parents=True, exist_ok=True)
            ext = {"ogg":".ogg","wav":".wav","raw":".raw"}.get(fmt, ".ogg")
            fname = f"{uuid.uuid4().hex}{ext}"
            out_path = OUT_DIR / fname
            with _slot():  # hold slot until encode to file completes
                _pcm_to_file(pcm_iter, out_path, fmt=fmt)
            url = f"/files/{fname}"
            return jsonify({"ok":True,"files":[{"filename":fname,"url":url,"path":str(out_path)}]})

        elif mode == "play":
            with _slot():
                _play_pcm_stream(pcm_iter)
            return jsonify({"ok":True})

        elif mode == "stream":
            return _stream_with_slot_encoded(pcm_iter, fmt)

        else:
            return jsonify({"error":"mode must be one of: file, play, stream"}), 400

    except TimeoutError:
        return jsonify({"error":"capacity"}), 503, {"Retry-After":"2"}
    except Exception as e:
        log(f"/speak error: {e}", "ERROR")
        return jsonify({"error":str(e)}), 500

# ─────────────────────────────────────────────────────────────────────────────
# 6) TTL cleanup, TLS helpers + Serve (TLS optional)
# ─────────────────────────────────────────────────────────────────────────────
def _cleanup_loop():
    while True:
        try:
            cutoff = time.time() - float(TTS_FILE_TTL_S)
            for p in OUT_DIR.glob("*"):
                try:
                    if p.is_file() and p.stat().st_mtime < cutoff:
                        p.unlink()
                except Exception:
                    pass
        except Exception:
            pass
        time.sleep(600)  # every 10 min

threading.Thread(target=_cleanup_loop, daemon=True, name="tts-cleanup").start()

from werkzeug.serving import make_server, generate_adhoc_ssl_context
import atexit, signal

def _list_local_ips():
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.connect(("8.8.8.8", 80)); ips.add(s.getsockname()[0]); s.close()
    except Exception: pass
    try:
        host = socket.gethostname()
        for info in socket.getaddrinfo(host, None, socket.AF_INET, socket.SOCK_DGRAM):
            ips.add(info[4][0])
    except Exception: pass
    return sorted(i for i in ips if not i.startswith("127."))

def _get_all_sans():
    sans_dns = set(["localhost"]); sans_ip = set(["127.0.0.1"])
    for ip in _list_local_ips(): sans_ip.add(ip)
    for h in TTS_SSL_SANS: sans_dns.add(h)
    return sorted(sans_dns), sorted(sans_ip)

def _generate_self_signed(cert_file: Path, key_file: Path):
    from cryptography.hazmat.primitives import serialization, hashes
    from cryptography.hazmat.primitives.asymmetric import rsa
    import cryptography.x509 as x509
    from cryptography.x509 import NameOID, SubjectAlternativeName, DNSName, IPAddress
    import ipaddress as ipa
    keyobj = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    dns_sans, ip_sans = _get_all_sans()
    san_list = [DNSName(d) for d in dns_sans]
    for ip in ip_sans:
        try: san_list.append(IPAddress(ipa.ip_address(ip)))
        except ValueError: pass
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

def _mkcert_generate(cert_file: Path, key_file: Path):
    if shutil.which("mkcert") is None: raise RuntimeError("mkcert not found on PATH")
    try: subprocess.check_call(["mkcert", "-install"])
    except Exception as e: log(f"mkcert -install warning: {e}", "WARNING")
    dns_sans, ip_sans = _get_all_sans(); names = dns_sans + ip_sans
    cert_file.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["mkcert", "-cert-file", str(cert_file), "-key-file", str(key_file)] + names
    log(f"Generating mkcert certificate for: {', '.join(names)}", "PROCESS")
    subprocess.check_call(cmd)
    log(f"mkcert certificate written to {cert_file}", "SUCCESS")

def _build_ssl_context():
    mode = TTS_SSL_MODE
    if mode in ("0","off","false",""): return None, "http"
    if mode == "adhoc":
        try: return generate_adhoc_ssl_context(), "https"
        except Exception as e: log(f"Adhoc SSL failed: {e}", "ERROR"); return None, "http"
    if mode == "mkcert":
        cert_p = Path(TTS_SSL_CERT or (TLS_DIR / "cert.pem")); key_p = Path(TTS_SSL_KEY or (TLS_DIR / "key.pem"))
        try:
            if TTS_SSL_REFRESH or (not cert_p.exists() or not key_p.exists()): _mkcert_generate(cert_p, key_p)
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); ctx.load_cert_chain(certfile=str(cert_p), keyfile=str(key_p))
            return ctx, "https"
        except Exception as e:
            log(f"mkcert mode failed ({e}); falling back to self-signed.", "WARNING"); mode = "generate"
    if mode in ("1","true","yes","on"):
        cert_p = Path(TTS_SSL_CERT); key_p  = Path(TTS_SSL_KEY)
        if cert_p.exists() and key_p.exists():
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); ctx.load_cert_chain(certfile=str(cert_p), keyfile=str(key_p))
            return ctx, "https"
        else:
            log("TTS_SSL=1 but cert/key not found; generating self-signed instead.", "WARNING"); mode = "generate"
    if mode in ("generate","gen","selfsigned"):
        cert_p = Path(TTS_SSL_CERT or (TLS_DIR / "cert.pem")); key_p  = Path(TTS_SSL_KEY or (TLS_DIR / "key.pem"))
        if TTS_SSL_REFRESH or (not cert_p.exists() or not key_p.exists()): _generate_self_signed(cert_p, key_p)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); ctx.load_cert_chain(certfile=str(cert_p), keyfile=str(key_p))
        return ctx, "https"
    try:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); ctx.load_cert_chain(certfile=TTS_SSL_CERT, keyfile=TTS_SSL_KEY)
        return ctx, "https"
    except Exception as e:
        log(f"TLS config error ({mode}): {e}. Serving over HTTP.", "WARNING"); return None, "http"

from werkzeug.serving import BaseWSGIServer
class _ServerThread(threading.Thread):
    def __init__(self, app, host, port, ssl_context=None):
        super().__init__(daemon=True)
        try:
            self._srv: BaseWSGIServer = make_server(host, port, app, threaded=True, ssl_context=ssl_context)  # type: ignore
            log("Werkzeug: threaded server enabled", "INFO")
        except TypeError:
            self._srv = make_server(host, port, app, ssl_context=ssl_context)
            log("Werkzeug: no threaded arg; running single-threaded", "WARNING")
        self.port = port
    def run(self): self._srv.serve_forever()
    def shutdown(self):
        try: self._srv.shutdown()
        except Exception: pass

def _print_startup(actual_port: int, scheme: str):
    log(f"TTS service listening on {scheme}://{TTS_BIND}:{actual_port}", "SUCCESS")
    auth_note = "ON" if TTS_REQUIRE_AUTH else "OFF"
    log(f"Auth required: {auth_note}", "INFO")
    if TTS_REQUIRE_AUTH: log("Provide X-API-Key or Bearer session_key (via /handshake).", "INFO")
    try_host = "localhost" if TTS_BIND == "0.0.0.0" else TTS_BIND
    curl_flag = "-k " if scheme == "https" else ""
    log(f"Try:  curl {curl_flag}-s {scheme}://{try_host}:{actual_port}/health | jq", "INFO")
    if TTS_BIND == "0.0.0.0":
        for ip in _list_local_ips():
            log(f"LAN URL: {scheme}://{ip}:{actual_port}/health", "INFO")

def _port_is_free(host: str, port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try: s.bind((host, port)); s.close(); return True
    except OSError:
        try: s.close()
        except: pass
        return False

def _find_free_port(host: str, preferred: int, tries: int = 100) -> int:
    for p in range(preferred, preferred + tries + 1):
        if _port_is_free(host, p): return p
    raise RuntimeError(f"No free port in range {preferred}..{preferred+tries}")

_server_thread = None

def _start_server():
    global _server_thread, TTS_BIND
    if TTS_BIND in ("127.0.0.1","localhost","::1") and not TTS_FORCE_LOCAL:
        log("TTS_BIND was localhost; switching to 0.0.0.0 for LAN access. Set TTS_FORCE_LOCAL=1 to keep localhost-only.", "WARNING")
        TTS_BIND = "0.0.0.0"
    ssl_ctx, scheme = _build_ssl_context()
    actual_port = _find_free_port(TTS_BIND, TTS_PORT, tries=100)
    # Prefer production WSGI when executed directly
    try:
        from waitress import serve as _serve
        threading.Thread(target=lambda: _serve(app, host=TTS_BIND, port=actual_port, threads=max(8, TTS_MAX_CONCURRENCY*4)), daemon=True).start()
        _print_startup(actual_port, scheme)
        return actual_port
    except Exception as e:
        log(f"waitress failed ({e}); falling back to Werkzeug.", "WARNING")
        _server_thread = _ServerThread(app, TTS_BIND, actual_port, ssl_context=ssl_ctx)
        _server_thread.start()
        _print_startup(actual_port, scheme)
        return actual_port

def _graceful_exit(signum=None, frame=None):
    import signal as _sig
    signame = {_sig.SIGINT:"SIGINT", getattr(_sig, "SIGTSTP", 20):"SIGTSTP", _sig.SIGTERM:"SIGTERM"}.get(signum, str(signum))
    log(f"Received {signame} — shutting down server…", "INFO")
    try:
        if _server_thread: _server_thread.shutdown()
    except Exception as e:
        log(f"Shutdown error: {e}", "WARNING")
    os._exit(0)

import atexit, signal as _sig
atexit.register(_graceful_exit)
_sig.signal(_sig.SIGINT,  _graceful_exit)
_sig.signal(_sig.SIGTERM, _graceful_exit)
if hasattr(_sig, "SIGTSTP"): _sig.signal(_sig.SIGTSTP, _graceful_exit)

def create_app():
    return app

if __name__ == "__main__":
    _start_server()
    try:
        while True:
            try:
                _sig.pause()
            except AttributeError:
                time.sleep(3600)
    except KeyboardInterrupt:
        _graceful_exit(_sig.SIGINT, None)
