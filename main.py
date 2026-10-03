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
from urllib.parse import urlparse, unquote

import boto3

try:
    import RPi.GPIO as GPIO
    GPIO_AVAILABLE = True
except ImportError:
    GPIO_AVAILABLE = False
    GPIO = None
    print("[AVISO] RPi.GPIO não disponível. Botão arcade desativado.")

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
OUTPUT_DIR = os.path.join(BASE_DIR, "clips")
BUFFER_DIR = os.path.join(BASE_DIR, "buffer")
INDEX_HTML_PATH = os.path.join(BASE_DIR, "index.html")
INDEX_JSON_PATH = os.path.join(BASE_DIR, "clips.json")

os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(BUFFER_DIR, exist_ok=True)


def load_config():
    if not os.path.exists(CONFIG_PATH):
        print("[CONFIG] config.json não encontrado. Usando variáveis de ambiente.")
        return {}
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as file:
            data = json.load(file)
        if not isinstance(data, dict):
            print("[CONFIG] config.json não contém um objeto JSON.")
            return {}
        return data
    except Exception as error:
        print(f"[CONFIG] Erro ao ler config.json: {error}")
        return {}


CONFIG = load_config()


def env_or_cfg(env_key, cfg_key, default=None):
    value = os.environ.get(env_key)
    if value is not None and str(value).strip() != "":
        return value
    value = CONFIG.get(cfg_key)
    return default if value is None else value


def normalize_url(value):
    return "" if not value else str(value).strip().rstrip("/")


def to_int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


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

running = True
capture_process = None
capture_lock = threading.Lock()
trigger_lock = threading.Lock()
r2_lock = threading.Lock()
http_server = None
arcade_button = None


def get_local_ip():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def safe_filename(filename):
    filename = os.path.basename(unquote(filename))
    return "" if filename in ("", ".", "..") else filename


def public_url(filename):
    return f"{PUBLIC_BASE_URL}/{filename}" if PUBLIC_BASE_URL else ""


def local_url(filename):
    return f"/clips/{filename}"


def file_size_mb(path):
    try:
        return round(os.path.getsize(path) / (1024 * 1024), 1)
    except OSError:
        return 0


def process_is_running():
    global capture_process
    return capture_process is not None and capture_process.poll() is None


def has_r2():
    return all((R2_ACCESS_KEY, R2_SECRET_KEY, R2_BUCKET, R2_ENDPOINT, PUBLIC_BASE_URL))


def get_s3():
    return boto3.client(
        service_name="s3",
        endpoint_url=R2_ENDPOINT,
        aws_access_key_id=R2_ACCESS_KEY,
        aws_secret_access_key=R2_SECRET_KEY,
        region_name="auto",
    )


def list_clips():
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


def write_local_index():
    try:
        with open(INDEX_JSON_PATH, "w", encoding="utf-8") as file:
            json.dump(list_clips(), file, ensure_ascii=False, indent=2)
        return True
    except Exception as error:
        print(f"[INDEX] Erro ao gerar clips.json: {error}")
        return False


def upload_index():
    write_local_index()
    if not has_r2():
        return False
    with r2_lock:
        try:
            get_s3().upload_file(
                INDEX_JSON_PATH,
                R2_BUCKET,
                "clips.json",
                ExtraArgs={
                    "ContentType": "application/json; charset=utf-8",
                    "CacheControl": "no-cache, no-store, must-revalidate",
                },
            )
            print("[R2] Índice atualizado.")
            return True
        except Exception as error:
            print(f"[R2] Falha ao enviar clips.json: {error}")
            return False


def upload_index_html():
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
            print("[R2] index.html publicado.")
            return True
        except Exception as error:
            print(f"[R2] Falha ao enviar index.html: {error}")
            return False


def upload_clip(file_path):
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
    if not has_r2():
        return False
    with r2_lock:
        try:
            get_s3().delete_object(Bucket=R2_BUCKET, Key=filename)
            print(f"[R2] Vídeo removido: {filename}")
            return True
        except Exception as error:
            print(f"[R2] Falha ao remover {filename}: {error}")
            return False


def get_buffer_segments():
    files = glob.glob(os.path.join(BUFFER_DIR, "seg_*.ts"))
    files.sort(key=lambda path: os.path.getmtime(path))
    return files


def cleanup_buffer():
    while running:
        try:
            segments = get_buffer_segments()
            while len(segments) > BUFFER_SEGMENTS:
                old_segment = segments.pop(0)
                try:
                    os.remove(old_segment)
                except FileNotFoundError:
                    pass
                except OSError as error:
                    print(f"[BUFFER] Não foi possível remover {old_segment}: {error}")
        except Exception as error:
            print(f"[BUFFER] Erro na limpeza: {error}")
        time.sleep(max(1, min(SEGMENT_DURATION, 5)))


def stop_capture():
    global capture_process
    with capture_lock:
        process = capture_process
        if process is None:
            return
        capture_process = None
        try:
            if process.poll() is None:
                print("[CAPTURA] Encerrando FFmpeg...")
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
        except Exception as error:
            print(f"[CAPTURA] Erro ao encerrar FFmpeg: {error}")


def start_capture():
    global capture_process
    with capture_lock:
        if not running:
            return False
        if process_is_running():
            return True

        print("[CAPTURA] Conectando à câmera RTSP...")
        output_pattern = os.path.join(BUFFER_DIR, "seg_%Y%m%d_%H%M%S.ts")
        gop_size = max(30, SEGMENT_DURATION * 30)

        command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "warning",
            "-rtsp_transport", "tcp",
            "-timeout", "10000000",
            "-rw_timeout", "10000000",
            "-i", RTSP_URL,
            "-map", "0:v:0",
            "-map", "0:a:0?",
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-threads", "2",
            "-crf", "22",
            "-g", str(gop_size),
            "-keyint_min", str(gop_size),
            "-sc_threshold", "0",
            "-c:a", "aac",
            "-b:a", "128k",
            "-f", "segment",
            "-segment_time", str(SEGMENT_DURATION),
            "-reset_timestamps", "1",
            "-strftime", "1",
            "-segment_format", "mpegts",
            output_pattern,
        ]

        try:
            capture_process = subprocess.Popen(
                command,
                stdout=subprocess.DEVNULL,
                stderr=None,
            )
            print(f"[CAPTURA] FFmpeg iniciado. PID={capture_process.pid}")
            return True
        except FileNotFoundError:
            print("[ERRO] FFmpeg não encontrado.")
            capture_process = None
            return False
        except Exception as error:
            print(f"[CAPTURA] Falha ao iniciar FFmpeg: {error}")
            capture_process = None
            return False


def wait_for_buffer(timeout=60):
    start = time.time()
    required = max(2, min(BUFFER_SEGMENTS, int(CLIP_DURATION / SEGMENT_DURATION) + 1))
    while running and time.time() - start < timeout:
        segments = get_buffer_segments()
        if len(segments) >= required:
            print(f"[CAPTURA] Buffer pronto: {len(segments)} segmentos.")
            return True
        time.sleep(1)
    print("[CAPTURA] Timeout aguardando buffer.")
    return False


def capture_watchdog():
    last_warning = 0
    while running:
        try:
            if not process_is_running():
                now = time.time()
                if now - last_warning > 10:
                    print("[WATCHDOG] FFmpeg está parado. Reiniciando captura...")
                    last_warning = now
                start_capture()
        except Exception as error:
            print(f"[WATCHDOG] Erro: {error}")
        time.sleep(5)


def build_clip_name():
    now = datetime.now()
    base = now.strftime("clip_%Y%m%d_%H%M%S")
    filename = f"{base}.mp4"
    counter = 1
    while os.path.exists(os.path.join(OUTPUT_DIR, filename)):
        filename = f"{base}_{counter:02d}.mp4"
        counter += 1
    return filename


def create_concat_file(segments):
    filename = os.path.join(
        BUFFER_DIR,
        f"concat_{os.getpid()}_{threading.get_ident()}.txt"
    )
    try:
        with open(filename, "w", encoding="utf-8") as file:
            for segment in segments:
                safe_path = os.path.abspath(segment).replace("\\", "/").replace("'", "'\\''")
                file.write(f"file '{safe_path}'\n")
        return filename
    except Exception:
        try:
            os.remove(filename)
        except OSError:
            pass
        raise


def save_clip():
    segments = get_buffer_segments()
    if len(segments) < 2:
        print("[CLIP] Buffer ainda não possui segmentos suficientes.")
        return None

    complete_segments = segments[:-1]
    required_segments = max(
        1,
        int((CLIP_DURATION + SEGMENT_DURATION - 1) / SEGMENT_DURATION)
    )

    if len(complete_segments) < required_segments:
        print(
            f"[CLIP] Buffer insuficiente: "
            f"{len(complete_segments)}/{required_segments} segmentos completos."
        )
        return None

    selected = complete_segments[-required_segments:]
    clip_name = build_clip_name()
    clip_path = os.path.join(OUTPUT_DIR, clip_name)
    concat_file = None

    print(f"[CLIP] Criando {clip_name}")

    try:
        concat_file = create_concat_file(selected)
        command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "warning",
            "-f", "concat",
            "-safe", "0",
            "-i", concat_file,
            "-map", "0:v:0",
            "-map", "0:a:0?",
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-threads", "2",
            "-crf", "22",
            "-vf", "scale=1280:-2",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-b:a", "128k",
            "-movflags", "+faststart",
            "-y", clip_path,
        ]

        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=max(120, CLIP_DURATION * 10),
        )

        if result.returncode != 0:
            print(f"[CLIP] FFmpeg falhou: {result.stderr.strip() if result.stderr else 'erro desconhecido'}")
            try:
                os.remove(clip_path)
            except OSError:
                pass
            return None

        if not os.path.exists(clip_path) or os.path.getsize(clip_path) < 1024:
            print("[CLIP] Arquivo gerado é inválido/vazio.")
            try:
                os.remove(clip_path)
            except OSError:
                pass
            return None

        print(f"[CLIP] Clipe criado: {clip_path} ({file_size_mb(clip_path)} MB)")
        return clip_path

    except subprocess.TimeoutExpired:
        print("[CLIP] Timeout ao gerar o clipe.")
        return None
    except Exception as error:
        print(f"[CLIP] Erro ao gerar clipe: {error}")
        return None
    finally:
        if concat_file:
            try:
                os.remove(concat_file)
            except OSError:
                pass


def process_trigger(source="desconhecido"):
    if not trigger_lock.acquire(blocking=False):
        print(f"[TRIGGER] Ignorado ({source}). Já existe uma gravação em processamento.")
        return {"success": False, "status": 429, "error": "Já existe uma gravação em curso."}

    try:
        print(f"[TRIGGER] Evento recebido: {source}")

        if not process_is_running():
            start_capture()
            time.sleep(1)

        clip_path = save_clip()
        if not clip_path:
            return {"success": False, "status": 500, "error": "Não foi possível gerar o clipe."}

        filename = os.path.basename(clip_path)
        public = upload_clip(clip_path)
        upload_index()

        return {
            "success": True,
            "status": 200,
            "file": filename,
            "filename": filename,
            "local_url": local_url(filename),
            "url": public,
            "public_url": public,
            "size_mb": file_size_mb(clip_path),
        }

    except Exception as error:
        print(f"[TRIGGER] Erro: {error}")
        return {"success": False, "status": 500, "error": str(error)}
    finally:
        trigger_lock.release()


class ArcadeButton:
    def __init__(self, gpio_pin, debounce_time=0.05, double_click_window=0.35):
        self.gpio_pin = gpio_pin
        self.debounce_time = debounce_time
        self.double_click_window = double_click_window
        self.enabled = False
        self.last_state = None
        self.is_pressed = False
        self.press_start_time = 0
        self.last_release_time = 0
        self.single_click_timer = None
        self.lock = threading.Lock()
        self.on_press = None
        self.on_release = None
        self.on_single_click = None
        self.on_double_click = None

    def setup(self):
        if not GPIO_AVAILABLE:
            print("[BOTAO] GPIO não disponível.")
            return False
        try:
            GPIO.setmode(GPIO.BCM)
            GPIO.setup(self.gpio_pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)
            self.last_state = GPIO.input(self.gpio_pin)
            self.enabled = True
            print(f"[BOTAO] GPIO {self.gpio_pin} configurado.")
            return True
        except Exception as error:
            print(f"[BOTAO] Erro ao configurar GPIO: {error}")
            self.enabled = False
            return False

    def _execute_single_click(self):
        with self.lock:
            self.single_click_timer = None
        if self.enabled and self.on_single_click:
            try:
                self.on_single_click()
            except Exception as error:
                print(f"[BOTAO] Erro no clique simples: {error}")

    def update(self):
        if not self.enabled:
            return
        try:
            current_state = GPIO.input(self.gpio_pin)
            if current_state == self.last_state:
                return

            time.sleep(self.debounce_time)
            current_state = GPIO.input(self.gpio_pin)

            if current_state == self.last_state:
                return

            self.last_state = current_state

            if current_state == GPIO.LOW:
                self.is_pressed = True
                self.press_start_time = time.time()
                if self.on_press:
                    self.on_press()
                return

            if current_state == GPIO.HIGH and self.is_pressed:
                self.is_pressed = False
                press_duration = time.time() - self.press_start_time
                now = time.time()

                if self.last_release_time > 0 and now - self.last_release_time <= self.double_click_window:
                    if self.single_click_timer:
                        self.single_click_timer.cancel()
                        self.single_click_timer = None
                    self.last_release_time = 0
                    if self.on_double_click:
                        self.on_double_click(press_duration)
                else:
                    self.last_release_time = now
                    if self.single_click_timer:
                        self.single_click_timer.cancel()
                    self.single_click_timer = threading.Timer(
                        self.double_click_window,
                        self._execute_single_click
                    )
                    self.single_click_timer.daemon = True
                    self.single_click_timer.start()

                if self.on_release:
                    self.on_release()

        except Exception as error:
            print(f"[BOTAO] Erro no processamento: {error}")

    def cleanup(self):
        if not GPIO_AVAILABLE:
            return
        try:
            if self.single_click_timer:
                self.single_click_timer.cancel()
                self.single_click_timer = None
            if self.enabled:
                GPIO.cleanup(self.gpio_pin)
            self.enabled = False
            print("[BOTAO] GPIO encerrado.")
        except Exception as error:
            print(f"[BOTAO] Erro ao limpar GPIO: {error}")


def on_button_press():
    print("[BOTAO] Pressionado.")


def on_button_release():
    pass


def on_button_single_click():
    print("[BOTAO] CLIQUE → Salvando lance...")
    result = process_trigger(source="botão")
    if result.get("success"):
        print("[BOTAO] CLIPE SALVO.")
        if result.get("public_url"):
            print(f"[BOTAO] URL: {result['public_url']}")
    else:
        print(f"[BOTAO] Falha: {result.get('error')}")


def on_button_double_click(duration):
    print(f"[BOTAO] DUPLO CLIQUE ({duration:.2f}s)")


def button_loop():
    print("[BOTAO] Loop iniciado.")
    while running:
        try:
            if arcade_button:
                arcade_button.update()
        except Exception as error:
            print(f"[BOTAO] Erro no loop: {error}")
        time.sleep(0.01)


class Handler(BaseHTTPRequestHandler):
    server_version = "ApertouGravou/3.0"

    def log_message(self, format, *args):
        return

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS, DELETE")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")

    def _json(self, data, status=200):
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
                    chunk = file.read(1024 * 1024)
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
            segments = get_buffer_segments()
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
            result = process_trigger(source="API")
            status = result.pop("status", 200)
            self._json(result, status)
            return

        self._json({"error": "Rota inexistente."}, 404)

    def do_DELETE(self):
        path = urlparse(self.path).path

        if not path.startswith("/api/clips/"):
            self._json({"error": "Rota inexistente."}, 404)
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


def run_server():
    global http_server
    local_ip = get_local_ip()

    print("")
    print("============================================================")
    print(" APERTOU GRAVOU")
    print("============================================================")
    print(f"[SERVER] http://{local_ip}:{SERVER_PORT}")
    print(f"[SERVER] Porta: {SERVER_PORT}")
    print(f"[SERVER] Arena: {ARENA_NAME}")
    print("============================================================")
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


def signal_handler(sig, frame):
    global running

    if not running:
        return

    print("")
    print("[SISTEMA] Encerrando...")
    running = False

    global http_server

    if http_server:
        try:
            http_server.shutdown()
        except Exception:
            pass

    if arcade_button:
        try:
            arcade_button.cleanup()
        except Exception:
            pass

    stop_capture()
    print("[SISTEMA] Encerrado.")


def main():
    global arcade_button

    print("")
    print("============================================================")
    print(" APERTOU GRAVOU")
    print(" Sistema de captura e replay")
    print("============================================================")
    print(f"[CONFIG] Arena: {ARENA_NAME}")
    print(f"[CONFIG] Segmento: {SEGMENT_DURATION}s")
    print(f"[CONFIG] Buffer: {BUFFER_SEGMENTS} segmentos")
    print(f"[CONFIG] Clipe: {CLIP_DURATION}s")
    print(f"[CONFIG] Porta: {SERVER_PORT}")
    print(f"[CONFIG] R2: {'ATIVO' if has_r2() else 'DESATIVADO'}")
    print(f"[CONFIG] GPIO: {'DISPONÍVEL' if GPIO_AVAILABLE else 'INDISPONÍVEL'}")
    print("============================================================")
    print("")

    arcade_button = ArcadeButton(gpio_pin=ARCADE_BUTTON_PIN)

    if ARCADE_BUTTON_ENABLED:
        if arcade_button.setup():
            arcade_button.on_press = on_button_press
            arcade_button.on_release = on_button_release
            arcade_button.on_single_click = on_button_single_click
            arcade_button.on_double_click = on_button_double_click
    else:
        print("[BOTAO] Desativado pela configuração.")

    start_capture()

    threading.Thread(target=capture_watchdog, name="capture-watchdog", daemon=True).start()
    threading.Thread(target=cleanup_buffer, name="buffer-cleanup", daemon=True).start()

    if arcade_button.enabled:
        threading.Thread(target=button_loop, name="button-loop", daemon=True).start()

    wait_for_buffer(timeout=60)

    write_local_index()

    if has_r2():
        upload_index_html()
        upload_index()

    run_server()


if __name__ == "__main__":
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        main()
    except Exception as error:
        print(f"[FATAL] {error}")
        signal_handler(None, None)
        sys.exit(1)
