#!/usr/bin/env python3
"""
╔═══════════════════════════════════════════════════════════════════╗
║                    APERTOU GRAVOU - ARENA JAGUAR                 ║
║                                                                   ║
║  Sistema de Instant Replay com Botão Arcade                      ║
║  - Captura RTSP contínua                                         ║
║  - Cliques do botão salvam clips                                 ║
║  - Upload automático para Cloudflare R2                          ║
║  - Interface web para visualizar clipes                          ║
╚═══════════════════════════════════════════════════════════════════╝
"""

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

# ═══════════════════════════════════════════════════════════════════
#  IMPORTS OPCIONAIS
# ═══════════════════════════════════════════════════════════════════

try:
    import RPi.GPIO as GPIO
    GPIO_AVAILABLE = True
except ImportError:
    GPIO_AVAILABLE = False

# ═══════════════════════════════════════════════════════════════════
#  CONFIGURAÇÕES DE CAMINHOS
# ═══════════════════════════════════════════════════════════════════

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
OUTPUT_DIR = os.path.join(BASE_DIR, "clips")
BUFFER_DIR = os.path.join(BASE_DIR, "buffer")
INDEX_HTML_PATH = os.path.join(BASE_DIR, "index.html")
INDEX_JSON_NAME = "clips.json"

# Criar diretórios se não existirem
for directory in [OUTPUT_DIR, BUFFER_DIR]:
    os.makedirs(directory, exist_ok=True)

# ═══════════════════════════════════════════════════════════════════
#  CARREGAMENTO DE CONFIGURAÇÃO
# ═══════════════════════════════════════════════════════════════════

def load_config():
    """Carrega configurações do config.json"""
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        print(f"[ERRO] Arquivo {CONFIG_PATH} não encontrado")
        sys.exit(1)
    except json.JSONDecodeError:
        print(f"[ERRO] Arquivo {CONFIG_PATH} inválido (JSON mal formatado)")
        sys.exit(1)

def env_or_cfg(env_key, cfg_key, default=""):
    """Lê valor de variável de ambiente ou config.json"""
    return os.environ.get(env_key) or CONFIG.get(cfg_key, default)

def normalize_url(url):
    """Remove barras finais de URLs"""
    return (url or "").rstrip("/")

# ═══════════════════════════════════════════════════════════════════
#  CARREGAR CONFIGURAÇÕES
# ═══════════════════════════════════════════════════════════════════

CONFIG = load_config()

# Configurações de captura
RTSP_URL = env_or_cfg("RTSP_URL", "rtsp_url").strip()
SEGMENT_DURATION = int(env_or_cfg("SEGMENT_DURATION", "segment_duration", 5))
BUFFER_SEGMENTS = int(env_or_cfg("BUFFER_SEGMENTS", "buffer_segments", 12))
CLIP_DURATION = int(env_or_cfg("CLIP_DURATION", "clip_duration", 30))

# Configurações de servidor
SERVER_PORT = int(env_or_cfg("SERVER_PORT", "server_port", 8080))
ARENA_NAME = env_or_cfg("ARENA_NAME", "arena_name", "Arena")

# URLs públicas
PUBLIC_API_URL = normalize_url(env_or_cfg("PUBLIC_API_URL", "public_api_url", ""))
PUBLIC_BASE_URL = normalize_url(env_or_cfg("PUBLIC_BASE_URL", "public_base_url", ""))

# Configurações Cloudflare R2
R2_ACCOUNT_ID = env_or_cfg("R2_ACCOUNT_ID", "r2_account_id").strip()
R2_ACCESS_KEY = env_or_cfg("R2_ACCESS_KEY", "r2_access_key").strip()
R2_SECRET_KEY = env_or_cfg("R2_SECRET_KEY", "r2_secret_key").strip()
R2_BUCKET = env_or_cfg("R2_BUCKET", "r2_bucket").strip()
R2_ENDPOINT = normalize_url(env_or_cfg(
    "R2_ENDPOINT", "r2_endpoint",
    f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com" if R2_ACCOUNT_ID else ""
))

# Configurações do botão arcade
ARCADE_BUTTON_PIN = int(env_or_cfg("ARCADE_BUTTON_PIN", "arcade_button_pin", 17))
ARCADE_BUTTON_ENABLED = env_or_cfg("ARCADE_BUTTON_ENABLED", "arcade_button_enabled", "true").lower() == "true"

# Validações básicas
if not RTSP_URL:
    print("[AVISO] RTSP_URL não configurada - captura desativada")

CLIPS_INDEX_URL = f"{PUBLIC_BASE_URL}/{INDEX_JSON_NAME}" if PUBLIC_BASE_URL else ""

# ═══════════════════════════════════════════════════════════════════
#  VARIÁVEIS GLOBAIS
# ═══════════════════════════════════════════════════════════════════

capture_process = None
running = True
trigger_lock = threading.Lock()
arcade_button = None

# ═══════════════════════════════════════════════════════════════════
#  CLASSE ARCADE BUTTON
# ═══════════════════════════════════════════════════════════════════

class ArcadeButton:
    """
    Gerenciador de botão arcade com detecção de cliques
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
        
        self.enabled = False
    
    def setup(self):
        """Configurar GPIO"""
        if not GPIO_AVAILABLE:
            print(f"[BOTAO] GPIO não disponível")
            return False
        
        try:
            GPIO.setmode(GPIO.BCM)
            GPIO.setup(self.gpio_pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)
            self.last_state = GPIO.input(self.gpio_pin)
            self.enabled = True
            print(f"[BOTAO] ✓ Configurado no GPIO {self.gpio_pin}")
            return True
        except Exception as e:
            print(f"[BOTAO] Erro: {e}")
            return False
    
    def update(self):
        """Processar estado do botão (chamar regularmente)"""
        if not self.enabled:
            return
        
        try:
            current_state = GPIO.input(self.gpio_pin)
            
            if current_state != self.last_state:
                time.sleep(self.debounce_time)
                current_state = GPIO.input(self.gpio_pin)
                
                if current_state != self.last_state:
                    self.last_state = current_state
                    
                    if current_state == 0:  # Pressionado
                        self.is_pressed = True
                        self.press_start_time = time.time()
                        if self.on_press:
                            self.on_press()
                    
                    else:  # Solto
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
            print(f"[BOTAO] Erro: {e}")
    
    def cleanup(self):
        """Limpar GPIO"""
        if self.enabled and GPIO_AVAILABLE:
            try:
                GPIO.cleanup()
                self.enabled = False
            except:
                pass

# ═══════════════════════════════════════════════════════════════════
#  FUNÇÕES UTILITÁRIAS
# ═══════════════════════════════════════════════════════════════════

def has_r2():
    """Verificar se R2 está configurado"""
    return all([R2_ACCESS_KEY, R2_SECRET_KEY, R2_BUCKET, R2_ENDPOINT, PUBLIC_BASE_URL])

def get_local_ip():
    """Obter IP local da Raspberry"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()

def get_s3():
    """Obter cliente S3 para Cloudflare R2"""
    return boto3.client(
        service_name="s3",
        endpoint_url=R2_ENDPOINT,
        aws_access_key_id=R2_ACCESS_KEY,
        aws_secret_access_key=R2_SECRET_KEY,
        region_name="auto",
    )

def public_url(filename):
    """Gerar URL pública do clipe"""
    return f"{PUBLIC_BASE_URL}/{filename}" if PUBLIC_BASE_URL else ""

def local_url(filename):
    """Gerar URL local do clipe"""
    return f"/clips/{filename}"

# ═══════════════════════════════════════════════════════════════════
#  FUNÇÕES DE CLIPES
# ═══════════════════════════════════════════════════════════════════

def list_clips():
    """Listar todos os clipes salvos"""
    clips = []
    files = sorted(
        glob.glob(os.path.join(OUTPUT_DIR, "*.mp4")),
        key=os.path.getmtime, reverse=True
    )
    
    for f in files:
        name = os.path.basename(f)
        mtime = os.path.getmtime(f)
        size_mb = round(os.path.getsize(f) / (1024 * 1024), 1)
        
        clips.append({
            "name": name,
            "time": datetime.fromtimestamp(mtime).strftime("%H:%M:%S"),
            "created_at": datetime.fromtimestamp(mtime).strftime("%d/%m/%Y %H:%M:%S"),
            "size_mb": size_mb,
            "local_url": local_url(name),
            "public_url": public_url(name),
            "url": public_url(name),
        })
    
    return clips

def save_clip():
    """Gerar clipe a partir do buffer"""
    segs = sorted(
        glob.glob(os.path.join(BUFFER_DIR, "seg_*.ts")),
        key=os.path.getmtime
    )
    
    if not segs:
        print("[CLIPE] Buffer vazio")
        return None
    
    # Calcular número de segmentos necessários
    n = max(1, (CLIP_DURATION + SEGMENT_DURATION - 1) // SEGMENT_DURATION)
    
    if len(segs) < n:
        print(f"[CLIPE] Buffer insuficiente: {len(segs)}/{n}")
        return None
    
    # Selecionar últimos n segmentos
    selected = segs[-n:]
    name = datetime.now().strftime("clip_%Y%m%d_%H%M%S.mp4")
    clip_path = os.path.join(OUTPUT_DIR, name)
    concat_file = os.path.join(BUFFER_DIR, "concat.txt")
    
    # Criar arquivo de concatenação
    with open(concat_file, "w", encoding="utf-8") as f:
        for seg in selected:
            f.write(f"file '{seg.replace(chr(92), '/')}'\n")
    
    # Executar FFmpeg
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-f", "concat", "-safe", "0", "-i", concat_file,
        "-c:v", "libx264", "-preset", "ultrafast",
        "-threads", "2",
        "-c:a", "aac",
        "-crf", "22", "-vf", "scale=1280:-2",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        "-y", clip_path,
    ]
    
    result = subprocess.run(cmd, capture_output=True, text=True)
    
    if result.returncode != 0:
        print(f"[CLIPE] FFmpeg error: {result.stderr.strip()}")
        return None
    
    print(f"[CLIPE] ✓ Salvo: {name}")
    return clip_path

# ═══════════════════════════════════════════════════════════════════
#  FUNÇÕES R2
# ═══════════════════════════════════════════════════════════════════

def upload_clip(file_path):
    """Fazer upload do clipe para R2"""
    if not has_r2():
        return ""
    
    try:
        name = os.path.basename(file_path)
        get_s3().upload_file(
            file_path, R2_BUCKET, name,
            ExtraArgs={
                "ContentType": "video/mp4",
                "CacheControl": "public, max-age=31536000"
            }
        )
        url = public_url(name)
        print(f"[R2] ✓ Upload: {url}")
        return url
    except Exception as e:
        print(f"[R2] Erro: {e}")
        return ""

def upload_index():
    """Atualizar índice de clipes no R2"""
    if not has_r2():
        return False
    
    try:
        data = [c for c in list_clips() if c["public_url"]]
        path = os.path.join(BASE_DIR, INDEX_JSON_NAME)
        
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        
        get_s3().upload_file(
            path, R2_BUCKET, INDEX_JSON_NAME,
            ExtraArgs={
                "ContentType": "application/json",
                "CacheControl": "no-cache"
            }
        )
        
        if CLIPS_INDEX_URL:
            print(f"[R2] ✓ Índice: {CLIPS_INDEX_URL}")
        return True
    except Exception as e:
        print(f"[R2] Erro no índice: {e}")
        return False

def upload_index_html():
    """Publicar index.html no R2"""
    if not has_r2() or not os.path.exists(INDEX_HTML_PATH):
        return
    
    try:
        get_s3().upload_file(
            INDEX_HTML_PATH, R2_BUCKET, "index.html",
            ExtraArgs={
                "ContentType": "text/html; charset=utf-8",
                "CacheControl": "no-cache"
            }
        )
        print("[R2] ✓ index.html publicado")
    except Exception as e:
        print(f"[R2] Erro: {e}")

# ═══════════════════════════════════════════════════════════════════
#  FUNÇÕES DE CAPTURA RTSP
# ═══════════════════════════════════════════════════════════════════

def start_capture():
    """Iniciar captura RTSP"""
    global capture_process
    
    # Limpar processo anterior
    if capture_process:
        try:
            capture_process.terminate()
            capture_process.wait(timeout=2)
        except:
            try:
                capture_process.kill()
            except:
                pass
    
    if not RTSP_URL:
        return
    
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

def capture_watchdog():
    """Monitorar captura RTSP"""
    while running:
        if not capture_process or capture_process.poll() is not None:
            if RTSP_URL:
                start_capture()
        time.sleep(5)

def cleanup_loop():
    """Limpar segmentos antigos do buffer"""
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

# ═══════════════════════════════════════════════════════════════════
#  CALLBACKS DO BOTÃO
# ═══════════════════════════════════════════════════════════════════

def on_button_single_click(duration):
    """Clique simples - Salvar clipe"""
    print(f"[BOTAO] ✓ CLIQUE ({duration:.0f}ms)")
    
    if not trigger_lock.acquire(blocking=False):
        print("[BOTAO] ⚠ Já existe gravação em curso")
        return
    
    try:
        path = save_clip()
        if path:
            url = upload_clip(path)
            upload_index()
            filename = os.path.basename(path)
            print(f"[BOTAO] ✅ Clipe: {filename}")
            if url:
                print(f"[BOTAO] 🌐 URL: {url}")
        else:
            print("[BOTAO] ❌ Falha ao gerar clipe")
    finally:
        trigger_lock.release()

def on_button_double_click(duration):
    """Duplo clique - Reservado para funcionalidade futura"""
    print(f"[BOTAO] 🔥 DUPLO CLIQUE ({duration:.0f}ms)")

# ═══════════════════════════════════════════════════════════════════
#  SERVIDOR HTTP
# ═══════════════════════════════════════════════════════════════════

class Handler(BaseHTTPRequestHandler):
    server_version = "ApertouGravou/2.0"
    
    def log_message(self, format, *args):
        pass  # Desabilitar logs padrão
    
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
            self._json({"error": "Não encontrado"}, 404)
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
        path = urlparse(self.path).path
        
        if path == "/" or path == "/index.html":
            self._file(INDEX_HTML_PATH, "text/html; charset=utf-8")
        elif path == "/api/status":
            segs = glob.glob(os.path.join(BUFFER_DIR, "seg_*.ts"))
            self._json({
                "status": "online" if (capture_process and capture_process.poll() is None) else "offline",
                "buffer_segments": len(segs),
                "buffer_seconds": len(segs) * SEGMENT_DURATION,
                "clips_saved": len(glob.glob(os.path.join(OUTPUT_DIR, "*.mp4"))),
                "arena": ARENA_NAME
            })
        elif path == "/api/clips":
            self._json(list_clips())
        elif path == "/api/config":
            self._json({
                "public_api": PUBLIC_API_URL,
                "media_url": PUBLIC_BASE_URL,
                "arena": ARENA_NAME
            })
        elif path.startswith("/clips/"):
            name = os.path.basename(path)
            self._file(os.path.join(OUTPUT_DIR, name))
        else:
            self._json({"error": "Rota não encontrada"}, 404)
    
    def do_POST(self):
        path = urlparse(self.path).path
        
        if path == "/api/save" or path == "/api/trigger":
            if not trigger_lock.acquire(blocking=False):
                self._json({"error": "Gravação em curso"}, 429)
                return
            
            try:
                print("[API] Requisição de gravação")
                clip_path = save_clip()
                if clip_path:
                    url = upload_clip(clip_path)
                    upload_index()
                    self._json({
                        "success": True,
                        "file": os.path.basename(clip_path),
                        "url": url
                    })
                else:
                    self._json({"error": "Falha ao gerar clipe"}, 500)
            finally:
                trigger_lock.release()
        else:
            self._json({"error": "Rota não encontrada"}, 404)
    
    def do_DELETE(self):
        path = urlparse(self.path).path
        
        if path.startswith("/api/clips/"):
            name = os.path.basename(path)
            clip_path = os.path.join(OUTPUT_DIR, name)
            if os.path.exists(clip_path):
                os.remove(clip_path)
                upload_index()
                self._json({"success": True})
            else:
                self._json({"error": "Arquivo não encontrado"}, 404)
        else:
            self._json({"error": "Rota não encontrada"}, 404)

def run_server():
    """Rodar servidor HTTP"""
    local_ip = get_local_ip()
    print(f"[SERVER] http://{local_ip}:{SERVER_PORT}")
    httpd = ThreadingHTTPServer(("0.0.0.0", SERVER_PORT), Handler)
    httpd.serve_forever()

def button_loop():
    """Loop do botão arcade"""
    while running:
        arcade_button.update()
        time.sleep(0.01)

def signal_handler(sig, frame):
    """Tratador de sinais de encerramento"""
    global running
    print("\n[SISTEMA] Encerrando...")
    running = False
    
    if capture_process:
        capture_process.terminate()
    
    if arcade_button and arcade_button.enabled:
        arcade_button.cleanup()
    
    sys.exit(0)

# ═══════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    print("\n" + "="*70)
    print(f"APERTOU GRAVOU - {ARENA_NAME}")
    print("="*70)
    
    # Inicializar botão
    arcade_button = ArcadeButton(gpio_pin=ARCADE_BUTTON_PIN)
    if ARCADE_BUTTON_ENABLED:
        arcade_button.setup()
        arcade_button.on_single_click = on_button_single_click
        arcade_button.on_double_click = on_button_double_click
    
    # Iniciar threads
    threading.Thread(target=capture_watchdog, daemon=True).start()
    threading.Thread(target=cleanup_loop, daemon=True).start()
    
    if arcade_button.enabled:
        threading.Thread(target=button_loop, daemon=True).start()
    
    # Publicar arquivos iniciais
    upload_index_html()
    upload_index()
    
    # Rodar servidor (bloqueante)
    run_server()
