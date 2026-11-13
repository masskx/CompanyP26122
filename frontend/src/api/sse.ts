export interface ServerSentEvent {
  event: string
  data: string
  id?: string
}

/**
 * Incremental SSE parser. It accepts arbitrary network chunk boundaries and
 * supports LF, CRLF, and CR line endings.
 */
export class SSEParser {
  private line = ''
  private dataLines: string[] = []
  private eventName = ''
  private eventID: string | undefined
  private skipLeadingLF = false
  private atStart = true

  push(chunk: string): ServerSentEvent[] {
    const events: ServerSentEvent[] = []

    for (const character of chunk) {
      if (this.atStart) {
        this.atStart = false
        if (character === '\uFEFF') {
          continue
        }
      }

      if (character === '\r') {
        this.consumeLine(events)
        this.skipLeadingLF = true
        continue
      }

      if (character === '\n') {
        if (this.skipLeadingLF) {
          this.skipLeadingLF = false
          continue
        }
        this.consumeLine(events)
        continue
      }

      this.skipLeadingLF = false
      this.line += character
    }

    return events
  }

  finish(): ServerSentEvent[] {
    const events: ServerSentEvent[] = []
    if (this.line.length > 0) {
      this.consumeLine(events)
    }
    this.dispatch(events)
    return events
  }

  private consumeLine(events: ServerSentEvent[]): void {
    const line = this.line
    this.line = ''

    if (line === '') {
      this.dispatch(events)
      return
    }
    if (line.startsWith(':')) {
      return
    }

    const separator = line.indexOf(':')
    const field = separator === -1 ? line : line.slice(0, separator)
    let value = separator === -1 ? '' : line.slice(separator + 1)
    if (value.startsWith(' ')) {
      value = value.slice(1)
    }

    if (field === 'data') {
      this.dataLines.push(value)
    } else if (field === 'event') {
      this.eventName = value
    } else if (field === 'id') {
      this.eventID = value
    }
  }

  private dispatch(events: ServerSentEvent[]): void {
    if (this.dataLines.length > 0) {
      events.push({
        event: this.eventName || 'message',
        data: this.dataLines.join('\n'),
        ...(this.eventID === undefined ? {} : { id: this.eventID }),
      })
    }
    this.dataLines = []
    this.eventName = ''
  }
}
