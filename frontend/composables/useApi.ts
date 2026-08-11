import type { components, paths } from '~/types/api'

/**
 * Обёртка над fetch.
 *
 * Авторизация — на cookie: токены в localStorage не кладём, иначе их достанет
 * XSS. Поэтому каждому запросу нужен credentials: 'include'.
 */

export type Schemas = components['schemas']
export type ApiPaths = paths

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    readonly requestId?: string,
  ) {
    super(message)
  }
}

interface ErrorBody {
  error?: { code?: string; message?: string; request_id?: string }
}

export async function apiFetch<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(`/api/v1${path}`, {
    ...options,
    credentials: 'include',
    headers: {
      Accept: 'application/json',
      ...(options.body ? { 'Content-Type': 'application/json' } : {}),
      ...options.headers,
    },
  })

  if (response.status === 204) {
    return undefined as T
  }

  const text = await response.text()
  const body = text ? JSON.parse(text) : {}

  if (!response.ok) {
    const details = (body as ErrorBody).error
    throw new ApiError(
      response.status,
      details?.code ?? 'UNKNOWN',
      details?.message ?? 'Не удалось выполнить запрос',
      details?.request_id,
    )
  }
  return body as T
}

export function apiGet<T>(path: string): Promise<T> {
  return apiFetch<T>(path)
}

export function apiPost<T>(path: string, body?: unknown): Promise<T> {
  return apiFetch<T>(path, {
    method: 'POST',
    body: body === undefined ? undefined : JSON.stringify(body),
  })
}

export function apiPut<T>(path: string, body: unknown): Promise<T> {
  return apiFetch<T>(path, { method: 'PUT', body: JSON.stringify(body) })
}

export function apiPatch<T>(path: string, body: unknown): Promise<T> {
  return apiFetch<T>(path, { method: 'PATCH', body: JSON.stringify(body) })
}

export function apiDelete(path: string): Promise<void> {
  return apiFetch<void>(path, { method: 'DELETE' })
}

/** Текст ошибки для показа пользователю. */
export function errorText(error: unknown): string {
  if (error instanceof ApiError) {
    return error.message
  }
  return 'Сервис недоступен'
}
