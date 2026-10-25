/** Quality check run by "Copy … message" before anything is copied (REQUIREMENTS DRAFT-10).
 * Rule-based, no LLM: house rules from ~/CLAUDE.md §4 plus a read of the
 * recruiter's latest message, so the reply neither re-asks what they answered
 * nor skips what they asked. */

export type IssueLevel = 'rule' | 'suggest'

export interface QualityIssue {
  id: string
  level: IssueLevel
  message: string
  suggestion: string
  fix?: (text: string) => string
}

export interface QualityContext {
  /** The recruiter's latest message (empty when none was received). */
  theirMessage?: string
  /** First name the greeting should use. */
  firstName?: string
  kind: 'reply' | 'follow-up' | 'decline'
}

const SIGNATURE = 'Best, Shawn\nshawn.becker@spexture.com'
const CLOSING_RE = /\bBest(?: regards)?,\s*Shawn(?: Becker)?\b/i

const COMP_RE = /\$\s?\d|\b\d{2,3}\s*(?:\/|per\s+)(?:hr|hour)\b|\b\d{2,3}\s?k\b(?!\w)/i
const WORK_AUTH_RE =
  /\b(?:u\.?s\.? citizen|citizenship|green card|visa|sponsor(?:ship)?|h-?1b|work authori[sz]ation|authori[sz]ed to work|security clearance|clearance)\b/i
const AVAILABILITY_RE = /\b(?:available (?:to start|immediately|starting)|start date|notice period|can start (?:on|in|immediately))\b/i
const BANNED: [RegExp, string][] = [
  [/\bSpexture LLC\b/i, 'Use "Spexture" or "Spexture (Independent Consulting)" — never "Spexture LLC".'],
  [/\bsub-100ms\b/i, 'Remove "sub-100ms".'],
  [/\bCline\b/, 'Remove "Cline".'],
  [/\bMember Nav\b/i, 'Remove "Member Nav".'],
  [/\bCambria\b/i, 'The name is "Cambia Health Solutions".'],
]
const PLACEHOLDER_RE = /<[^>\n]{1,40}>|\[[^\]\n]{1,40}\]|\bTBD\b|\bXXX\b/

const ASKS = {
  endClient: /\b(?:end client|hiring company|who is the client)\b[^?\n]*\?/i,
  remote: /\b(?:fully remote|on-?site|hybrid)\b[^?\n]*\?/i,
  engagement: /\bW-?2 or 1099\b[^?\n]*\?/i,
  rate: /\b(?:rate or salary band|what(?:'|’)?s the rate|rate range)\b[^?\n]*\?/i,
  jd: /\bjob description\b/i,
}
const THEY_SAID = {
  endClient: /\b(?:end\s+)?client\s*(?:is|:|-)\s*[A-Z0-9]/i,
  remote: /\b(?:remote|on-?site|hybrid)\b/i,
  engagement: /\b(?:w-?2|1099)\b/i,
  rate: /\$\s?\d/,
  jd: /\b(?:responsibilities|requirements|qualifications)\b/i,
}
const THEY_ASKED = {
  rate: /\b(?:your (?:expected |desired )?rate|expect(?:ed|ation)s?\s*(?:rate|salary)|rate expectations?|salary expectations?|expectation rate)\b/i,
  call: /\b(?:good time|time to (?:talk|chat|connect|speak)|(?:a|quick) call|your availability|schedule)\b[^\n]*\?|\b(?:available|free) (?:to|for) (?:a )?(?:call|chat|talk)/i,
  resume: /\b(?:resume|résumé|cv)\b[^\n]*\?|\bsend (?:me |over )?(?:your|an updated|the latest) (?:resume|résumé|cv)\b/i,
}
const REPLY_COVERS = {
  rate: /\b(?:rate|range|band|compensation)\b/i,
  call: /\b(?:talk|call|time|chat|speak|phone)\b/i,
  resume: /\b(?:resume|résumé|cv)\b/i,
}

const GREETING_RE = /^(Hi|Hello|Dear)\b([^,\n]*),/

export function checkMessage(text: string, ctx: QualityContext): QualityIssue[] {
  const issues: QualityIssue[] = []
  const t = text.trim()
  if (!t) return issues
  const add = (i: QualityIssue) => issues.push(i)

  if (COMP_RE.test(t))
    add({
      id: 'comp',
      level: 'rule',
      message: 'States a rate or dollar figure.',
      suggestion: 'House rule: never state a figure. Say "the range you mentioned works for me" or ask for their band instead.',
    })
  if (WORK_AUTH_RE.test(t))
    add({
      id: 'workauth',
      level: 'rule',
      message: 'Mentions work authorization, citizenship, visa, sponsorship or clearance.',
      suggestion: 'House rule: leave this topic out entirely.',
    })
  if (AVAILABILITY_RE.test(t))
    add({
      id: 'availability',
      level: 'rule',
      message: 'Includes an availability or start-date statement.',
      suggestion: 'House rule: leave availability out; it comes up on the call.',
    })
  for (const [re, fix] of BANNED) {
    if (re.test(t)) add({ id: `banned:${re.source}`, level: 'rule', message: 'Uses a banned term.', suggestion: fix })
  }

  const greet = t.match(GREETING_RE)
  const first = (ctx.firstName || '').trim()
  if (!greet)
    add({
      id: 'greeting',
      level: 'suggest',
      message: 'No greeting line.',
      suggestion: first ? `Start with "Hi ${first},".` : 'Start with "Hi <first name>,".',
      fix: first ? (s) => `Hi ${first},\n\n${s.trimStart()}` : undefined,
    })
  else if (first && greet[2].trim().toLowerCase() !== first.toLowerCase())
    add({
      id: 'greeting-name',
      level: 'suggest',
      message: greet[2].trim() ? `Greets "${greet[2].trim()}", but you're writing to ${first}.` : 'The greeting has no name.',
      suggestion: `Use "${greet[1]} ${first},".`,
      fix: (s) => s.replace(GREETING_RE, `${greet[1]} ${first},`),
    })

  if (!CLOSING_RE.test(t) || !/shawn\.becker@spexture\.com/.test(t))
    add({
      id: 'signature',
      level: 'suggest',
      message: 'Missing the standard sign-off.',
      suggestion:
        'End with "Best, Shawn" (or "Best regards, Shawn Becker") and shawn.becker@spexture.com.',
      fix: (s) => `${s.replace(/\s*Best(?: regards)?,[\s\S]*$/i, '').trimEnd()}\n\n${SIGNATURE}`,
    })

  if (PLACEHOLDER_RE.test(t))
    add({
      id: 'placeholder',
      level: 'suggest',
      message: 'Looks like it still has a placeholder (<…>, […], TBD or XXX).',
      suggestion: 'Fill it in or remove it.',
    })
  if (t.length > 1200)
    add({
      id: 'length',
      level: 'suggest',
      message: `Long for a recruiter reply (${t.length} characters).`,
      suggestion: 'Trim to the answer and one or two questions; details fit better on a call.',
    })

  const theirs = (ctx.theirMessage || '').trim()
  if (theirs && ctx.kind === 'reply') {
    const reAsked: [keyof typeof ASKS, string][] = [
      ['endClient', 'the end client'],
      ['remote', 'remote vs onsite'],
      ['engagement', 'W2 vs 1099'],
      ['rate', 'the rate'],
    ]
    for (const [k, label] of reAsked) {
      if (ASKS[k].test(t) && THEY_SAID[k].test(theirs))
        add({
          id: `reask:${k}`,
          level: 'suggest',
          message: `Asks about ${label}, which their message already covers.`,
          suggestion: 'Drop that question or acknowledge their answer instead.',
          fix: (s) => dropQuestionLines(s, ASKS[k]),
        })
    }
    if (ASKS.jd.test(t) && THEY_SAID.jd.test(theirs) && theirs.length > 1500)
      add({
        id: 'reask:jd',
        level: 'suggest',
        message: 'Asks for the job description, but their message looks like it includes one.',
        suggestion: 'Check their message; ask only for what is missing.',
      })
    if (THEY_ASKED.rate.test(theirs) && !REPLY_COVERS.rate.test(t))
      add({
        id: 'answer:rate',
        level: 'suggest',
        message: 'They asked about your rate, and the reply doesn\'t answer.',
        suggestion: 'Without a figure: "The range you mentioned works for me" or "I\'d like to see the scope before settling on a rate. What band does the client have?"',
      })
    if (THEY_ASKED.call.test(theirs) && !REPLY_COVERS.call.test(t))
      add({
        id: 'answer:call',
        level: 'suggest',
        message: 'They asked for a call or a time, and the reply doesn\'t answer.',
        suggestion: 'Add "Happy to talk. What times work on your side? You can also reach me at +1 857-891-0896."',
      })
    if (THEY_ASKED.resume.test(theirs) && !REPLY_COVERS.resume.test(t))
      add({
        id: 'answer:resume',
        level: 'suggest',
        message: 'They asked for your résumé, and the reply doesn\'t mention it.',
        suggestion: 'Attach the generic latest résumé and say so, or say when you\'ll send a tailored one.',
      })
  }
  return issues
}

/** Remove the bullet / sentence carrying a question that their message already answered. */
function dropQuestionLines(text: string, ask: RegExp): string {
  const lines = text.split('\n')
  const kept = lines.filter((l) => !(ask.test(l) && /\?\s*$/.test(l.trim())))
  if (kept.length !== lines.length) return kept.join('\n').replace(/\n{3,}/g, '\n\n')
  return text.replace(new RegExp(`[^.?!\\n]*${ask.source}`, ask.flags), '').replace(/ {2,}/g, ' ')
}
