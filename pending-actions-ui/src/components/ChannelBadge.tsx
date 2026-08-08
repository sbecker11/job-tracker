import type { Channel } from '../types'

export function ChannelBadge({ channel }: { channel: Channel | string }) {
  const label = channel === 'linkedin' ? 'LinkedIn' : 'Email'
  return <span className={`channel-badge channel-${channel}`}>{label}</span>
}
