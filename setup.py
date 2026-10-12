import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DATABASE = os.path.join(HERE, os.pardir, 'db')


def dev(databases):
    """Run the Vite dev server and a debug sqlite-web against the same files.

    Vite serves the frontend with hot reload from frontend/src and proxies /api
    to the server below, so nothing has to be built.
    """
    databases = databases or [DEFAULT_DATABASE]
    missing = [name for name in databases if not os.path.exists(name)]
    if missing:
        sys.exit(f'{missing[0]} does not exist. Usage: setup.py dev [database ...]')
    if shutil.which('npm') is None:
        sys.exit('npm is not on PATH; the frontend dev server needs Node.')

    # Vite's side of this pairing is in frontend/vite.config.ts, where it proxies
    # /api to port 8080 -- the two have to agree on the port.
    print('Frontend: http://127.0.0.1:5173/    API: http://127.0.0.1:8080/')
    vite = subprocess.Popen('npm run dev', cwd=os.path.join(HERE, 'frontend'), shell=True)
    try:
        subprocess.run([sys.executable, '-m', 'sqlite_web', '-d', *databases],
                       cwd=HERE, check=False)
    finally:
        if os.name == 'nt':
            subprocess.run(['taskkill', '/F', '/T', '/PID', str(vite.pid)],
                           capture_output=True, check=False)
        else:
            vite.terminate()


if __name__ == '__main__' and sys.argv[1:2] == ['dev']:
    dev(sys.argv[2:])
else:
    # The sdist is what a release is built from, and MANIFEST.in collects the
    # tree as it stands. Without this, a release silently installs an app whose
    # / is the old HTML UI. Not applied to a plain wheel build, so that
    # `pip install .` in a checkout without Node still works.
    if not os.path.exists(os.path.join(HERE, 'sqlite_web', 'static', 'vue', 'index.html')) \
            and 'sdist' in sys.argv[1:]:
        sys.exit('static/vue is missing. Run "cd frontend && npm run build" '
                 'before building a release.')

    # Deliberately not at the top: uv runs scripts without setuptools installed,
    # so importing it up there would make `dev` die before it starts.
    from setuptools import find_packages, setup

    setup(name='sqlite-web', packages=find_packages())
