// Mirrors greeting_line / greeting_name in job_tracker/pipeline/qualifying_reply.py
// (REQUIREMENTS.md DRAFT-1). Keep the two in step.

const TITLES = new Set(['dr', 'doctor', 'mr', 'mrs', 'ms', 'miss', 'prof', 'professor', 'sir', 'madam', 'mx', 'rev'])
// "Md" / "Mohd" at the start of a name abbreviates Mohammad; it is not a medical degree.
const GIVEN_ABBREV = new Set(['md', 'mohd'])
const GENERATIONAL = new Set(['jr', 'sr', 'ii', 'iii', 'iv'])
const DOCTORATES = new Set(['phd', 'dphil', 'do', 'dds', 'dmd', 'pharmd', 'edd', 'psyd', 'dvm', 'od', 'drph', 'dnp', 'mbbs'])

/** First line of a message when it is a greeting ("Hi Ann," / "Hello, Dr. Lee."). */
export const GREETING_LINE_RE = /^(Hi|Hello|Dear)\b[^\n]*/

function tokens(name: string): string[] {
  const out: string[] = []
  for (const raw of (name || '').replace(/,/g, ' ').split(/\s+/)) {
    if (raw.includes('@')) return []
    if (raw) out.push(raw)
  }
  return out
}

const bare = (t: string) => t.replace(/^\.+|\.+$/g, '').toLowerCase()

function isDoctorCredential(t: string): boolean {
  const compact = t.replace(/\./g, '')
  const key = compact.toLowerCase()
  if (DOCTORATES.has(key)) return true
  return key === 'md' && (compact === compact.toUpperCase() || t.includes('.'))
}

function isDoctor(name: string): boolean {
  const ts = tokens(name)
  if (!ts.length) return false
  if (bare(ts[0]) === 'dr' || bare(ts[0]) === 'doctor') return true
  return ts.some(isDoctorCredential)
}

function familyName(name: string): string {
  const ts = tokens(name)
  while (ts.length && TITLES.has(bare(ts[0]))) ts.shift()
  while (ts.length && (isDoctorCredential(ts[ts.length - 1]) || GENERATIONAL.has(bare(ts[ts.length - 1])))) ts.pop()
  while (ts.length > 1 && GIVEN_ABBREV.has(bare(ts[0]))) ts.shift()
  return ts.length ? ts[ts.length - 1].replace(/^\.+|\.+$/g, '') : ''
}

/** Given name: "Dr. Md Mostafizur Rahman" -> "Mostafizur". */
export function greetingName(name: string): string {
  const ts = tokens(name)
  while (ts.length && TITLES.has(bare(ts[0]))) ts.shift()
  while (ts.length > 1 && GIVEN_ABBREV.has(bare(ts[0]))) ts.shift()
  while (ts.length && (isDoctorCredential(ts[0]) || GENERATIONAL.has(bare(ts[0])))) ts.shift()
  return ts.length ? ts[0].replace(/^\.+|\.+$/g, '') : ''
}

/** How to address them: "Dr. Rahman" for a doctorate, otherwise the given name. */
export function addressName(name: string): string {
  if (isDoctor(name)) {
    const last = familyName(name)
    if (last) return `Dr. ${last}`
  }
  return greetingName(name)
}

/** "Hello, Dr. Rahman." for a doctorate, "Hi Kimber," otherwise, "Hi," with no name. */
export function greetingLine(name: string): string {
  const who = addressName(name)
  if (who.startsWith('Dr. ')) return `Hello, ${who}.`
  return who ? `Hi ${who},` : 'Hi,'
}

/** Same rule from an already-chosen address name ("Dr. Rahman" or "Kimber"). */
export function greetingLineFor(address: string): string {
  if (address.startsWith('Dr. ')) return `Hello, ${address}.`
  return address ? `Hi ${address},` : 'Hi,'
}
