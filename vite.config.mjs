import { resolve } from 'node:path';
import { defineConfig } from 'vite';

const root = import.meta.dirname;
const source = name => resolve(root, 'mobile_server', 'static', name);

export default defineConfig({
  base: '/static/',
  build: {
    outDir: resolve(root, 'mobile_server', 'static_dist'),
    emptyOutDir: true,
    manifest: true,
    minify: 'oxc',
    target: ['chrome111', 'edge111', 'firefox114', 'safari16.4'],
    rollupOptions: {
      input: {
        style: source('style.css'),
        'interface-style': source('interface.css'),
        'chat-redesign': source('chat-redesign.css'),
        'chat-spacing': source('chat-spacing.css'),
        'mobile-media-guard-style': source('mobile-media-guard.css')
      },
      output: {
        entryFileNames: 'assets/[name]-[hash].js',
        chunkFileNames: 'assets/[name]-[hash].js',
        assetFileNames: 'assets/[name]-[hash][extname]'
      }
    }
  }
});
