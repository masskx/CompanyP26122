import { afterEach, describe, expect, it, vi } from 'vitest'
import http from '../src/api/client'
import {
  createSession,
  deleteSession,
  getSession,
  listMessages,
  listSessions,
  renameSession,
} from '../src/api/modules/conversation'

const session = {
  session_id: 's_1',
  user_id: 'anonymous',
  title: '问题',
  message_count: 2,
  workflow: null,
  created_at: '2026-11-16T08:00:00Z',
  updated_at: '2026-11-16T08:01:00Z',
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('conversation API', () => {
  it('loads and pages the global session list', async () => {
    const get = vi
      .spyOn(http, 'get')
      .mockResolvedValue({ data: [session] } as never)

    await expect(listSessions(20, 40)).resolves.toEqual([session])
    expect(get).toHaveBeenCalledWith('/v1/assistant/sessions', {
      params: { limit: 20, offset: 40 },
    })
  })

  it('creates, opens, renames and deletes sessions', async () => {
    const post = vi
      .spyOn(http, 'post')
      .mockResolvedValue({ data: session } as never)
    const get = vi
      .spyOn(http, 'get')
      .mockResolvedValue({ data: session } as never)
    const patch = vi
      .spyOn(http, 'patch')
      .mockResolvedValue({ data: { ...session, title: '新标题' } } as never)
    const remove = vi
      .spyOn(http, 'delete')
      .mockResolvedValue({ data: undefined } as never)

    await createSession('新对话')
    await createSession()
    await getSession('s/1')
    await renameSession('s/1', '新标题')
    await deleteSession('s/1')

    expect(post).toHaveBeenCalledWith('/v1/assistant/sessions', {
      title: '新对话',
    })
    expect(post).toHaveBeenLastCalledWith('/v1/assistant/sessions', undefined)
    expect(get).toHaveBeenCalledWith('/v1/assistant/sessions/s%2F1')
    expect(patch).toHaveBeenCalledWith('/v1/assistant/sessions/s%2F1', {
      title: '新标题',
    })
    expect(remove).toHaveBeenCalledWith('/v1/assistant/sessions/s%2F1')
  })

  it('loads messages with before_seq pagination', async () => {
    const get = vi
      .spyOn(http, 'get')
      .mockResolvedValue({ data: [] } as never)

    await listMessages('s_1', 50, 79)

    expect(get).toHaveBeenCalledWith(
      '/v1/assistant/sessions/s_1/messages',
      {
        params: { limit: 50, before_seq: 79 },
      },
    )
  })
})
