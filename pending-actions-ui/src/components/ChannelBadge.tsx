import type { Channel } from '../types'

/**
 * Channel badge. When `href` is set (usually a recruiting-Gmail deep link for
 * the last archived message), the badge is clickable for both email and
 * LinkedIn — LinkedIn InMails often also land as hit-reply@ Gmail copies.
 */
export function ChannelBadge({
  channel,
  href,
}: {
  channel: Channel | string
  href?: string
}) {
  const label = channel === 'linkedin' ? 'LinkedIn' : 'Email'
  const className = `channel-badge channel-${channel}`
  const title =
    channel === 'linkedin' && href
      ? 'Open LinkedIn message copy in shawnbecker.recruiting@gmail.com'
      : href
        ? 'Open last message in shawnbecker.recruiting@gmail.com'
        : undefined
  if (href) {
    return (
      <a
        className={className}
        href={href}
        target="_blank"
        rel="noreferrer"
        title={title}
      >
        {label}
      </a>
    )
  }
  return <span className={className}>{label}</span>
}
