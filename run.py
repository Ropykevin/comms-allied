"""Development server. Production uses Gunicorn (wsgi.py).

Binds to localhost only; set HOST=0.0.0.0 to expose it on your network. The interactive
debugger is enabled only when FLASK_DEBUG=1, because it allows running code on the machine.
"""
import os

from app import create_app

app = create_app(os.environ.get("APP_ENV", "development"))

if __name__ == "__main__":
    app.run(
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "5000")),
        debug=os.environ.get("FLASK_DEBUG") == "1",
    )
