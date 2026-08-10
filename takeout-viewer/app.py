import sys
import webbrowser
from pathlib import Path
from server import create_server, server_state
from scanner import scan_takeout_directory

def main():
    port = 8000
    target_dir = None

    if len(sys.argv) > 1:
        target_dir = Path(sys.argv[1])
        if target_dir.exists():
            print(f"Escaneando directorio inicial: {target_dir}")
            server_state["current_path"] = str(target_dir.resolve())
            server_state["photos_data"] = scan_takeout_directory(target_dir)
        else:
            print(f"Advertencia: La ruta '{target_dir}' no existe. Puedes seleccionarla desde la interfaz web.")

    server = create_server(port=port)
    url = f"http://localhost:{port}"
    
    print(f"\nServidor iniciado en {url}")
    print("Abre tu navegador si no se abre automáticamente.\n")
    webbrowser.open(url)
    
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServidor detenido.")

if __name__ == "__main__":
    main()
