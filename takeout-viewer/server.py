import json
import mimetypes
import os
import re
import urllib.parse

from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from scanner import scan_takeout_directory
from threading import Lock

# Estado global compartido
server_state = {
    "photos_data": [],
    "current_path": None,
    "progress": {"active": False, "message": ""}
}
state_lock = Lock()

class TakeoutHTTPHandler(BaseHTTPRequestHandler):
    static_dir = Path(__file__).parent / "static"

    def handle_one_request(self):
        """Las cancelaciones del navegador no son errores del servidor."""
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True
            return
        except OSError as error:
            if getattr(error, "winerror", None) in (10053, 10054):
                self.close_connection = True
                return
            raise

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        
        if parsed.path == "/":
            html_path = self.static_dir / "index.html"
            if html_path.exists():
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                with open(html_path, "rb") as f:
                    self.wfile.write(f.read())
            else:
                self.send_error(404, "No se encontró index.html")

        elif parsed.path == "/api/status":
            with state_lock:
                self._send_json({
                    "current_path": server_state["current_path"],
                    "total_items": len(server_state["photos_data"]),
                    "years": sorted({p["year"] for p in server_state["photos_data"] if p.get("year")}, reverse=True)
                })

        elif parsed.path == "/api/progress":
            with state_lock:
                self._send_json(server_state["progress"])

        elif parsed.path == "/api/photos":
            query = urllib.parse.parse_qs(parsed.query)
            page = int(query.get("page", [0])[0])
            limit = int(query.get("limit", [50])[0])
            filter_type = query.get("type", ["all"])[0]
            filter_year = query.get("year", ["all"])[0]

            photos = server_state["photos_data"]
            if filter_type == "image":
                filtered = [p for p in photos if not p["is_video"]]
            elif filter_type == "video":
                filtered = [p for p in photos if p["is_video"]]
            else:
                filtered = photos

            if filter_year != "all":
                filtered = [p for p in filtered if str(p.get("year")) == filter_year]

            start, end = page * limit, (page + 1) * limit
            self._send_json(filtered[start:end])

        elif parsed.path == "/api/browse-folder":
            folder_selected = ""
            try:
                import tkinter as tk
                from tkinter import filedialog
                root = tk.Tk()
                root.withdraw()
                root.attributes("-topmost", True)
                folder_selected = filedialog.askdirectory(title="Seleccionar carpeta de Google Takeout / ZIPs")
                root.destroy()
            except Exception as e:
                print(f"Tkinter no disponible: {e}")

            self._send_json({"path": folder_selected})

        elif parsed.path == "/file":
            query = urllib.parse.parse_qs(parsed.query)
            file_path = query.get("path", [None])[0]
            
            if file_path and os.path.exists(file_path):
                self._serve_file(file_path)
            else:
                self.send_error(404, "Archivo no encontrado")
        else:
            self.send_error(404)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        
        if parsed.path == "/api/set-directory":
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length).decode('utf-8')
            
            try:
                data = json.loads(body)
                raw_path = data.get("path", "").strip()
                target_path = Path(raw_path)

                if not raw_path or not target_path.exists():
                    self._send_json({"success": False, "error": f"La ruta '{raw_path}' no existe o no es válida."}, status=400)
                    return

                def set_progress(message):
                    with state_lock:
                        server_state["progress"] = {"active": True, "message": message}

                set_progress("Preparando la carga...")
                print(f"\n[Cambiando directorio] Nueva ruta: {target_path}")
                items = scan_takeout_directory(target_path, set_progress)
                
                with state_lock:
                    server_state["current_path"] = str(target_path.resolve())
                    server_state["photos_data"] = items
                    server_state["progress"] = {"active": False, "message": ""}

                self._send_json({
                    "success": True,
                    "current_path": server_state["current_path"],
                    "total_items": len(items),
                    "years": sorted({p["year"] for p in items if p.get("year")}, reverse=True)
                })
            except Exception as e:
                with state_lock:
                    server_state["progress"] = {"active": False, "message": ""}
                self._send_json({"success": False, "error": str(e)}, status=500)

        else:
            self.send_error(404)

    def _send_json(self, data, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode("utf-8"))

    def _serve_file(self, file_path):
        """Sirve archivos multimedia por segmentos para permitir reproducción inmediata."""
        file_size = os.path.getsize(file_path)
        mime_type, _ = mimetypes.guess_type(file_path)
        range_header = self.headers.get("Range")
        start, end = 0, file_size - 1
        status = 200

        if range_header:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip())
            if not match:
                self.send_error(416, "Rango no válido")
                return

            start_text, end_text = match.groups()
            if start_text:
                start = int(start_text)
                if start >= file_size:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{file_size}")
                    self.end_headers()
                    return
            elif end_text:
                start = max(file_size - int(end_text), 0)

            if end_text and start_text:
                end = min(int(end_text), file_size - 1)
            if end < start:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{file_size}")
                self.end_headers()
                return
            status = 206

        content_length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", mime_type or "application/octet-stream")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(content_length))
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
        self.end_headers()

        with open(file_path, "rb") as file_handle:
            file_handle.seek(start)
            remaining = content_length
            while remaining:
                chunk = file_handle.read(min(64 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def log_message(self, format, *args):
        return

def create_server(port: int = 8000) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(('localhost', port), TakeoutHTTPHandler)
    # Las conexiones de medios canceladas no deben retener el cierre de la app.
    server.daemon_threads = True
    return server
