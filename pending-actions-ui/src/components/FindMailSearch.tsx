import { useState, type FormEvent } from 'react'

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

function HitList({ title, hits, empty }: { title: string; hits: FindMailHit[]; empty: string }) {
  return (
    <div className="find-mail-group">
      <h3>{title}</h3>
      {hits.length === 0 ? (
        <p className="find-mail-empty">{empty}</p>
      ) : (
        <ul>
          {hits.map((hit, i) => (
            <li key={`${hit.source}-${hit.folder}-${hit.subject}-${hit.date}-${i}`}>
              <div className="find-mail-hit-head">
                <strong>{hit.subject || '(no subject)'}</strong>
                {hit.folder ? <span className="find-mail-folder">{hit.folder}</span> : null}
              </div>
              <div className="find-mail-meta">
                {[hit.from_address, hit.contact_name, hit.date, hit.company && hit.title ? `${hit.company} — ${hit.title}` : hit.company || hit.title]
                  .filter(Boolean)
                  .join(' · ')}
              </div>
              {hit.snippet ? <p className="find-mail-snippet">{hit.snippet}</p> : null}
              <p className="find-mail-links">
                {hit.hostinger_url ? (
                  <a className="btn link" href={hit.hostinger_url} target="_blank" rel="noreferrer">
                    Open in Hostinger
                  </a>
                ) : null}
                {hit.lead_href ? (
                  <a className="btn link" href={hit.lead_href}>
                    Open lead
                  </a>
                ) : null}
              </p>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

export function FindMailSearch() {
  const [query, setQuery] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [result, setResult] = useState<FindMailResult | null>(null)

  const onSubmit = async (e: FormEvent) => {
    e.preventDefault()
    const q = query.trim()
    if (q.length < 2) {
      setError('Type at least 2 characters (name, email, or phone).')
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
            placeholder="Name, email, phone, company, or job title"
            autoComplete="off"
          />
          <button type="submit" className="btn link regen-btn" disabled={busy}>
            {busy ? 'Searching…' : 'Search'}
          </button>
        </div>
      </form>
      <p className="find-mail-hint">
        Live Spexture IMAP (Inbox + JobTracker) plus the tracker: recruiter name/email/phone, company, and job title.
      </p>
      {error && <p className="find-mail-error">{error}</p>}
      {result && (
        <div className="find-mail-results">
          <HitList
            title="On Spexture now (before or after the 3-min tick)"
            hits={result.imap}
            empty="No IMAP match — not on Spexture, still delivering, or IMAP error above."
          />
          <HitList
            title="In the tracker (after the tick)"
            hits={result.db}
            empty="No stored contact or conversation yet — the tick has not linked this mail."
          />
        </div>
      )}
    </section>
  )
}
