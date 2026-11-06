import { describe, expect, it } from 'vitest'
import { addressName, greetingLine } from './greeting'
import { checkMessage } from './replyQuality'

describe('greeting', () => {
  it('matches the Python greeting_line rules', () => {
    expect(greetingLine('Dr. Md Mostafizur Rahman')).toBe('Hello, Dr. Rahman.')
    expect(greetingLine('Carlson Nkwain, PhD')).toBe('Hello, Dr. Nkwain.')
    expect(greetingLine('Anuj kumar Pandey')).toBe('Hi Anuj,')
    expect(greetingLine('Md Rahim')).toBe('Hi Rahim,')
    expect(greetingLine('')).toBe('Hi,')
  })
})

const SIG = 'Best, Shawn\nshawn.becker@spexture.com'
const ids = (text: string, theirMessage = '', firstName = 'Nalini') =>
  checkMessage(text, { theirMessage, firstName, kind: 'reply' }).map((i) => i.id)

describe('checkMessage', () => {
  it('passes a clean reply that answers the rate question', () => {
    const their = 'Client is IBM. Fully remote, W2 or 1099, $85 on W-2. What is your expectation rate?'
    expect(ids(`Hi Nalini,\n\nThe range you mentioned works for me.\n\n${SIG}`, their)).toEqual([])
  })

  it('flags a stated figure and work-authorization language', () => {
    const out = ids(`Hi Nalini,\n\nI need $90/hr and I'm a US citizen.\n\n${SIG}`)
    expect(out).toContain('comp')
    expect(out).toContain('workauth')
  })

  it('flags re-asking what they answered, and fixes it by dropping the question', () => {
    const their = 'Client is IBM. It is fully remote.'
    const text = `Hi Nalini,\n\nA few quick clarifiers:\n- Who is the end client / hiring company?\n- Is the role fully remote (US)?\n\n${SIG}`
    const issues = checkMessage(text, { theirMessage: their, firstName: 'Nalini', kind: 'reply' })
    const reask = issues.find((i) => i.id === 'reask:endClient')!
    expect(reask).toBeTruthy()
    expect(reask.fix!(text)).not.toContain('end client')
    expect(issues.map((i) => i.id)).toContain('reask:remote')
  })

  it('flags an unanswered call request', () => {
    expect(ids(`Hi Ed,\n\nThanks for the update.\n\n${SIG}`, "What's a good time to reach you tomorrow?", 'Ed')).toContain(
      'answer:call',
    )
  })

  it('fixes a wrong greeting name and a missing sign-off', () => {
    const issues = checkMessage('Hi Pat,\n\nThanks!', { firstName: 'Nalini', kind: 'reply' })
    let text = 'Hi Pat,\n\nThanks!'
    for (const i of issues) if (i.fix) text = i.fix(text)
    expect(text.startsWith('Hi Nalini,')).toBe(true)
    expect(text.endsWith(SIG)).toBe(true)
    expect(checkMessage(text, { firstName: 'Nalini', kind: 'reply' })).toEqual([])
  })

  it('accepts and enforces the formal greeting for a doctor', () => {
    const ok = `Hello, Dr. Rahman.\n\nThanks for the note.\n\n${SIG}`
    expect(ids(ok, '', addressName('Dr. Md Mostafizur Rahman'))).toEqual([])
    const wrong = `Hi Dr Pandey,\n\nThanks for the note.\n\n${SIG}`
    const issue = checkMessage(wrong, { firstName: 'Dr. Rahman', kind: 'reply' }).find((i) => i.id === 'greeting-name')!
    expect(issue.fix!(wrong).startsWith('Hello, Dr. Rahman.\n\nThanks')).toBe(true)
  })

  it('accepts "Best regards, Shawn Becker" as the closing', () => {
    const text = 'Hi Ed,\n\nThanks for the update.\n\nBest regards, Shawn Becker\nshawn.becker@spexture.com'
    expect(ids(text, '', 'Ed')).toEqual([])
  })

  it('does not treat the phone number as a rate', () => {
    expect(ids(`Hi Ed,\n\nHappy to talk. You can reach me at +1 857-891-0896.\n\n${SIG}`, '', 'Ed')).toEqual([])
  })
})
