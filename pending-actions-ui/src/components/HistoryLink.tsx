import { useState, type MouseEvent } from 'react'
import { viewCommunicationsUrl } from '../lib/links'

async function fetchViewComms(href: string): Promise<void> {
  const urls = [href, href.startsWith('http') ? href : `http://127.0.0.1:3175${href}`]
  let lastErr = 'Find-mail API is not running'
  for (const url of urls) {
    try {
      const res = await fetch(url, { cache: 'no-store' })
      const payload = (await res.json().catch(() => ({}))) as { error?: string; ok?: boolean }
      if (!res.ok || payload.ok === false) {
        lastErr = payload.error || `${res.status} from ${url}`
        continue
      }
      return
    } catch (err) {
      lastErr = err instanceof Error ? err.message : String(err)
    }
  }
  throw new Error(
    `${lastErr}. History is opened by the local API on 127.0.0.1:3175 (re-run tools/pending-actions-ui-server/install.sh).`,
  )
}

/** Export + open the communications ODT via HTTP so Cursor's browser does not
 * need the viewcomms:// helper (that scheme fails with "no application set"). */
export function HistoryLink({
  company,
  title,
  label = 'History',
  className = 'btn',
  titleAttr = 'Export and open full communications ODT for this lead',
}: {
  company?: string
  title?: string
  label?: string
  className?: string
  titleAttr?: string
}) {
  const href = viewCommunicationsUrl(company, title)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  if (!href) return null

  const onClick = async (e: MouseEvent<HTMLAnchorElement>) => {
    e.preventDefault()
    setBusy(true)
    setError('')
    try {
      await fetchViewComms(href)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <a className={className} href={href} onClick={onClick} title={titleAttr} aria-busy={busy}>
        {busy ? 'Opening…' : label}
      </a>
      {error ? <span className="find-mail-error">{error}</span> : null}
    </>
  )
}
