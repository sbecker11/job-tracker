import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

const extraHosts = (process.env.PENDING_ACTIONS_UI_ALLOWED_HOSTS ?? '')
  .split(',').map(h => h.trim()).filter(Boolean)

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    port: 3174,
    strictPort: true,
    allowedHosts: ['localhost', '127.0.0.1', ...extraHosts],
  },
})
