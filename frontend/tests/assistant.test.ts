import { describe, expect, it, vi } from 'vitest'
import {
  AssistantApiError,
  parseAssistantEvent,
  streamAssistantChat,
  type AssistantStreamEvent,
} from '../src/api/modules/assistant'
import { SSEParser } from '../src/api/sse'
import { renderMarkdown } from '../src/features/assistant/markdown'

describe('SSEParser', () => {
  it('handles events split across arbitrary CRLF chunks', () => {
    const parser = new SSEParser()

    expect(parser.push('event: session\r')).toEqual([])
    expect(parser.push('\ndata: {"session_id":"s_1"}\r')).toEqual([])
    expect(parser.push('\n\r\n')).toEqual([
      { event: 'session', data: '{"session_id":"s_1"}', id: undefined },
    ])
  })

  it('joins multiline data and ignores comments', () => {
    const parser = new SSEParser()
    const events = parser.push(
      ': heartbeat\nevent: citation\ndata: first\ndata: second\n\n',
    )

    expect(events).toHaveLength(1)
    expect(events[0]).toMatchObject({
      event: 'citation',
      data: 'first\nsecond',
    })
  })

  it('dispatches a buffered event at end of stream', () => {
    const parser = new SSEParser()
    parser.push('event: delta\ndata: {"text":"tail"}')

    expect(parser.finish()).toEqual([
      { event: 'delta', data: '{"text":"tail"}' },
    ])
  })

  it('supports CR-only line endings and strips a BOM', () => {
    const parser = new SSEParser()
    const events = parser.push(
      '\uFEFFevent: delta\rdata: {"text":"ok"}\r\r',
    )

    expect(events).toEqual([
      { event: 'delta', data: '{"text":"ok"}' },
    ])
  })
})

describe('parseAssistantEvent', () => {
  it('preserves whitespace in streamed answer text', () => {
    const event = parseAssistantEvent({
      event: 'delta',
      data: '{"text":"  第一段\\n"}',
    })

    expect(event).toEqual({ type: 'delta', text: '  第一段\n' })
  })

  it('normalizes backend citations for the UI', () => {
    const event = parseAssistantEvent(
      {
        event: 'citation',
        data: JSON.stringify({
          ref: 2,
          kind: 'data',
          title: '设备状态',
          device_id: 'AS01-ISO-RM-02',
          time_range: '最近 6 小时',
          snippet: '连续越限',
        }),
      },
      4,
    )

    expect(event).toEqual({
      type: 'citation',
      citation: {
        id: 'citation-2',
        title: '设备状态',
        source: '设备数据',
        section: 'AS01-ISO-RM-02',
        version: '',
        excerpt: '连续越限',
      },
    })
  })

  it('throws a typed error for malformed events and error events', () => {
    expect(() =>
      parseAssistantEvent({ event: 'delta', data: '{' }),
    ).toThrow(AssistantApiError)

    expect(() =>
      parseAssistantEvent({
        event: 'error',
        data: '{"code":"agent_failed","message":"model failed"}',
      }),
    ).toThrowError('model failed')
  })
})

describe('streamAssistantChat', () => {
  it('posts the message and remote session, then emits typed events', async () => {
    const fetchMock = vi.fn(async (_url: string, init?: RequestInit) => {
      expect(new Headers(init?.headers).get('X-Request-ID')).toBe('turn-test')
      expect(JSON.parse(String(init?.body))).toEqual({
        message: '继续回答',
        session_id: 's_existing',
      })
      const body = new ReadableStream<Uint8Array>({
        start(controller) {
          const encoder = new TextEncoder()
          controller.enqueue(
            encoder.encode('event: session\ndata: {"session_id":"s_existing"}\n\n'),
          )
          controller.enqueue(
            encoder.encode('event: delta\ndata: {"text":"真实"}\n\n'),
          )
          controller.enqueue(
            encoder.encode(
              'event: done\ndata: {"message":{"content":"真实回答","session_id":"s_existing","citations":[]}}\n\n',
            ),
          )
          controller.close()
        },
      })
      return new Response(body, {
        status: 200,
        headers: { 'Content-Type': 'text/event-stream' },
      })
    })
    vi.stubGlobal('fetch', fetchMock)

    const events: AssistantStreamEvent[] = []
    await streamAssistantChat('继续回答', 's_existing', {
      requestId: 'turn-test',
      onEvent: (event) => events.push(event),
    })

    expect(fetchMock).toHaveBeenCalledOnce()
    expect(events.map((event) => event.type)).toEqual([
      'session',
      'delta',
      'done',
    ])
    expect(events[2]).toMatchObject({
      type: 'done',
      message: { content: '真实回答', sessionId: 's_existing' },
    })
    vi.unstubAllGlobals()
  })

  it('maps HTTP errors to AssistantApiError', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        new Response(
          JSON.stringify({
            code: 'assistant_upstream_timeout',
            message: 'assistant response timed out',
            request_id: 'r_1',
          }),
          {
            status: 504,
            headers: { 'Content-Type': 'application/json' },
          },
        ),
      ),
    )

    await expect(
      streamAssistantChat('你好', undefined, { onEvent: () => undefined }),
    ).rejects.toMatchObject({
      code: 'assistant_upstream_timeout',
      status: 504,
      requestId: 'r_1',
    })
    vi.unstubAllGlobals()
  })
})

describe('renderMarkdown', () => {
  it('renders supported Markdown and safe external links', () => {
    const html = renderMarkdown(
      '## 结论\n\n- 第一项\n- 第二项\n\n[资料](https://example.com/doc)',
    )

    expect(html).toContain('<h2>结论</h2>')
    expect(html).toContain('<ul>')
    expect(html).toContain('href="https://example.com/doc"')
    expect(html).toContain('target="_blank"')
    expect(html).toContain('rel="noopener noreferrer"')
  })

  it('removes scripts, styles, iframes, and images', () => {
    const html = renderMarkdown(
      '<script>alert(1)</script><style>body{display:none}</style><iframe src="x"></iframe><img src=x onerror=alert(2)>正文',
    )

    expect(html).toContain('正文')
    expect(html).not.toContain('<script')
    expect(html).not.toContain('<style')
    expect(html).not.toContain('<iframe')
    expect(html).not.toContain('<img')
    expect(html).not.toContain('onerror')
  })
})
