"""Entry point for production servers.

    gunicorn wsgi:app            (Render, Railway, a VPS)
    PythonAnywhere: in the WSGI config file, add your project folder to
    sys.path and use:  from wsgi import app as application
"""
from app import app

if __name__ == "__main__":
    app.run()
