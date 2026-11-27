import { createHttp } from './client'

/**
 * Internal compute-server endpoints (`/internal/v1`).
 *
 * In development Vite proxies `/internal` to the FastAPI backend. In the
 * target deployment the main server owns these calls; the maintenance page
 * uses this client for the work-order copy and review endpoints.
 */
const internalHttp = createHttp('/internal/v1')

export default internalHttp
