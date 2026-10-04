import glob
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
from urllib.parse import urlparse

import boto3

# ─────────────────────────────────────────
#  GPIO / BOTÃO ARCADE (opcional)
# ─────────────────────────────────────────
try:
    import RPi.GPIO as GPIO
    GPIO_AVAILABLE = True
except ImportError:
    GPIO_AVAILABLE = False
    print("[AVISO] RPi.GPIO não disponível - botão arcade desativado")

# ─────────────────────────────────────────
#  CAMINHOS
# ─────────────────────────────────────────
BASE_DIR        = os.path.abspath(os.path.dirname(__file__ ))
CONFIG_PATH     = os.path.join(BASE_DIR, "config.json")
OUTPUT_DIR      = os.path.join(BASE_DIR, "clips")
BUFFER_DIR      = os.path.join(BASE_DIR, "buffer")
INDEX_HTML_PATH = os.path.join(BASE_DIR, "index.html")
INDEX_JSON_NAME = "clips.json"

# ─────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────
def load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[ERRO] config.json: {e}")
        sys.exit(1)

def env_or_cfg(env_key, cfg_key, default=""):
    return os.environ.get(env_key) or CONFIG.get(cfg_key, default)

def norm(url):
    return (url or "").rstrip("/")

CONFIG          = load_config()
RTSP_URL        = env_or_cfg("RTSP_URL",        "rtsp_url").strip()
SEGMENT_DURATION= int(env_or_cfg("SEGMENT_DURATION", "segment_duration", 5))
BUFFER_SEGMENTS = int(env_or_cfg("BUFFER_SEGMENTS",  "buffer_segments",  12))
CLIP_DURATION   = int(env_or_cfg("CLIP_DURATION",    "clip_duration",    30))
SERVER_PORT     = int(env_or_cfg("SERVER_PORT",      "server_port",      8080))
ARENA_NAME      = env_or_cfg("ARENA_NAME",  "arena_name",  "Arena")
PUBLIC_API_URL  = norm(env_or_cfg("PUBLIC_API_URL",  "public_api_url",  ""))
PUBLIC_BASE_URL = norm(env_or_cfg("PUBLIC_BASE_URL", "public_base_url", ""))
R2_ACCOUNT_ID   = env_or_cfg("R2_ACCOUNT_ID", "r2_account_id").strip()
R2_ACCESS_KEY   = env_or_cfg("R2_ACCESS_KEY", "r2_access_key").strip()
R2_SECRET_KEY   = env_or_cfg("R2_SECRET_KEY", "r2_secret_key").strip()
R2_BUCKET       = env_or_cfg("R2_BUCKET",     "r2_bucket").strip()
R2_ENDPOINT     = norm(env_or_cfg(
    "R2_ENDPOINT", "r2_endpoint",
    f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com" if R2_ACCOUNT_ID else ""
 ))
CLIPS_INDEX_URL = f"{PUBLIC_BASE_URL}/{INDEX_JSON_NAME}" if PUBLIC_BASE_URL else ""

# Configurações do botão arcade
ARCADE_BUTTON_PIN = int(env_or_cfg("ARCADE_BUTTON_PIN", "arcade_button_pin", 17))
ARCADE_BUTTON_ENABLED = env_or_cfg("ARCADE_BUTTON_ENABLED", "arcade_button_enabled", "true").lower() == "true"

if not RTSP_URL:
    print("[ERRO] 'rtsp_url' nao definido no config.json")
    sys.exit(1)

for d in (OUTPUT_DIR, BUFFER_DIR):
    os.makedirs(d, exist_ok=True)

capture_process = None
running         = True
trigger_lock    = threading.Lock()

# ─────────────────────────────────────────
#  CLASSE ARCADE BUTTON
# ─────────────────────────────────────────
class ArcadeButton:
    """
    Gerencia um botão arcade com detecção de cliques e eventos
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
        
        # Logger
        self.enabled = False
    
    def setup(self):
        """Configurar GPIO (chamar uma vez no início)"""
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
            print(f"[BOTAO] Erro ao configurar: {e}")
            return False
    
    def update(self):
        """Chamar regularmente (ex: a cada 10ms no seu loop principal)"""
        if not self.enabled:
            return
        
        try:
            current_state = GPIO.input(self.gpio_pin)
            
            if current_state != self.last_state:
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
                            
                            # Duplo clique?
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
        """Limpar GPIO ao finalizar"""
        if self.enabled and GPIO_AVAILABLE:
            try:
                GPIO.cleanup()
                self.enabled = False
                print("[BOTAO] GPIO limpo")
            except:
                pass

# ─────────────────────────────────────────
#  UTILITARIOS
# ─────────────────────────────────────────
def has_r2():
    return all([R2_ACCESS_KEY, R2_SECRET_KEY, R2_BUCKET, R2_ENDPOINT, PUBLIC_BASE_URL])

def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()

def get_s3():
    return boto3.client(
        service_name="s3",
        endpoint_url=R2_ENDPOINT,
        aws_access_key_id=R2_ACCESS_KEY,
        aws_secret_access_key=R2_SECRET_KEY,
        region_name="auto",
    )

def public_url(filename):
    return f"{PUBLIC_BASE_URL}/{filename}" if PUBLIC_BASE_URL else ""

def local_url(filename):
    return f"/clips/{filename}"

# ─────────────────────────────────────────
#  LISTA DE CLIPES
# ─────────────────────────────────────────
def list_clips():
    clips = []
    files = sorted(
        glob.glob(os.path.join(OUTPUT_DIR, "*.mp4")),
        key=os.path.getmtime, reverse=True
    )
    for f in files:
        name  = os.path.basename(f)
        mtime = os.path.getmtime(f)
        clips.append({
            "name":       name,
            "time":       datetime.fromtimestamp(mtime).strftime("%H:%M:%S"),
            "created_at": datetime.fromtimestamp(mtime).strftime("%d/%m/%Y %H:%M:%S"),
            "size_mb":    round(os.path.getsize(f) / (1024 * 1024), 1),
            "local_url":  local_url(name),
            "public_url": public_url(name),
            "url":        public_url(name),
        })
    return clips

# ─────────────────────────────────────────
#  R2
# ─────────────────────────────────────────
def upload_index():
    if not has_r2():
        return False
    try:
        data = [c for c in list_clips() if c["public_url"]]
        path = os.path.join(BASE_DIR, INDEX_JSON_NAME)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        get_s3().upload_file(path, R2_BUCKET, INDEX_JSON_NAME,
            ExtraArgs={"ContentType": "application/json", "CacheControl": "no-cache"})
        print(f"[R2] Indice atualizado: {CLIPS_INDEX_URL}")
        return True
    except Exception as e:
        print(f"[R2] Falha no indice: {e}")
        return False

def upload_index_html():
    if not has_r2() or not os.path.exists(INDEX_HTML_PATH):
        return
    try:
        get_s3().upload_file(INDEX_HTML_PATH, R2_BUCKET, "index.html",
            ExtraArgs={"ContentType": "text/html; charset=utf-8", "CacheControl": "no-cache"})
        print("[R2] index.html publicado")
    except Exception as e:
        print(f"[R2] Falha ao subir index.html: {e}")

def upload_clip(file_path):
    if not has_r2():
        return ""
    try:
        name = os.path.basename(file_path)
        get_s3().upload_file(file_path, R2_BUCKET, name,
            ExtraArgs={"ContentType": "video/mp4", "CacheControl": "public, max-age=31536000"})
        url = public_url(name)
        print(f"[R2] Video enviado: {url}")
        return url
    except Exception as e:
        print(f"[R2] Falha no upload: {e}")
        return ""

# ─────────────────────────────────────────
#  CAPTURA RTSP
# ─────────────────────────────────────────
def start_capture():
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
    capture_process = subprocess.Popen(cmd)

def wait_buffer(timeout=60):
    t0 = time.time()
    while time.time() - t0 < timeout:
        segs = glob.glob(os.path.join(BUFFER_DIR, "seg_*.ts"))
        if len(segs) >= 2:
            print(f"[CAPTURA] Buffer pronto ({len(segs)} segmentos)")
            return True
        time.sleep(1)
    return False

def cleanup_loop():
    while running:
        try:
            segs = sorted(
                glob.glob(os.path.join(BUFFER_DIR, "seg_*.ts")),
                key=os.path.getmtime
            )
            while len(segs) > BUFFER_SEGMENTS:
                try:
                    os.remove(segs.pop(0))
                except OSError:
                    pass
        except Exception:
            pass
        time.sleep(max(1, SEGMENT_DURATION))

def capture_watchdog():
    while running:
        if capture_process is None or capture_process.poll() is not None:
            start_capture()
        time.sleep(5)

# ─────────────────────────────────────────
#  GERAR CLIPE
# ─────────────────────────────────────────
def save_clip():
    segs = sorted(
        glob.glob(os.path.join(BUFFER_DIR, "seg_*.ts")),
        key=os.path.getmtime
    )
    if not segs:
        return None

    n = max(1, (CLIP_DURATION + SEGMENT_DURATION - 1) // SEGMENT_DURATION)

    # Requisito: buffer precisa ter pelo menos n segmentos (senão concat pode falhar/gerar clipe ruim)
    if len(segs) < n:
        print(f"[CAPTURA] Buffer insuficiente: {len(segs)}/{n} segmentos (aguarde mais)")
        return None

    selected  = segs[-n:]
    name      = datetime.now().strftime("clip_%Y%m%d_%H%M%S.mp4")
    clip_path = os.path.join(OUTPUT_DIR, name)
    concat    = os.path.join(BUFFER_DIR, "concat.txt")

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
    return clip_path

# ─────────────────────────────────────────
#  CALLBACKS DO BOTÃO
# ─────────────────────────────────────────
def on_button_press():
    """Chamado quando botão é pressionado"""
    print(f"[BOTAO] 🔘 Pressionado")

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
    # Por exemplo: alternar gravação contínua, mudar modo, etc

# ─────────────────────────────────────────
#  HTTP SERVER
# ─────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    server_version = "ApertouGravou/2.0"

    def log_message(self, format, *args): return

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS, DELETE")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path, ctype=None):
        if not os.path.exists(path):
            self._json({"error": "Nao encontrado"}, 404)
            return
        try:
            with open(path, "rb") as f:
                data = f.read()
            mime = ctype or mimetypes.guess_type(path)[0] or "application/octet-stream"
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(data)))
            self._cors()
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            self._json({"error": str(e)}, 500)

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    def do_GET(self):
        p = urlparse(self.path).path
        if p == "/" or p == "/index.html":
            self._file(INDEX_HTML_PATH, "text/html; charset=utf-8")
        elif p == "/api/status":
            segs = glob.glob(os.path.join(BUFFER_DIR, "seg_*.ts"))
            self._json({
                "status": "online" if capture_process and capture_process.poll() is None else "offline",
                "buffer_segments": len(segs),
                "buffer_seconds": len(segs) * SEGMENT_DURATION,
                "clips_saved": len(glob.glob(os.path.join(OUTPUT_DIR, "*.mp4"))),
                "arena": ARENA_NAME
            })
        elif p == "/api/clips":
            self._json(list_clips())
        elif p == "/api/config":
            self._json({
                "public_api": PUBLIC_API_URL,
                "media_url": PUBLIC_BASE_URL,
                "arena": ARENA_NAME
            })
        elif p.startswith("/clips/"):
            name = os.path.basename(p)
            self._file(os.path.join(OUTPUT_DIR, name))
        else:
            self._json({"error": "Rota inexistente"}, 404)

    def do_POST(self):
        p = urlparse(self.path).path
        if p == "/api/save" or p == "/api/trigger":
            if not trigger_lock.acquire(blocking=False):
                self._json({"error": "Ja existe uma gravacao em curso"}, 429)
                return
            try:
                print("[API] Solicitacao de gravacao recebida")
                path = save_clip()
                if path:
                    url = upload_clip(path)
                    upload_index()
                    self._json({"success": True, "file": os.path.basename(path), "url": url})
                else:
                    self._json({"error": "Falha ao gerar clipe"}, 500)
            finally:
                trigger_lock.release()
        else:
            self._json({"error": "Rota inexistente"}, 404)

    def do_DELETE(self):
        p = urlparse(self.path).path
        if p.startswith("/api/clips/"):
            name = os.path.basename(p)
            path = os.path.join(OUTPUT_DIR, name)
            if os.path.exists(path):
                os.remove(path)
                upload_index()
                self._json({"success": True})
            else:
                self._json({"error": "Arquivo nao encontrado"}, 404)

def run_server():
    print(f"[SERVER] Rodando em http://{get_local_ip( )}:{SERVER_PORT}")
    httpd = ThreadingHTTPServer(("0.0.0.0", SERVER_PORT ), Handler)
    httpd.serve_forever( )

def button_loop():
    """Loop para processar o botão a cada 10ms"""
    while running:
        arcade_button.update()
        time.sleep(0.01)

def signal_handler(sig, frame):
    global running
    print("\n[SISTEMA] Encerrando...")
    running = False
    if capture_process:
        capture_process.terminate()
    if arcade_button.enabled:
        arcade_button.cleanup()
    sys.exit(0)

if __name__ == "__main__":
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Inicializar botão arcade
    arcade_button = ArcadeButton(gpio_pin=ARCADE_BUTTON_PIN)
    if ARCADE_BUTTON_ENABLED:
        arcade_button.setup()
        arcade_button.on_press = on_button_press
        arcade_button.on_release = on_button_release
        arcade_button.on_single_click = on_button_single_click
        arcade_button.on_double_click = on_button_double_click

    # Inicia Threads
    threading.Thread(target=capture_watchdog, daemon=True).start()
    threading.Thread(target=cleanup_loop, daemon=True).start()
    
    if arcade_button.enabled:
        threading.Thread(target=button_loop, daemon=True).start()
    
    # Publica index inicial se R2 configurado
    upload_index_html()
    upload_index()

    # Roda Servidor (Bloqueante)
    run_server()
