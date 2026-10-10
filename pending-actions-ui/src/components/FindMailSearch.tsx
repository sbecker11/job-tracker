import { useState, type FormEvent, type MouseEvent } from 'react'
import { revealFolderUrl } from '../lib/links'
import { HistoryLink } from './HistoryLink'

export type FindMailHit = {
  source: string
  when: string
  from_address: string
  subject: string
  date: string
  folder: string
  snippet: string
  company: string
  title: string
  job_key: string
  contact_name: string
  status?: string
  comm_count?: number
  match_fields?: string[]
  folder_path?: string
  company_folder_path?: string
  reveal_url?: string
  hostinger_url: string
  lead_href: string
}

export type FindMailResult = {
  query: string
  imap: FindMailHit[]
  db: FindMailHit[]
  imap_error: string
}

async function fetchFindMail(query: string): Promise<FindMailResult> {
  const urls = [`/api/find-mail?q=${encodeURIComponent(query)}`, `http://127.0.0.1:3175/api/find-mail?q=${encodeURIComponent(query)}`]
  let lastErr = 'Find-mail API is not running'
  for (const url of urls) {
    try {
      const res = await fetch(url, { cache: 'no-store' })
      if (!res.ok) {
        lastErr = `${res.status} from ${url}`
        continue
      }
      return (await res.json()) as FindMailResult
    } catch (err) {
      lastErr = err instanceof Error ? err.message : String(err)
    }
  }
  throw new Error(
    `${lastErr}. Start: python scripts/find_mail_api.py  — or re-run tools/pending-actions-ui-server/install.sh`,
  )
}

function HitList({
  title,
  hits,
  empty,
  onOpenLead,
}: {
  title: string
  hits: FindMailHit[]
  empty: string
  onOpenLead?: (jobKey: string) => void
}) {
  return (
    <div className="find-mail-group">
      <h3>{title}</h3>
      {hits.length === 0 ? (
        <p className="find-mail-empty">{empty}</p>
      ) : (
        <ul>
          {hits.map((hit, i) => {
            const leadLabel = [hit.company, hit.title].filter(Boolean).join(' — ')
            const statusNote = hit.status ? ` (${hit.status})` : ''
            const folderHref = hit.reveal_url || revealFolderUrl(hit.folder_path) || revealFolderUrl(hit.company_folder_path)
            const openLead = (e: MouseEvent<HTMLAnchorElement>) => {
              if (!hit.job_key || !onOpenLead) return
              e.preventDefault()
              onOpenLead(hit.job_key)
            }
            return (
              <li key={`${hit.source}-${hit.job_key || hit.subject}-${hit.date}-${i}`}>
                <div className="find-mail-hit-head">
                  <strong>{leadLabel || hit.subject || '(no subject)'}</strong>
                  {hit.folder ? <span className="find-mail-folder">{hit.folder}</span> : null}
                </div>
                <div className="find-mail-meta">
                  {[
                    hit.from_address,
                    hit.contact_name,
                    hit.snippet,
                    hit.date,
                  ]
                    .filter(Boolean)
                    .join(' · ')}
                </div>
                <p className="find-mail-links">
                  {folderHref ? (
                    <a className="btn link" href={folderHref}>
                      Open folder
                    </a>
                  ) : null}
                  {hit.company && hit.title ? (
                    <HistoryLink company={hit.company} title={hit.title} className="btn link" />
                  ) : null}
                  {hit.job_key && hit.lead_href ? (
                    <a
                      className="btn link"
                      href={hit.lead_href}
                      onClick={openLead}
                      title="Jump to this lead in the list below"
                    >
                      Open lead{statusNote}
                    </a>
                  ) : null}
                  {hit.hostinger_url ? (
                    <a className="btn link" href={hit.hostinger_url} target="_blank" rel="noreferrer">
                      Open in Hostinger
                    </a>
                  ) : null}
                </p>
              </li>
            )
          })}
        </ul>
      )}
    </div>
  )
}

export function FindMailSearch({ onOpenLead }: { onOpenLead?: (jobKey: string) => void }) {
  const [query, setQuery] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [result, setResult] = useState<FindMailResult | null>(null)

  const onSubmit = async (e: FormEvent) => {
    e.preventDefault()
    const q = query.trim()
    if (q.length < 2) {
      setError('Type at least 2 characters.')
      setResult(null)
      return
    }
    setBusy(true)
    setError('')
    try {
      const payload = await fetchFindMail(q)
      setResult(payload)
      if (payload.imap_error) setError(payload.imap_error)
    } catch (err) {
      setResult(null)
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="find-mail" aria-label="Find JD email">
      <form className="find-mail-form" onSubmit={onSubmit}>
        <label htmlFor="find-mail-q">Find JD email</label>
        <div className="find-mail-row">
          <input
            id="find-mail-q"
            className="archive-search find-mail-input"
            type="search"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Any field — name, JD, notes, files, company, title…"
            autoComplete="off"
          />
          <button type="submit" className="btn link regen-btn" disabled={busy}>
            {busy ? 'Searching…' : 'Search'}
          </button>
        </div>
      </form>
      <p className="find-mail-hint">
        Every lead and status, every stored field, package documents, plus live Spexture IMAP (all folders).
      </p>
      {error && <p className="find-mail-error">{error}</p>}
      {result && (
        <div className="find-mail-results">
          <HitList
            title="On Spexture now (all IMAP folders)"
            hits={result.imap}
            empty="No IMAP match — not on Spexture, still delivering, or IMAP error above."
            onOpenLead={onOpenLead}
          />
          <HitList
            title="In the tracker (all leads, fields, and package files)"
            hits={result.db}
            empty="No stored lead, field, or package file matched — try another spelling."
            onOpenLead={onOpenLead}
          />
        </div>
      )}
    </section>
  )
}
