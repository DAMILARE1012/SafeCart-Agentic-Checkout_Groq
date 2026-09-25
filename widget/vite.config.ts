import { fileURLToPath, URL } from 'node:url';
import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';

// The widget reads its build-time config (VITE_* only) from the single repo-root
// `.env`, the same file every backend service uses.
export default defineConfig(({ mode }) => ({
  envDir: fileURLToPath(new URL('..', import.meta.url)),
  plugins: [react()],
  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },
  // Library mode does not replace this automatically; React needs it to drop dev-only code.
  define:
    mode === 'production' ? { 'process.env.NODE_ENV': JSON.stringify('production') } : {},
  server: { port: 5173 },
  build: {
    // One self-contained file a merchant embeds with a single <script> tag.
    // CSS is imported with `?inline` and injected into the Shadow DOM, so no .css asset.
    lib: {
      entry: fileURLToPath(new URL('./src/embed.tsx', import.meta.url)),
      name: 'CommerceChat',
      formats: ['iife'],
      fileName: () => 'commerce-chat.js',
    },
    target: 'es2020',
    sourcemap: 'hidden', // for error-tracking uploads; not referenced by or served with the bundle
    emptyOutDir: true,
    // public/ holds dev-only mocks (MSW worker, fake Stripe page); never ship them.
    copyPublicDir: false,
  },
}));
