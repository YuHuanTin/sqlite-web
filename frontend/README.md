# sqlite-web frontend

Vue 3 + TypeScript app, served by sqlite-web itself at `/app/`. Sources are in
`src/`; nothing here is built on install.

## Development

From the repository root:

```console
$ uv run setup.py dev                          # defaults to ../db
$ uv run setup.py dev /path/to/other.db        # or name your own
```

That puts Vite on http://127.0.0.1:5173/ -- open that -- and a debug sqlite-web
on :8080, whose `/api` it proxies. Nothing is built: `-d` restarts the Python
side on every change, Vite hot-reloads these sources in place.

Vite only strips types, it does not check them -- run `npm run typecheck` (or
`npm run build`, which does) before trusting a change.

Editing a component, or a module a component imports, hot-replaces it in place.
Editing `store.ts` does not reload the page either, but it does re-run the
module: open tabs, selection and scroll reset to whatever the URL hash says.

Both servers listen on 127.0.0.1. `npm run dev -- --host` and sqlite-web's
`-H 0.0.0.0` open them to the LAN. To develop against a server on another port,
edit the proxy target in `vite.config.ts`.

The database is opened writable, so pass a copy if you are going to exercise
the write paths. For sqlite-web's own flags (`-r`, `-R`, ...) run the two
halves yourself: `sqlite_web -d -r /path/to/db.db` and `npm run dev`.

## Build

`npm run build` type-checks and writes the bundle to `../sqlite_web/static/vue/`,
where `sqlite_web/vue.py` serves it at `/app/`. That directory is git-ignored --
it is a build product, so a checkout that has never been built serves the
original HTML app instead. Build before packaging, or before copying the app to
another machine.
