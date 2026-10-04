"""Production WSGI entry point: gunicorn -c gunicorn.conf.py wsgi:app"""
import os

from app import create_app

app = create_app(os.environ.get("APP_ENV", "production"))
