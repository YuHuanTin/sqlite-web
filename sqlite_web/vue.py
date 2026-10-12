"""
Serves the Vue app built into sqlite_web/static/vue/ (cd frontend && npm run build).

Everything is served from under /app/ rather than Flask's /static/: Vite's
relative base needs that to survive -u/--url-prefix, where an absolute
/static/... path would carry no prefix.
"""
import os

from flask import Blueprint, send_from_directory

bp = Blueprint('vue', __name__)

BUILT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static', 'vue')


@bp.get('/app/')
@bp.get('/app/<path:filename>')
def serve(filename='index.html'):
    return send_from_directory(BUILT_DIR, filename)


def is_mounted(app):
    return 'vue' in app.blueprints


def is_built():
    return os.path.exists(os.path.join(BUILT_DIR, 'index.html'))


def register(app, enabled=True):
    if not enabled or 'vue' in app.blueprints:
        return
    # Without this a checkout that has never been built mounts the route and
    # turns the first request into a 500.
    if not is_built():
        return
    app.register_blueprint(bp)
