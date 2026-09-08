import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'

// https://vitejs.dev/config/
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')

  return {
    plugins: [react()],
    server: {
      port: 5173,
      // `--host` publica el server en la LAN, que es como se abre esta app desde
      // el celular: `localhost` desde el teléfono es el teléfono.
      proxy: {
        '/api': {
          target: env.VITE_BACKEND_ORIGIN || 'http://127.0.0.1:8000',
          changeOrigin: true,
          secure: false,
        },
        // El flujo de "Vincular cuenta de Alexa" vive en la raíz del backend y
        // no bajo `/api`, porque el Redirect URI registrado en Amazon dice
        // `/auth/alexa/callback` y Amazon lo compara literal. Sin esta entrada,
        // en desarrollo el botón pega contra el server de Vite y da 404.
        //
        // No le pisa nada al frontend: la SPA no tiene router y sus llamadas de
        // login van a `/api/v1/auth/...`.
        '/auth': {
          target: env.VITE_BACKEND_ORIGIN || 'http://127.0.0.1:8000',
          changeOrigin: true,
          secure: false,
        },
      },
    },
  }
})
