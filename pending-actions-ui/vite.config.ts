import { networkInterfaces } from 'node:os'
import { defineConfig, type Plugin } from 'vite'
import react from '@vitejs/plugin-react'

const extraHosts = (process.env.PENDING_ACTIONS_UI_ALLOWED_HOSTS ?? '')
  .split(',').map(h => h.trim()).filter(Boolean)

function serverAddresses(): Set<string> {
  const addrs = new Set(['127.0.0.1', '::1'])
  for (const list of Object.values(networkInterfaces())) {
    for (const info of list ?? []) addrs.add(info.address)
  }
  return addrs
}

/** GET /__client-on-server → {"onServer": true} when the browser runs on this Mac.
 * The page hostname can't tell (HomeOffice and mini1 both browse mini1.local),
 * but the request's source address can. */
function clientOnServer(): Plugin {
  return {
    name: 'client-on-server',
    configureServer(server) {
      server.middlewares.use('/__client-on-server', (req, res) => {
        const remote = (req.socket.remoteAddress ?? '').replace(/^::ffff:/, '')
        res.setHeader('Content-Type', 'application/json')
        res.setHeader('Cache-Control', 'no-store')
        res.end(JSON.stringify({ onServer: serverAddresses().has(remote) }))
      })
    },
  }
}

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), clientOnServer()],
  server: {
    port: 3174,
    strictPort: true,
    allowedHosts: ['localhost', '127.0.0.1', ...extraHosts],
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:3175',
        changeOrigin: false,
      },
    },
  },
})
