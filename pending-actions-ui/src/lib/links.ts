/** Shared custom-URL helpers (Finder reveal, communications export) used by
 * both ArchivedLeadsPanel and DuplicateLeadsFolder — kept in one place so the
 * two never drift on how these links get built. */

/** Finder folder open (tools/reveal-folder/). */
export function revealFolderUrl(folderPath?: string): string | null {
  if (!folderPath) return null
  return `revealfolder://reveal?path=${encodeURIComponent(folderPath)}`
}

/** Export + open this lead's job_conversations ODT via the local API.
 * Cursor's embedded browser cannot launch viewcomms:// ("no application set"). */
export function viewCommunicationsUrl(company?: string, title?: string): string | null {
  if (!company?.trim() || !title?.trim()) return null
  return `/api/view-comms?company=${encodeURIComponent(company)}&title=${encodeURIComponent(title)}`
}

/**
 * shawn.becker@spexture.com's Gmail account-switcher slot (the "u/1" in
 * https://mail.google.com/mail/u/1/...) — matches how Shawn already signs into
 * this account in Chrome, so compose opens from the right sender.
 */
const RECRUITER_REPLY_ACCOUNT_SLOT = 1

/**
 * The recruiting-automation's tracked inbox (RECRUITING_GMAIL_USER in
 * reply_continuity.py). BCC'd on every recruiter-email send so the Sent-folder
 * scanner (comms_fast_cycle.py) can see this reply even though it's sent from
 * the Spexture account, not this inbox itself.
 */
const RECRUITER_REPLY_BCC = 'shawnbecker.recruiting@gmail.com'

/** Gmail compose in the browser (Chrome) from the Spexture account, BCC'ing the tracked inbox. */
export function gmailComposeUrl(to: string, subject: string, body: string): string {
  const params = new URLSearchParams({
    view: 'cm',
    fs: '1',
    tf: '1',
    to,
    bcc: RECRUITER_REPLY_BCC,
    su: subject,
    body,
  })
  return `https://mail.google.com/mail/u/${RECRUITER_REPLY_ACCOUNT_SLOT}/?${params.toString()}`
}

/** Query string for the tools/<name>/ URL-scheme helpers. Not URLSearchParams:
 * its "+" for spaces survives Swift's URLComponents.queryItems as a literal
 * "+", so a normalized key like "acme::data engineer" would never match. */
export function helperQuery(params: Record<string, string | undefined>): string {
  return Object.entries(params)
    .filter(([, v]) => v != null && v !== '')
    .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(v!)}`)
    .join('&')
}

/** Lead status change via tools/set-lead-status/ (`set-lead-status`). */
export function leadStatusUrl(
  key: string,
  status: string,
  reason: string,
  sent?: { channel: string; message: string },
): string {
  return `leadstatus://set?${helperQuery({
    key,
    status,
    reason: reason.trim(),
    sent_channel: sent?.channel,
    sent_message: sent?.message.trim(),
  })}`
}

/** Decline a lead via tools/decline-lead/ (`decline-lead`): skipped + reason, optional sent letter. */
export function declineUrl(
  key: string,
  reason: string,
  note: string,
  opts: { channel?: string; letter: boolean; letterText?: string },
): string {
  return `declinelead://decline?${helperQuery({
    key,
    reason,
    note: note.trim(),
    channel: opts.letter ? opts.channel : undefined,
    letter: opts.letter ? undefined : '0',
    letter_text: opts.letter ? opts.letterText?.trim() : undefined,
  })}`
}

/** Gmail search in the Spexture account — last resort when a lead has no thread or message link. */
export function gmailSearchUrl(query: string): string {
  return `https://mail.google.com/mail/u/${RECRUITER_REPLY_ACCOUNT_SLOT}/#search/${encodeURIComponent(query)}`
}

export interface SenderInfo {
  /** The recruiter's message — its signature often has email / phone / LinkedIn. */
  messageText?: string
  name?: string
  /** Real inbox only — LinkedIn relay addresses find nothing. */
  email?: string
  phone?: string
  company?: string
  title?: string
}

/** Drops placeholder company/title text ("Role TBD", "(end-client TBD)"). */
function cleanField(value: string | undefined): string {
  const v = (value || '')
    .replace(/\s*\([^)]*\b(?:tbd|unknown)\b[^)]*\)/gi, '')
    .trim()
  return /^(?:role\s+)?(?:tbd|unknown|n\/a)$/i.test(v) ? '' : v
}

const OWN_EMAIL_RE = /spexture\.com$|^shawnbecker\.recruiting@|^scbboston@/i
const SYSTEM_EMAIL_RE = /linkedin\.com$|no-?reply|notifications?@|mailer-daemon|donotreply/i
const OWN_PHONE_DIGITS = '8578910896'

/** Email / phone / LinkedIn profile found in a recruiter's message (usually the
 * signature), skipping Shawn's own details and system addresses. */
export function extractSenderDetails(text: string): { email: string; phone: string; linkedin: string } {
  const email =
    (text.match(/[\w.+-]+@[\w-]+(?:\.[\w-]+)+/g) || []).find(
      (e) => !OWN_EMAIL_RE.test(e) && !SYSTEM_EMAIL_RE.test(e),
    ) || ''
  const phone =
    (text.match(/(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}\b/g) || []).find(
      (p) => !p.replace(/\D/g, '').endsWith(OWN_PHONE_DIGITS),
    ) || ''
  const linkedin = (text.match(/https?:\/\/(?:[a-z]{2,3}\.)?linkedin\.com\/in\/[\w%-]+\/?/i) || [''])[0]
  return { email: email.replace(/[.,;]+$/, ''), phone: phone.trim(), linkedin }
}

const quoted = (s: string) => `"${s}"`
const googleUrl = (q: string) => `https://www.google.com/search?q=${encodeURIComponent(q)}`

/** "Search for sender" links — one per kind of search the known fields support. */
export function senderSearchLinks(sender: SenderInfo): { label: string; href: string }[] {
  const name = (sender.name || '').trim()
  const email = (sender.email || '').trim()
  const phone = (sender.phone || '').trim()
  const company = cleanField(sender.company)
  const title = cleanField(sender.title)
  const links: { label: string; href: string }[] = []
  if (email) links.push({ label: `Gmail: ${email}`, href: gmailSearchUrl(email) })
  else if (name) links.push({ label: `Gmail: ${name}`, href: gmailSearchUrl(quoted(name)) })
  if (name) {
    links.push({
      label: 'LinkedIn: sender',
      href: `https://www.linkedin.com/search/results/people/?keywords=${encodeURIComponent(
        [name, company].filter(Boolean).join(' '),
      )}`,
    })
    links.push({ label: 'Web: sender', href: googleUrl([quoted(name), company].filter(Boolean).join(' ')) })
  }
  if (email) links.push({ label: 'Web: email', href: googleUrl(quoted(email)) })
  if (phone) links.push({ label: 'Web: phone', href: googleUrl(quoted(phone)) })
  if (company || title) {
    links.push({ label: 'Web: company + role', href: googleUrl([company, title, 'job'].filter(Boolean).join(' ')) })
    links.push({
      label: 'LinkedIn Jobs: role',
      href: `https://www.linkedin.com/jobs/search/?keywords=${encodeURIComponent(
        [title, company].filter(Boolean).join(' '),
      )}`,
    })
  }
  return links
}

/** How to reach the recruiter for a message sent from Manage Lead. */
export interface MessageTarget {
  channel: 'linkedin' | 'email' | 'other'
  threadUrl?: string
  /** Real recruiter inbox (not a LinkedIn relay) for Gmail compose. */
  composeTo?: string
  /** Recruiter's last email, opened in Gmail (reply from there). */
  gmailUrl?: string
  /** Recruiter's LinkedIn profile (Save contact). */
  linkedinUrl?: string
  /** Company inbox (Save contact → Company) — offered only when no recruiter owns the lead. */
  companyEmail?: string
  companyContactName?: string
  /** A recruiter / agency contact owns this lead (server-side). */
  recruiterOwned?: boolean
  /** What's known about the sender, for "Search for sender" and route labels. */
  sender: SenderInfo
  subject: string
  /** False when there's no recruiter at all — Decline can only skip. */
  hasRecruiter: boolean
  /** Where an address was found when it isn't on the lead itself ("their signature"). */
  via?: string
}

export interface SendRoute {
  channel: 'email' | 'linkedin' | 'other'
  /** "Send message to Karth <k@ngt.com> for North Grand Technology — Data Engineer" */
  label: string
  /** Main button text — "Email Priyanshu", "Reply on LinkedIn", "Text Karth". */
  button: string
  /** How it's sent, for the Send to line: "email", "LinkedIn thread", "text". */
  how: string
  /** Address / number / link the message goes to (shown and copyable). */
  to: string
  /** Step 2 of the card: what happens on click and what's left to do. */
  step: string
  /** True: the link opens with the message filled in. False: copy it first, then paste. */
  prefilled: boolean
  /** True: the SendText helper confirms, sends and logs it — no Message sent click. */
  sendsAndLogs?: boolean
  href: (text: string) => string
}

function firstName(name: string): string {
  const n = name.trim()
  if (!n || n.includes('@')) return n
  return n.split(/\s+/)[0]
}

/** Every way to reach the sender, best first: recruiter email (Gmail draft with
 * the message filled in), LinkedIn thread, LinkedIn profile, the recruiter's
 * email thread, the company inbox (only when no recruiter owns the lead, so an
 * agency's submission is never bypassed), then a text to their phone. */
export function sendRoutes(t: MessageTarget): SendRoute[] {
  const found = extractSenderDetails(t.sender.messageText || '')
  if (!t.composeTo && found.email) t = { ...t, composeTo: found.email }
  if (!t.linkedinUrl && found.linkedin) t = { ...t, linkedinUrl: found.linkedin }
  const name = (t.sender.name || '').trim()
  const first = firstName(name)
  const company = cleanField(t.sender.company)
  const title = cleanField(t.sender.title)
  const forPart =
    company && title ? ` for ${company} — ${title}` : company ? ` for ${company}` : title ? ` for ${title}` : ''
  const who = name || 'the recruiter'
  const routes: SendRoute[] = []
  if (t.composeTo) {
    const to = t.composeTo
    const button = `Email ${first || to}`
    routes.push({
      channel: 'email',
      label: `Send message to ${name && name.toLowerCase() !== to.toLowerCase() ? `${name} <${to}>` : to}${forPart}`,
      button,
      how: 'email',
      to,
      step: `Click "${button}". Gmail opens with To, Subject and the message filled in. Press Send.`,
      prefilled: true,
      href: (text) => gmailComposeUrl(to, t.subject, text),
    })
  }
  if (t.threadUrl) {
    const url = t.threadUrl
    const li = /linkedin\.com/i.test(url)
    const button = li ? 'Reply on LinkedIn' : 'Reply in the thread'
    routes.push({
      channel: li ? 'linkedin' : t.channel,
      label: `Send message in the ${li ? 'LinkedIn ' : ''}thread with ${who}${forPart}`,
      button,
      how: li ? 'LinkedIn thread' : 'thread',
      to: url,
      step: `Click "${button}". The thread opens and the message is copied. Paste it and press Send.`,
      prefilled: false,
      href: () => url,
    })
  }
  if (t.linkedinUrl) {
    const url = t.linkedinUrl
    const button = `Message ${first || 'them'} on LinkedIn`
    routes.push({
      channel: 'linkedin',
      label: `Send message to ${who} on LinkedIn${forPart}`,
      button,
      how: 'LinkedIn profile',
      to: url,
      step: `Click "${button}". Their profile opens and the message is copied. Click Message, paste it and press Send.`,
      prefilled: false,
      href: () => url,
    })
  }
  if (t.gmailUrl) {
    const url = t.gmailUrl
    const button = 'Reply in Gmail'
    routes.push({
      channel: 'email',
      label: `Reply in the email thread with ${who}${forPart}`,
      button,
      how: 'email thread',
      to: url,
      step: `Click "${button}". The email thread opens and the message is copied. Click Reply, paste it and press Send.`,
      prefilled: false,
      href: () => url,
    })
  }
  if (t.companyEmail && !t.recruiterOwned) {
    const to = t.companyEmail
    const org = (t.companyContactName || company || to).trim()
    const button = `Email ${company || to}`
    routes.push({
      channel: 'email',
      label: `Send message to ${org} <${to}> about ${title || 'the role'}`,
      button,
      how: 'email',
      to,
      step: `Click "${button}". Gmail opens with To, Subject and the message filled in. Press Send.`,
      prefilled: true,
      href: (text) => gmailComposeUrl(to, t.subject, text),
    })
  }
  const phone = (t.sender.phone || found.phone || '').trim()
  if (phone) {
    const button = `Text ${first || phone}`
    routes.push({
      channel: 'other',
      label: `Text ${who} at ${phone}${forPart}`,
      button,
      how: 'text',
      to: phone,
      step: `Click "${button}". Check the message in the window that opens and click Send — it goes out from Messages and is logged on the lead.`,
      prefilled: true,
      sendsAndLogs: true,
      href: () => smsUrl(phone),
    })
  }
  return routes
}

/** SendText helper: confirm window → Messages → logged via set-lead-status (or decline-lead). */
export function sendTextUrl(
  key: string,
  to: string,
  name: string,
  message: string,
  after: { status: string } | { declineReason: string; declineNote?: string },
): string {
  const q: Record<string, string> = { key, to, name, message }
  if ('status' in after) q.status = after.status
  else {
    q.decline_reason = after.declineReason
    if (after.declineNote) q.decline_note = after.declineNote
  }
  return `sendtext://send?${helperQuery(q)}`
}

/** RecordReply helper: confirm the reply sent on LinkedIn → record-unmatched-reply. */
export const recordReplyUrl = (messageId: string, name: string, text: string, channel = 'linkedin') =>
  `recordreply://record?${helperQuery({ message_id: messageId, name, text, channel })}`

export const smsUrl = (phone: string) => `sms:${phone.replace(/[^\d+]/g, '')}`
export const callUrl = (phone: string) => `tel:${phone.replace(/[^\d+]/g, '')}`

/** Step 2 when no route was found — the Send to box in Manage lead has everything to copy. */
export const NO_ROUTE_STEP =
  'No email, LinkedIn or phone found. Copy the Send to details and the message from Manage lead into your email or LinkedIn, then press Send.'

/** Save a contact found via Search for sender (set-lead-status --contact-*). */
export function saveContactUrl(
  key: string,
  c: { name: string; email: string; phone: string; linkedin: string; role: string },
): string {
  return `leadstatus://set?${helperQuery({
    key,
    contact_name: c.name.trim(),
    contact_email: c.email.trim(),
    contact_phone: c.phone.trim(),
    contact_linkedin: c.linkedin.trim(),
    contact_role: c.role,
  })}`
}

export function formatDecidedAt(iso?: string): string {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso.slice(0, 10) || '—'
  return d.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' })
}

/** Real HTML id for a lead's row, used as a genuine `<a href="#...">`
 * anchor target — every row that can be jumped to (Contact priority,
 * Decide/apply, Archived, and each duplicate row on the Duplicates skipped
 * tab) gets one, via this single shared function, so an href built on one
 * side always matches the id rendered on the other. normalizedKeys look
 * like "company::title", not valid as a raw HTML id, so non-id-safe
 * characters are collapsed to `-`. */
export function leadAnchorId(normalizedKey: string): string {
  return `lead-${normalizedKey.replace(/[^a-zA-Z0-9_-]/g, '-')}`
}

/** `href` for a real anchor link to a lead's row — combine with an onClick
 * that does the actual tab-switch (see App.tsx's onViewDuplicates /
 * onJumpToSurvivor) since the target only exists in the DOM once the
 * right tab is showing; the plain hash href still makes the link a real,
 * inspectable, right-click-able anchor rather than a JS-only button. */
export function leadAnchorHref(normalizedKey: string): string {
  return `#${leadAnchorId(normalizedKey)}`
}

/** Scrolls to and briefly flashes the row for `normalizedKey` — used after
 * switching tabs, or opening the Archived <details> (see App.tsx's
 * onJumpToSurvivor/onViewDuplicates) to land on a lead that's now visible.
 *
 * 2026-08-17: an earlier version of duplicate-lead navigation tried to
 * reach into a *closed* `<details>` element via `scrollIntoView` — that
 * doesn't reliably work (closed <details> content has no layout box until
 * opened, and browsers differ on auto-expanding for fragment navigation).
 * The fix was architectural for the Duplicates-skipped case ("Duplicates
 * skipped" became its own top-level tab instead of a nested folder), but
 * the *target* can still be the "Archived / decided leads" <details> below
 * the tabs (a survivor jump can land there — see locateLeadTab's 'archived'
 * branch), and *that* re-render can take more than one frame to settle:
 * opening it + ArchivedLeadsPanel's own filter-reset effect are two
 * separate state updates, not one. A fixed double-rAF can fire in the gap
 * between them and find a not-yet-laid-out (zero-size) element and
 * silently no-op. Polling for an actually-laid-out element (non-empty
 * getClientRects) up to ~20 frames (~one-third of a second) instead of
 * guessing exactly how many re-renders are involved fixes that without the
 * caller needing to know that stage-and-filter-reset happens at all.
 *
 * 2026-08-18: switched `behavior: 'smooth'` → `'auto'` (instant) after
 * confirming the *actual* bug behind "the Duplicate of link does nothing"
 * — with 1200+ archived rows, the target can be tens of thousands of
 * pixels down the page, and a smooth scroll across that distance took
 * ~2.2s in testing. The `.lead-highlight` flash (App.css) only lasts 2.5s
 * *from click time*, before the scroll even starts — so by the time the
 * page finished crawling down, the highlight had already faded (or was
 * about to), leaving the user staring at an unmarked row indistinguishable
 * from 1000+ others. It scrolled, technically, but looked completely
 * broken. Jumping instantly removes that race entirely regardless of list
 * size or which browser's smooth-scroll timing is in play. Also updates
 * the URL hash (via replaceState, not a real navigation) once landed, so
 * the address bar gives visible confirmation the click did something —
 * restores the "real, inspectable anchor" behavior `leadAnchorHref` was
 * meant to have (bookmarkable, right-click-able) instead of a silently
 * JS-only jump.
 *
 * 2026-08-18 (later same day): the 20-frame (~330ms) polling budget above
 * turned out to be too short for the "survivor already fully decided, so
 * open the 1200+-row Archived panel" path — confirmed live: highlightKey
 * was correctly set (visible as the flashed row once you scrolled/zoomed
 * out to find it yourself) but the *actual* scroll never fired, because
 * that panel's <details> opening plus its own filter-reset re-render of
 * every archived row settled in ~650ms — comfortably past 20 frames'
 * worth of polling, so `tryScroll` silently exhausted its attempts and
 * gave up just short of the row being ready. Switched to a wall-clock
 * deadline (2s) instead of a fixed frame count — rAF cadence isn't
 * guaranteed to be ~16ms (a backgrounded/throttled tab can be much
 * slower), so counting frames was measuring the wrong thing regardless of
 * how many were allowed. */
export function scrollToLeadRow(normalizedKey: string): void {
  const id = leadAnchorId(normalizedKey)
  const deadline = performance.now() + 2_000
  const tryScroll = () => {
    const el = document.getElementById(id)
    if (el && el.getClientRects().length > 0) {
      el.scrollIntoView({ behavior: 'auto', block: 'center' })
      if (window.location.hash !== `#${id}`) {
        window.history.replaceState(null, '', `#${id}`)
      }
      return
    }
    if (performance.now() < deadline) requestAnimationFrame(tryScroll)
  }
  requestAnimationFrame(tryScroll)
}
