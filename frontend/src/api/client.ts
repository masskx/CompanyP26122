import axios, { type AxiosError, type AxiosInstance } from 'axios'

interface ApiErrorPayload {
  message?: string
  detail?: string
  code?: string
}

interface ApiErrorOptions {
  status?: number
  code?: string
  originalError?: unknown
}

export class ApiError extends Error {
  readonly status?: number
  readonly code?: string
  readonly originalError?: unknown

  constructor(message: string, options: ApiErrorOptions = {}) {
    super(message)
    this.name = 'ApiError'
    this.status = options.status
    this.code = options.code
    this.originalError = options.originalError
  }
}

function readErrorPayload(payload: unknown): ApiErrorPayload {
  if (typeof payload !== 'object' || payload === null) {
    return {}
  }

  const record = payload as Record<string, unknown>
  const detail = record.detail
  const detailRecord =
    typeof detail === 'object' && detail !== null
      ? (detail as Record<string, unknown>)
      : null

  return {
    message:
      typeof record.message === 'string'
        ? record.message
        : typeof detailRecord?.message === 'string'
          ? detailRecord.message
          : undefined,
    detail: typeof detail === 'string' ? detail : undefined,
    code:
      typeof record.code === 'string'
        ? record.code
        : typeof detailRecord?.code === 'string'
          ? detailRecord.code
          : undefined,
  }
}

export function createHttp(baseURL: string): AxiosInstance {
  const instance = axios.create({
    baseURL,
    timeout: 30_000,
    headers: {
      Accept: 'application/json',
    },
  })

  instance.interceptors.response.use(
    (response) => response,
    (error: AxiosError) => {
      const payload = readErrorPayload(error.response?.data)
      const message =
        payload.message ||
        payload.detail ||
        error.message ||
        '请求失败，请稍后重试'

      return Promise.reject(
        new ApiError(message, {
          status: error.response?.status,
          code: payload.code || error.code,
          originalError: error,
        }),
      )
    },
  )

  return instance
}

const baseURL = import.meta.env.VITE_API_BASE_URL?.trim() || '/api'

const http = createHttp(baseURL)

export default http
