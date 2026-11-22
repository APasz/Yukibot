import createClient from 'openapi-fetch';
import type { paths } from '../generated/api';

export const api = createClient<paths>({
  baseUrl: window.location.origin,
  credentials: 'same-origin',
  cache: 'no-store',
});

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

export function requireData<T>(result: { data?: T; response: Response }): T {
  if (!result.response.ok || result.data === undefined) {
    const messages: Record<number, string> = {
      401: 'Your session has expired. Sign in again to continue.',
      403: 'You do not have permission for this action.',
      413: 'This report is too large to save. Export it to keep a local copy.',
      422: 'The report did not pass validation. Export it to keep a local copy.',
    };
    throw new ApiError(
      result.response.status,
      messages[result.response.status] ??
        `The server returned HTTP ${result.response.status}. Please try again.`,
    );
  }
  return result.data;
}

export function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : 'An unexpected error occurred.';
}
