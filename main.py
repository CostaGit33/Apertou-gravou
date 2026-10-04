"""
APERTOU GRAVOU - Sistema de Captura e Replay Instantâneo
Versão HÍBRIDA PRO (Antigo + GitHub Melhorado)

Combina:
✓ Robustez da versão original (validações, streaming, security)
✓ Eficiência da versão GitHub (ArcadeButton pro, callbacks, debounce)
✓ Melhor tratamento de erros e locks
✓ Thread management otimizado
"""

import base64
import glob
import hmac
import json
import mimetypes
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, unquote

import boto3

# ─────────────────────────────────────────────────────────────────
# GPIO / BOTÃO ARCADE (opcional)
# ─────────────────────────────────────────────────────────────────

try:
    import RPi.GPIO as GPIO
    GPIO_AVAILABLE = True
except ImportError:
    GPIO_AVAILABLE = False
    GPIO = None
    print("[AVISO] RPi.GPIO não disponível. Botão arcade desativado.")

# ─────────────────────────────────────────────────────────────────
# CAMINHOS E DIRETÓRIOS
# ─────────────────────────────────────────────────────────────────

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
OUTPUT_DIR = os.path.join(BASE_DIR, "clips")
BUFFER_DIR = os.path.join(BASE_DIR, "buffer")
INDEX_HTML_PATH = os.path.join(BASE_DIR, "index.html")
INDEX_JSON_PATH = os.path.join(BASE_DIR, "clips.json")
INDEX_JSON_NAME = "clips.json"

os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(BUFFER_DIR, exist_ok=True)

# ─────────────────────────────────────────────────────────────────
# CARREGAMENTO DE CONFIGURAÇÃO
# ─────────────────────────────────────────────────────────────────

def load_config():
    """Carregar config.json com fallback para env vars"""
    if not os.path.exists(CONFIG_PATH):
        print("[CONFIG] config.json não encontrado. Usando variáveis de ambiente.")
        return {}
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as file:
            data = json.load(file)
        if not isinstance(data, dict):
            print("[CONFIG] config.json não contém um objeto JSON válido.")
            return {}
        print("[CONFIG] ✓ config.json carregado com sucesso")
        return data
    except Exception as error:
        print(f"[CONFIG] Erro ao ler config.json: {error}")
        return {}


CONFIG = load_config()


def env_or_cfg(env_key, cfg_key, default=None):
    """Prioridade: env var > config.json > default"""
    value = os.environ.get(env_key)
    if value is not None and str(value).strip() != "":
        return value
    value = CONFIG.get(cfg_key)
    return default if value is None else value


def normalize_url(value):
    """Normalizar URLs removendo trailing slash"""
    return "" if not value else str(value).strip().rstrip("/")


def to_int(value, default):
    """Converter para int com fallback"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# ─────────────────────────────────────────────────────────────────
# VARIÁVEIS DE CONFIGURAÇÃO
# ─────────────────────────────────────────────────────────────────

RTSP_URL = str(env_or_cfg("RTSP_URL", "rtsp_url", "")).strip()
SEGMENT_DURATION = max(1, to_int(env_or_cfg("SEGMENT_DURATION", "segment_duration", 5), 5))
BUFFER_SEGMENTS = max(3, to_int(env_or_cfg("BUFFER_SEGMENTS", "buffer_segments", 12), 12))
CLIP_DURATION = max(1, to_int(env_or_cfg("CLIP_DURATION", "clip_duration", 30), 30))
SERVER_PORT = max(1, min(65535, to_int(env_or_cfg("SERVER_PORT", "server_port", 8080), 8080)))
ARENA_NAME = str(env_or_cfg("ARENA_NAME", "arena_name", "Arena"))
PUBLIC_API_URL = normalize_url(env_or_cfg("PUBLIC_API_URL", "public_api_url", ""))
PUBLIC_BASE_URL = normalize_url(env_or_cfg("PUBLIC_BASE_URL", "public_base_url", ""))

R2_ACCOUNT_ID = str(env_or_cfg("R2_ACCOUNT_ID", "r2_account_id", "")).strip()
R2_ACCESS_KEY = str(env_or_cfg("R2_ACCESS_KEY", "r2_access_key", "")).strip()
R2_SECRET_KEY = str(env_or_cfg("R2_SECRET_KEY", "r2_secret_key", "")).strip()
R2_BUCKET = str(env_or_cfg("R2_BUCKET", "r2_bucket", "")).strip()
R2_ENDPOINT = normalize_url(env_or_cfg(
    "R2_ENDPOINT",
    "r2_endpoint",
    f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com" if R2_ACCOUNT_ID else ""
))

ARCADE_BUTTON_PIN = max(0, to_int(env_or_cfg("ARCADE_BUTTON_PIN", "arcade_button_pin", 17), 17))
ARCADE_BUTTON_ENABLED = str(
    env_or_cfg("ARCADE_BUTTON_ENABLED", "arcade_button_enabled", "true")
).lower() in ("1", "true", "yes", "on")

CLIPS_INDEX_URL = f"{PUBLIC_BASE_URL}/{INDEX_JSON_NAME}" if PUBLIC_BASE_URL else ""

# Autenticação Basic para operações críticas.
# Não há credenciais padrão: configure API_USERNAME/API_PASSWORD no ambiente
# ou api_username/api_password no config.json.
API_USERNAME = str(env_or_cfg("API_USERNAME", "api_username", "")).strip()
API_PASSWORD = str(env_or_cfg("API_PASSWORD", "api_password", ""))
AUTH_REALM = str(env_or_cfg("AUTH_REALM", "auth_realm", "Apertou Gravou"))

# ─────────────────────────────────────────────────────────────────
# VALIDAÇÃO DE CONFIGURAÇÃO CRÍTICA
# ─────────────────────────────────────────────────────────────────

if not RTSP_URL:
    print("[ERRO] 'rtsp_url' não definido no config.json ou variáveis de ambiente")
    print("[ERRO] Defina: RTSP_URL=rtsp://seu-stream")
    sys.exit(1)

print("")
print("=" * 60)
print(" APERTOU GRAVOU - Hybrid Pro Edition")
print("=" * 60)
print(f"[CONFIG] Arena: {ARENA_NAME}")
print(f"[CONFIG] RTSP: {RTSP_URL[:50]}...")
print(f"[CONFIG] Segmento: {SEGMENT_DURATION}s | Buffer: {BUFFER_SEGMENTS}")
print(f"[CONFIG] Clipe: {CLIP_DURATION}s | Porta: {SERVER_PORT}")
print(f"[CONFIG] R2: {'✓ ATIVO' if (R2_ACCESS_KEY and R2_BUCKET) else '✗ DESATIVADO'}")
print(f"[CONFIG] GPIO: {'✓ DISPONÍVEL' if GPIO_AVAILABLE else '✗ INDISPONÍVEL'}")
print("=" * 60)
print("")

# ─────────────────────────────────────────────────────────────────
# VARIÁVEIS GLOBAIS E LOCKS
# ─────────────────────────────────────────────────────────────────

running = True
capture_process = None
capture_lock = threading.Lock()
trigger_lock = threading.Lock()
r2_lock = threading.Lock()
http_server = None
arcade_button = None

# ─────────────────────────────────────────────────────────────────
# CLASSE ARCADE BUTTON - VERSÃO HÍBRIDA ROBUSTA
# ─────────────────────────────────────────────────────────────────

class ArcadeButton:
    """
    Gerencia botão arcade com debounce, detecção de duplo clique,
    e callbacks personalizáveis. Versão robusta com tratamento de erro.
    """

    def __init__(self, gpio_pin, debounce_time=0.05, double_click_window=0.3):
        self.gpio_pin = gpio_pin
        self.debounce_time = debounce_time
        self.double_click_window = double_click_window

        # Estado
        self.is_pressed = False
        self.last_state = None
        self.last_press_time = 0
        self.press_start_time = 0

        # Callbacks
        self.on_single_click = None
        self.on_double_click = None
        self.on_press = None
        self.on_release = None

        # Status
        self.enabled = False

    def setup(self):
        """Configurar GPIO uma única vez no startup"""
        if not GPIO_AVAILABLE:
            print(f"[BOTAO] GPIO não disponível, botão desativado")
            return False

        try:
            GPIO.setmode(GPIO.BCM)
            GPIO.setup(self.gpio_pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)
            self.last_state = GPIO.input(self.gpio_pin)
            self.enabled = True
            print(f"[BOTAO] ✓ Configurado no GPIO {self.gpio_pin}")
            return True
        except Exception as e:
            print(f"[BOTAO] Erro ao configurar GPIO: {e}")
            return False

    def update(self):
        """Chamar periodicamente (ex: a cada 10ms) para processar botão"""
        if not self.enabled:
            return

        try:
            current_state = GPIO.input(self.gpio_pin)

            if current_state != self.last_state:
                # Debounce: esperar e reconhecer
                time.sleep(self.debounce_time)
                current_state = GPIO.input(self.gpio_pin)

                if current_state != self.last_state:
                    self.last_state = current_state

                    if current_state == 0:  # Pressionado (LOW)
                        self.is_pressed = True
                        self.press_start_time = time.time()
                        if self.on_press:
                            self.on_press()

                    else:  # Solto (HIGH)
                        if self.is_pressed:
                            press_duration = (time.time() - self.press_start_time) * 1000
                            current_time = time.time()

                            # Detectar duplo clique
                            if (current_time - self.last_press_time) < self.double_click_window:
                                if self.on_double_click:
                                    self.on_double_click(press_duration)
                            else:
                                if self.on_single_click:
                                    self.on_single_click(press_duration)

                            self.last_press_time = current_time
                            self.is_pressed = False

                        if self.on_release:
                            self.on_release()

        except Exception as e:
            print(f"[BOTAO] Erro ao processar: {e}")

    def cleanup(self):
        """Limpar GPIO ao finalizar programa"""
        if self.enabled and GPIO_AVAILABLE:
            try:
                GPIO.cleanup()
                self.enabled = False
                print("[BOTAO] GPIO limpo")
            except Exception:
                pass

# ─────────────────────────────────────────────────────────────────
# FUNÇÕES UTILITÁRIAS
# ─────────────────────────────────────────────────────────────────

def get_local_ip():
    """Obter IP local da máquina"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def safe_filename(filename):
    """Sanitizar nome de arquivo contra path traversal attacks"""
    filename = os.path.basename(unquote(filename))
    return "" if filename in ("", ".", "..") else filename


def public_url(filename):
    """Gerar URL pública do arquivo"""
    return f"{PUBLIC_BASE_URL}/{filename}" if PUBLIC_BASE_URL else ""


def local_url(filename):
    """Gerar URL local do arquivo"""
    return f"/clips/{filename}"


def file_size_mb(path):
    """Obter tamanho do arquivo em MB"""
    try:
        return round(os.path.getsize(path) / (1024 * 1024), 1)
    except OSError:
        return 0


def process_is_running():
    """Verificar se processo de captura está rodando"""
    global capture_process
    return capture_process is not None and capture_process.poll() is None


def has_r2():
    """Verificar se R2 está configurado"""
    return all((R2_ACCESS_KEY, R2_SECRET_KEY, R2_BUCKET, R2_ENDPOINT, PUBLIC_BASE_URL))


def get_s3():
    """Criar cliente S3 para Cloudflare R2"""
    return boto3.client(
        service_name="s3",
        endpoint_url=R2_ENDPOINT,
        aws_access_key_id=R2_ACCESS_KEY,
        aws_secret_access_key=R2_SECRET_KEY,
        region_name="auto",
    )


# ─────────────────────────────────────────────────────────────────
# LISTAGEM DE CLIPES
# ─────────────────────────────────────────────────────────────────

def list_clips():
    """Listar todos os clipes salvos com metadata"""
    clips = []
    files = glob.glob(os.path.join(OUTPUT_DIR, "*.mp4"))
    files.sort(key=lambda path: os.path.getmtime(path), reverse=True)

    for path in files:
        try:
            name = os.path.basename(path)
            mtime = os.path.getmtime(path)
            created = datetime.fromtimestamp(mtime)
            clips.append({
                "name": name,
                "time": created.strftime("%H:%M:%S"),
                "created_at": created.strftime("%d/%m/%Y %H:%M:%S"),
                "timestamp": int(mtime),
                "size_mb": file_size_mb(path),
                "local_url": local_url(name),
                "public_url": public_url(name),
                "url": public_url(name),
            })
        except OSError:
            continue

    return clips


# ─────────────────────────────────────────────────────────────────
# ÍNDICE LOCAL
# ─────────────────────────────────────────────────────────────────

def write_local_index():
    """Gerar arquivo clips.json local"""
    try:
        with open(INDEX_JSON_PATH, "w", encoding="utf-8") as file:
            json.dump(list_clips(), file, ensure_ascii=False, indent=2)
        return True
    except Exception as error:
        print(f"[INDEX] Erro ao gerar clips.json: {error}")
        return False


# ─────────────────────────────────────────────────────────────────
# UPLOAD R2 / CLOUDFLARE
# ─────────────────────────────────────────────────────────────────

def upload_index():
    """Enviar índice de clipes para R2"""
    write_local_index()
    if not has_r2():
        return False

    with r2_lock:
        try:
            get_s3().upload_file(
                INDEX_JSON_PATH,
                R2_BUCKET,
                INDEX_JSON_NAME,
                ExtraArgs={
                    "ContentType": "application/json; charset=utf-8",
                    "CacheControl": "no-cache, no-store, must-revalidate",
                },
            )
            print(f"[R2] Índice atualizado: {CLIPS_INDEX_URL}")
            return True
        except Exception as error:
            print(f"[R2] Falha ao enviar clips.json: {error}")
            return False


def upload_index_html():
    """Enviar index.html para R2"""
    if not has_r2() or not os.path.exists(INDEX_HTML_PATH):
        return False

    with r2_lock:
        try:
            get_s3().upload_file(
                INDEX_HTML_PATH,
                R2_BUCKET,
                "index.html",
                ExtraArgs={
                    "ContentType": "text/html; charset=utf-8",
                    "CacheControl": "no-cache, no-store, must-revalidate",
                },
            )
            print("[R2] index.html publicado")
            return True
        except Exception as error:
            print(f"[R2] Falha ao enviar index.html: {error}")
            return False


def upload_clip(file_path):
    """Enviar clipe para R2"""
    if not has_r2() or not os.path.exists(file_path):
        return ""

    filename = os.path.basename(file_path)
    with r2_lock:
        try:
            get_s3().upload_file(
                file_path,
                R2_BUCKET,
                filename,
                ExtraArgs={
                    "ContentType": "video/mp4",
                    "CacheControl": "public, max-age=31536000, immutable",
                },
            )
            url = public_url(filename)
            print(f"[R2] Vídeo enviado: {url}")
            return url
        except Exception as error:
            print(f"[R2] Falha no upload do vídeo: {error}")
            return ""


def delete_r2_clip(filename):
    """Deletar clipe do R2"""
    if not has_r2():
        return False

    with r2_lock:
        try:
            get_s3().delete_object(
                Bucket=R2_BUCKET,
                Key=filename,
            )
            print(f"[R2] Deletado: {filename}")
            return True
        except Exception as error:
            print(f"[R2] Falha ao deletar: {error}")
            return False


# ─────────────────────────────────────────────────────────────────
# CAPTURA RTSP COM FFMPEG
# ─────────────────────────────────────────────────────────────────

def start_capture():
    """Iniciar captura RTSP"""
    global capture_process

    # Limpeza de segurança para evitar processos zumbis
    if capture_process:
        try:
            capture_process.terminate()
            capture_process.wait(timeout=2)
        except:
            try:
                capture_process.kill()
            except:
                pass

    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-rtsp_transport", "tcp",
        "-timeout", "10000000",
        "-i", RTSP_URL,
        "-c:v", "libx264",
        "-threads", "2",
        "-preset", "ultrafast",
        "-c:a", "aac",
        "-crf", "22",
        "-g", str(SEGMENT_DURATION * 12),
        "-sc_threshold", "0",
        "-f", "segment",
        "-segment_time", str(SEGMENT_DURATION),
        "-reset_timestamps", "1",
        "-strftime", "1",
        os.path.join(BUFFER_DIR, "seg_%Y%m%d_%H%M%S.ts"),
    ]

    print(f"[CAPTURA] Conectando: {RTSP_URL}")

    with capture_lock:
        capture_process = subprocess.Popen(cmd)


def wait_buffer(timeout=60):
    """Aguardar buffer ser preenchido com segmentos"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        segs = glob.glob(os.path.join(BUFFER_DIR, "seg_*.ts"))
        if len(segs) >= 2:
            print(f"[CAPTURA] Buffer pronto ({len(segs)} segmentos)")
            return True
        time.sleep(1)

    print(f"[CAPTURA] ⚠️ Timeout aguardando buffer ({timeout}s)")
    return False


def cleanup_buffer():
    """Loop para manter buffer dentro do limite"""
    while running:
        try:
            segs = sorted(
                glob.glob(os.path.join(BUFFER_DIR, "seg_*.ts")),
                key=os.path.getmtime
            )

            while len(segs) > BUFFER_SEGMENTS:
                try:
                    old_seg = segs.pop(0)
                    os.remove(old_seg)
                except OSError:
                    pass

        except Exception:
            pass

        time.sleep(max(1, SEGMENT_DURATION))


def capture_watchdog():
    """Verificar se processo de captura está rodando e reiniciar se necessário"""
    while running:
        if capture_process is None or capture_process.poll() is not None:
            print("[CAPTURA] Processo morreu, reiniciando...")
            start_capture()

        time.sleep(5)


# ─────────────────────────────────────────────────────────────────
# GERAÇÃO DE CLIPES
# ─────────────────────────────────────────────────────────────────

def save_clip():
    """Salvar clipe a partir dos segmentos do buffer"""
    segs = sorted(
        glob.glob(os.path.join(BUFFER_DIR, "seg_*.ts")),
        key=os.path.getmtime
    )

    if not segs:
        print("[CAPTURA] ⚠️ Nenhum segmento no buffer")
        return None

    n = max(1, (CLIP_DURATION + SEGMENT_DURATION - 1) // SEGMENT_DURATION)

    if len(segs) < n:
        print(f"[CAPTURA] Buffer insuficiente: {len(segs)}/{n} segmentos (aguarde mais)")
        return None

    selected = segs[-n:]

    name = datetime.now().strftime("clip_%Y%m%d_%H%M%S.mp4")
    clip_path = os.path.join(OUTPUT_DIR, name)
    concat = os.path.join(BUFFER_DIR, "concat.txt")

    with open(concat, "w", encoding="utf-8") as f:
        for s in selected:
            f.write(f"file '{s.replace(chr(92), '/')}'\n")

    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-f", "concat", "-safe", "0", "-i", concat,
        "-c:v", "libx264", "-preset", "ultrafast",
        "-threads", "2",
        "-c:a", "aac",
        "-crf", "22", "-vf", "scale=1280:-2",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        "-y", clip_path,
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        print(f"[ERRO] Clipe falhou: {result.stderr.strip()}")
        return None

    print(f"[CAPTURA] ✓ Clipe gerado: {name} ({file_size_mb(clip_path)}MB)")
    return clip_path


# ─────────────────────────────────────────────────────────────────
# CALLBACKS DO BOTÃO
# ─────────────────────────────────────────────────────────────────

def on_button_press():
    """Chamado quando botão é pressionado"""
    print("[BOTAO] 🔘 Pressionado")


def on_button_release():
    """Chamado quando botão é solto"""
    pass


def on_button_single_click(duration):
    """Chamar quando há um clique simples - SALVA CLIPE"""
    print(f"[BOTAO] ✓ CLIQUE ({duration:.0f}ms) → Salvando clip...")

    if not trigger_lock.acquire(blocking=False):
        print("[BOTAO] ⚠️ Já existe uma gravação em curso")
        return

    try:
        path = save_clip()
        if path:
            filename = os.path.basename(path)
            url = upload_clip(path)
            upload_index()
            print(f"[BOTAO] ✅ Clipe salvo: {filename}")
            if url:
                print(f"[BOTAO] 🌐 URL: {url}")
        else:
            print("[BOTAO] ❌ Falha ao gerar clipe")
    finally:
        trigger_lock.release()


def on_button_double_click(duration):
    """Chamar quando há um duplo clique"""
    print(f"[BOTAO] 🔥 DUPLO CLIQUE ({duration:.0f}ms)")
    # Aqui você pode adicionar funcionalidade especial
    # Ex: alternar gravação contínua, mudar modo, etc


# ─────────────────────────────────────────────────────────────────
# HTTP SERVER - HANDLER
# ─────────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    server_version = "ApertouGravou/3.0-Hybrid"

    def log_message(self, format, *args):
        return  # Suprimir logs padrão

    def _auth_configured(self):
        return bool(API_USERNAME and API_PASSWORD)

    def _require_basic_auth(self):
        """Exigir Basic Auth nas operações que alteram ou excluem dados."""
        if not self._auth_configured():
            self._json({"error": "Autenticação da API não configurada."}, 503)
            return False

        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            self.send_response(401)
            self.send_header("WWW-Authenticate", f'Basic realm="{AUTH_REALM}"')
            self._cors()
            self.end_headers()
            return False

        try:
            encoded = header[6:].strip()
            decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
            username, separator, password = decoded.partition(":")
        except (ValueError, UnicodeError, base64.binascii.Error):
            username, separator, password = "", "", ""

        valid = (
            separator == ":"
            and hmac.compare_digest(username, API_USERNAME)
            and hmac.compare_digest(password, API_PASSWORD)
        )
        if not valid:
            self.send_response(401)
            self.send_header("WWW-Authenticate", f'Basic realm="{AUTH_REALM}"')
            self._cors()
            self.end_headers()
            return False
        return True

    def _cors(self):
        """Enviar headers CORS"""
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS, DELETE")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _json(self, data, status=200):
        """Enviar resposta JSON"""
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def _file(self, path, content_type=None):
        """Enviar arquivo com streaming em chunks"""
        if not os.path.isfile(path):
            self._json({"error": "Arquivo não encontrado."}, 404)
            return

        try:
            size = os.path.getsize(path)
            mime = content_type or mimetypes.guess_type(path)[0] or "application/octet-stream"
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(size))
            self._cors()
            self.end_headers()

            with open(path, "rb") as file:
                while True:
                    chunk = file.read(1024 * 1024)  # 1MB chunks
                    if not chunk:
                        break
                    try:
                        self.wfile.write(chunk)
                    except BrokenPipeError:
                        break

        except Exception as error:
            print(f"[HTTP] Erro enviando arquivo: {error}")

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path

        if path in ("/", "/index.html"):
            self._file(INDEX_HTML_PATH, "text/html; charset=utf-8")
            return

        if path == "/api/status":
            segments = glob.glob(os.path.join(BUFFER_DIR, "seg_*.ts"))
            self._json({
                "status": "online" if process_is_running() else "offline",
                "capture": {
                    "running": process_is_running(),
                    "pid": capture_process.pid if capture_process and process_is_running() else None,
                    "rtsp_configured": bool(RTSP_URL),
                },
                "buffer": {
                    "segments": len(segments),
                    "configured_segments": BUFFER_SEGMENTS,
                    "segment_duration": SEGMENT_DURATION,
                    "seconds": len(segments) * SEGMENT_DURATION,
                },
                "clip": {
                    "duration": CLIP_DURATION,
                    "saved": len(glob.glob(os.path.join(OUTPUT_DIR, "*.mp4"))),
                },
                "button": {
                    "enabled": bool(arcade_button and arcade_button.enabled),
                    "gpio_available": GPIO_AVAILABLE,
                    "pin": ARCADE_BUTTON_PIN,
                },
                "r2": {
                    "configured": has_r2(),
                    "public_url": PUBLIC_BASE_URL,
                },
                "arena": ARENA_NAME,
                "server": {
                    "port": SERVER_PORT,
                    "local_ip": get_local_ip(),
                },
            })
            return

        if path == "/api/clips":
            self._json(list_clips())
            return

        if path == "/api/config":
            self._json({
                "public_api": PUBLIC_API_URL,
                "media_url": PUBLIC_BASE_URL,
                "arena": ARENA_NAME,
                "clip_duration": CLIP_DURATION,
            })
            return

        if path == "/clips.json":
            write_local_index()
            self._file(INDEX_JSON_PATH, "application/json; charset=utf-8")
            return

        if path.startswith("/clips/"):
            filename = safe_filename(path[len("/clips/"):])
            if not filename:
                self._json({"error": "Nome de arquivo inválido."}, 400)
                return
            self._file(os.path.join(OUTPUT_DIR, filename), "video/mp4")
            return

        self._json({"error": "Rota inexistente."}, 404)

    def do_POST(self):
        path = urlparse(self.path).path

        if path in ("/api/save", "/api/trigger"):
            if not self._require_basic_auth():
                return
            if not trigger_lock.acquire(blocking=False):
                self._json({"error": "Já existe uma gravação em curso"}, 429)
                return

            try:
                print("[API] Solicitação de gravação recebida")
                clip_path = save_clip()
                if clip_path:
                    url = upload_clip(clip_path)
                    upload_index()
                    self._json({
                        "success": True,
                        "file": os.path.basename(clip_path),
                        "url": url,
                        "size_mb": file_size_mb(clip_path)
                    })
                else:
                    self._json({"error": "Falha ao gerar clipe"}, 500)
            finally:
                trigger_lock.release()
            return

        self._json({"error": "Rota inexistente."}, 404)

    def do_DELETE(self):
        path = urlparse(self.path).path

        if not path.startswith("/api/clips/"):
            self._json({"error": "Rota inexistente."}, 404)
            return

        if not self._require_basic_auth():
            return

        filename = safe_filename(path[len("/api/clips/"):])

        if not filename:
            self._json({"error": "Nome de arquivo inválido."}, 400)
            return

        local_path = os.path.join(OUTPUT_DIR, filename)

        if not os.path.isfile(local_path):
            self._json({"error": "Arquivo não encontrado."}, 404)
            return

        try:
            os.remove(local_path)
            r2_deleted = delete_r2_clip(filename)
            upload_index()
            self._json({
                "success": True,
                "file": filename,
                "r2_deleted": r2_deleted,
            })
        except Exception as error:
            self._json({"success": False, "error": str(error)}, 500)


# ─────────────────────────────────────────────────────────────────
# HTTP SERVER - RUNNER
# ─────────────────────────────────────────────────────────────────

def run_server():
    """Rodar servidor HTTP"""
    global http_server
    local_ip = get_local_ip()

    print("")
    print("=" * 60)
    print(f"[SERVER] http://{local_ip}:{SERVER_PORT}")
    print(f"[SERVER] Porta: {SERVER_PORT}")
    print(f"[SERVER] Arena: {ARENA_NAME}")
    print("=" * 60)
    print("")

    http_server = ThreadingHTTPServer(("0.0.0.0", SERVER_PORT), Handler)
    http_server.daemon_threads = True

    try:
        http_server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            http_server.server_close()
        except Exception:
            pass
        http_server = None


# ─────────────────────────────────────────────────────────────────
# BUTTON LOOP
# ─────────────────────────────────────────────────────────────────

def button_loop():
    """Loop para processar botão a cada 10ms"""
    while running:
        arcade_button.update()
        time.sleep(0.01)


# ─────────────────────────────────────────────────────────────────
# SIGNAL HANDLER
# ─────────────────────────────────────────────────────────────────

def signal_handler(sig, frame):
    """Tratar Ctrl+C e SIGTERM gracefully"""
    global running

    if not running:
        return

    print("")
    print("[SISTEMA] Encerrando...")
    running = False

    if http_server:
        try:
            # shutdown() deve ser chamado fora da thread que executa serve_forever.
            threading.Thread(target=http_server.shutdown, daemon=True).start()
        except Exception:
            pass

    if arcade_button:
        try:
            arcade_button.cleanup()
        except Exception:
            pass

    if capture_process:
        try:
            capture_process.terminate()
            capture_process.wait(timeout=2)
        except:
            try:
                capture_process.kill()
            except:
                pass

    print("[SISTEMA] Encerrado.")
    sys.exit(0)


# ─────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────

def main():
    """Função principal"""
    global arcade_button

    # Inicializar botão arcade
    arcade_button = ArcadeButton(gpio_pin=ARCADE_BUTTON_PIN)

    if ARCADE_BUTTON_ENABLED:
        if arcade_button.setup():
            arcade_button.on_press = on_button_press
            arcade_button.on_release = on_button_release
            arcade_button.on_single_click = on_button_single_click
            arcade_button.on_double_click = on_button_double_click
    else:
        print("[BOTAO] Desativado pela configuração.")

    # Iniciar captura
    start_capture()

    # Iniciar threads
    threading.Thread(target=capture_watchdog, name="capture-watchdog", daemon=True).start()
    threading.Thread(target=cleanup_buffer, name="buffer-cleanup", daemon=True).start()

    if arcade_button.enabled:
        threading.Thread(target=button_loop, name="button-loop", daemon=True).start()

    # Aguardar buffer
    wait_buffer(timeout=60)

    # Gerar e enviar índices
    write_local_index()

    if has_r2():
        upload_index_html()
        upload_index()

    # Rodar servidor (bloqueante)
    run_server()


# ─────────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        main()
    except Exception as error:
        print(f"[FATAL] {error}")
        signal_handler(None, None)
        sys.exit(1)