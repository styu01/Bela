import pino from 'pino'

// The size-rotated dashboard.log can span more than a day.  Record the local
// date and UTC offset on every pretty-printed line so events remain traceable.
export const PRETTY_OPTIONS = { colorize: true, translateTime: 'SYS:standard' } as const

export const logger = pino({
  level: process.env.LOG_LEVEL ?? 'info',
  transport:
    process.env.NODE_ENV !== 'production'
      ? { target: 'pino-pretty', options: PRETTY_OPTIONS }
      : undefined,
})
