import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// Three things here are load-bearing for shipping this as a Python package:
//
//   base          Relative, so the app works under sqlite-web's -u/--url-prefix
//                 without knowing what the prefix is. The shell is served at
//                 "<prefix>/app/" and its assets at "<prefix>/app/assets/...",
//                 which "./assets/x.js" resolves to either way. An absolute
//                 base would hard-code a path the prefix then breaks.
//   outDir        Straight into the package, where sqlite_web/vue.py serves it.
//                 It is a build product, so git ignores it: rebuilt by release,
//                 not source.
//   emptyOutDir   Vite refuses to clear a directory outside its root, so this
//                 has to be explicit or the build fails.
export default defineConfig({
  plugins: [vue()],
  base: './',
  build: {
    outDir: '../sqlite_web/static/vue',
    emptyOutDir: true,
    sourcemap: false,
  },
  server: {
    // Vite's default host is the name "localhost", which on Windows resolves to
    // ::1 alone -- 127.0.0.1:5173 would refuse connections. 127.0.0.1 also
    // matches the proxy target below, and `npm run dev -- --host` still
    // overrides this for the LAN.
    host: '127.0.0.1',
    // `npm run dev` serves the sources with HMR on :5173 and hands /api to a
    // sqlite-web on :8080, so the dev server sees the same-origin layout as a
    // built one. Edit this line to develop against a server somewhere else.
    proxy: {
      '/api': 'http://127.0.0.1:8080',
    },
  },
})
