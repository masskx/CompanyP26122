import axios, { type AxiosError } from 'axios'

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

  const { message, detail, code } = payload as Record<string, unknown>

  return {
    message: typeof message === 'string' ? message : undefined,
    detail: typeof detail === 'string' ? detail : undefined,
    code: typeof code === 'string' ? code : undefined,
  }
}

const baseURL = import.meta.env.VITE_API_BASE_URL?.trim() || '/api'

const http = axios.create({
  baseURL,
  timeout: 30_000,
  headers: {
    Accept: 'application/json',
  },
})

http.interceptors.response.use(
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

export default http
