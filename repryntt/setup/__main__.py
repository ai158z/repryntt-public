"""Launch the setup wizard: python -m repryntt.setup"""

import sys
import threading
import webbrowser

from repryntt.setup.server import app

PORT = 9090


def _open_browser(port: int) -> None:
    webbrowser.open(f"http://localhost:{port}")


def _resolve_port(argv: list[str]) -> int:
    if len(argv) <= 1:
        return PORT
    try:
        return int(argv[1])
    except ValueError:
        print(f"Invalid port '{argv[1]}', falling back to {PORT}.")
        return PORT

if __name__ == "__main__":
    port = _resolve_port(sys.argv)
    threading.Timer(1.2, _open_browser, args=(port,)).start()
    print(f"\n  Repryntt Setup -> http://localhost:{port}\n")
    app.run(host="0.0.0.0", port=port, debug=False)
