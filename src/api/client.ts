import {
  TARGET_REPOSITORY,
  type AnalyticsSummary,
  type CancelSessionCommand,
  type CommandAccepted,
  type Health,
  type IssueDetail,
  type IssueFilters,
  type IssueSummary,
  type OwnerDecisionCommand,
  type Page,
  type Readiness,
  type ReporterResponseCommand,
  type RetryCommand,
  type SessionCapacity,
  type SessionDetail,
  type SessionFilters,
  type SessionMessageCommand,
  type SessionSummary,
  type WorkflowDefinition,
} from './types';

export type ApiErrorCode =
  | 'unavailable'
  | 'timeout'
  | 'not_found'
  | 'conflict'
  | 'unauthorized'
  | 'invalid_input'
  | 'policy_rejected'
  | 'server_error'
  | 'malformed_response'
  | 'wrong_repository';

export class ApiError extends Error {
  readonly code: ApiErrorCode;
  readonly status: number | null;
  readonly correlationId: string | null;
  readonly details: unknown;
  /** False only when no Relay API answered at all (network failure, timeout, or a non-API response at this origin). */
  readonly reachable: boolean;

  constructor(
    code: ApiErrorCode,
    message: string,
    status: number | null = null,
    correlationId: string | null = null,
    details: unknown = undefined,
    reachable = true,
  ) {
    super(message);
    this.name = 'ApiError';
    this.code = code;
    this.status = status;
    this.correlationId = correlationId;
    this.details = details;
    this.reachable = reachable;
  }

  /**
   * True only when the backend is genuinely absent. A reachable API that
   * answers 401/403, 5xx, malformed JSON, or a policy error is NOT absent and
   * must never fall back to demo data.
   */
  get apiAbsent(): boolean {
    return !this.reachable;
  }
}

export class RepositorySafetyError extends ApiError {
  readonly repository: string;

  constructor(repository: string, resource: string) {
    super(
      'wrong_repository',
      `${resource} belongs to ${repository}; Relay only processes ${TARGET_REPOSITORY}. The record was not entered into the flow.`,
      null,
      null,
      { repository, resource },
      true,
    );
    this.name = 'RepositorySafetyError';
    this.repository = repository;
  }
}

export interface MutationTarget {
  id: string;
  /** Current resource version, sent as `If-Match`. Omitted when the caller does not hold the resource. */
  version?: number;
}

interface RequestOptions {
  idempotencyKey?: string;
  ifMatchVersion?: number;
}

export interface ApiClientOptions {
  baseUrl?: string;
  timeoutMs?: number;
  fetchImpl?: typeof fetch;
  accessToken?: () => string | null;
}

export const OPERATOR_TOKEN_STORAGE_KEY = 'relay.operator_token';

function readBaseUrl(): string {
  const configured = import.meta.env.VITE_API_BASE as string | undefined;
  return (configured && configured.trim()) || '/api/v1';
}

function readAccessToken(): string | null {
  if (typeof window === 'undefined' || typeof window.sessionStorage === 'undefined') return null;
  return window.sessionStorage.getItem(OPERATOR_TOKEN_STORAGE_KEY);
}

export function newIdempotencyKey(): string {
  if (typeof crypto !== 'undefined' && 'randomUUID' in crypto) return crypto.randomUUID();
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`;
}

function toQuery(params: Record<string, string | number | string[] | undefined>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === '') continue;
    if (Array.isArray(value)) value.forEach(item => search.append(key, item));
    else search.set(key, String(value));
  }
  const text = search.toString();
  return text ? `?${text}` : '';
}

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function mapStatusToCode(status: number): ApiErrorCode {
  if (status === 401 || status === 403) return 'unauthorized';
  if (status === 404) return 'not_found';
  if (status === 409 || status === 412) return 'conflict';
  if (status === 400 || status === 422) return 'invalid_input';
  if (status === 451) return 'policy_rejected';
  if (status === 502 || status === 503 || status === 504) return 'unavailable';
  return 'server_error';
}

/** Rejects any record whose repository is not the configured Superset target. */
export function assertTargetRepository(repositoryFullName: string, resource: string): void {
  if (repositoryFullName !== TARGET_REPOSITORY) {
    throw new RepositorySafetyError(repositoryFullName, resource);
  }
}

export class ApiClient {
  readonly baseUrl: string;
  private readonly timeoutMs: number;
  private readonly fetchImpl: typeof fetch;
  private readonly accessToken: () => string | null;

  constructor(options: ApiClientOptions = {}) {
    this.baseUrl = (options.baseUrl ?? readBaseUrl()).replace(/\/$/, '');
    this.timeoutMs = options.timeoutMs ?? 8000;
    this.fetchImpl = options.fetchImpl ?? ((input, init) => fetch(input, init));
    this.accessToken = options.accessToken ?? readAccessToken;
  }

  private async request<T>(method: 'GET' | 'POST', path: string, body?: unknown, options: RequestOptions = {}): Promise<T> {
    const controller = new AbortController();
    const timer = window.setTimeout(() => controller.abort(), this.timeoutMs);
    let response: Response;
    try {
      const headers: Record<string, string> = { Accept: 'application/json' };
      const accessToken = this.accessToken();
      if (accessToken) headers.Authorization = `Bearer ${accessToken}`;
      if (body !== undefined) headers['Content-Type'] = 'application/json';
      if (options.idempotencyKey) headers['Idempotency-Key'] = options.idempotencyKey;
      if (options.ifMatchVersion !== undefined) headers['If-Match'] = `"${options.ifMatchVersion}"`;
      response = await this.fetchImpl(`${this.baseUrl}${path}`, {
        method,
        headers,
        body: body === undefined ? undefined : JSON.stringify(body),
        signal: controller.signal,
        credentials: 'same-origin',
      });
    } catch (error) {
      if (error instanceof DOMException && error.name === 'AbortError') {
        throw new ApiError('timeout', `Relay API did not respond within ${this.timeoutMs} ms`, null, null, undefined, false);
      }
      throw new ApiError('unavailable', 'Relay API is unreachable', null, null, undefined, false);
    } finally {
      window.clearTimeout(timer);
    }

    const correlationId = response.headers.get('x-correlation-id');
    const contentType = response.headers.get('content-type') ?? '';

    const isJson = contentType.includes('application/json');

    if (!response.ok) {
      if (!isJson && (response.status === 404 || response.status === 405)) {
        // Vite/nginx answering for an unknown route: no API is mounted at this origin.
        throw new ApiError('unavailable', 'Relay API is not mounted at this origin', response.status, correlationId, undefined, false);
      }
      let message = `Relay API returned ${response.status}`;
      let code = mapStatusToCode(response.status);
      let details: unknown;
      if (isJson) {
        const payload: unknown = await response.json().catch(() => null);
        const envelope = readErrorEnvelope(payload);
        if (envelope.message) message = envelope.message;
        if (envelope.code) code = normalizeCode(envelope.code, code);
        details = envelope.details;
      }
      throw new ApiError(code, message, response.status, correlationId, details);
    }

    if (!isJson) {
      // Vite/nginx returning index.html for an unknown route means no API is mounted.
      throw new ApiError('unavailable', 'Relay API is not mounted at this origin', response.status, correlationId, undefined, false);
    }

    try {
      return (await response.json()) as T;
    } catch {
      throw new ApiError('malformed_response', 'Relay API returned malformed JSON', response.status, correlationId);
    }
  }

  health(): Promise<Health> {
    return this.request<Health>('GET', '/health');
  }

  ready(): Promise<Readiness> {
    return this.request<Readiness>('GET', '/ready');
  }

  async listIssues(filters: IssueFilters = {}): Promise<Page<IssueSummary>> {
    const page = await this.request<Page<IssueSummary>>('GET', `/issues${toQuery({ state: filters.state, owner: filters.owner, search: filters.search })}`);
    page.items.forEach(issue => assertTargetRepository(issue.repository.full_name, issue.key));
    return page;
  }

  async getIssue(id: string): Promise<IssueDetail> {
    const issue = await this.request<IssueDetail>('GET', `/issues/${encodeURIComponent(id)}`);
    assertTargetRepository(issue.repository.full_name, issue.key);
    issue.pull_requests.forEach(pr => assertTargetRepository(pr.repository, `PR #${pr.number}`));
    return issue;
  }

  async listSessions(filters: SessionFilters = {}): Promise<Page<SessionSummary> & { capacity?: SessionCapacity }> {
    const page = await this.request<Page<SessionSummary> & { capacity?: SessionCapacity }>(
      'GET',
      `/sessions${toQuery({ status: filters.status, issue_id: filters.issue_id })}`,
    );
    page.items.forEach(session => assertTargetRepository(session.repository.full_name, session.id));
    return page;
  }

  async getSession(id: string): Promise<SessionDetail> {
    const session = await this.request<SessionDetail>('GET', `/sessions/${encodeURIComponent(id)}`);
    assertTargetRepository(session.repository.full_name, session.id);
    session.pull_requests.forEach(pr => assertTargetRepository(pr.repository, `PR #${pr.number}`));
    return session;
  }

  workflow(): Promise<WorkflowDefinition> {
    return this.request<WorkflowDefinition>('GET', '/workflow');
  }

  analyticsSummary(): Promise<AnalyticsSummary> {
    return this.request<AnalyticsSummary>('GET', '/analytics/summary');
  }

  /*
   * Mutations. The browser never sends actor identity; the backend derives it
   * from the authenticated request. `target.version` is sent as an `If-Match`
   * precondition so stale decisions are rejected server-side.
   */

  respondToIssue(target: MutationTarget, command: ReporterResponseCommand, idempotencyKey = newIdempotencyKey()): Promise<CommandAccepted> {
    return this.mutate(`/issues/${encodeURIComponent(target.id)}/responses`, target, command, idempotencyKey);
  }

  decideIssue(target: MutationTarget, command: OwnerDecisionCommand, idempotencyKey = newIdempotencyKey()): Promise<CommandAccepted> {
    return this.mutate(`/issues/${encodeURIComponent(target.id)}/decisions`, target, command, idempotencyKey);
  }

  retryIssue(target: MutationTarget, command: RetryCommand, idempotencyKey = newIdempotencyKey()): Promise<CommandAccepted> {
    return this.mutate(`/issues/${encodeURIComponent(target.id)}/actions/retry`, target, command, idempotencyKey);
  }

  cancelSession(target: MutationTarget, command: CancelSessionCommand, idempotencyKey = newIdempotencyKey()): Promise<CommandAccepted> {
    return this.mutate(`/sessions/${encodeURIComponent(target.id)}/actions/cancel`, target, command, idempotencyKey);
  }

  messageSession(target: MutationTarget, command: SessionMessageCommand, idempotencyKey = newIdempotencyKey()): Promise<CommandAccepted> {
    return this.mutate(`/sessions/${encodeURIComponent(target.id)}/messages`, target, command, idempotencyKey);
  }

  createDryRun(idempotencyKey = newIdempotencyKey()): Promise<CommandAccepted> {
    return this.request<CommandAccepted>('POST', '/dry-runs', {}, { idempotencyKey });
  }

  private mutate(path: string, target: MutationTarget, command: unknown, idempotencyKey: string): Promise<CommandAccepted> {
    return this.request<CommandAccepted>('POST', path, command, { idempotencyKey, ifMatchVersion: target.version });
  }
}

const knownCodes: ApiErrorCode[] = [
  'unavailable',
  'timeout',
  'not_found',
  'conflict',
  'unauthorized',
  'invalid_input',
  'policy_rejected',
  'server_error',
  'malformed_response',
  'wrong_repository',
];

/** Accepts the backend envelope `{error:{code,message,details}}` and a defensive top-level `{code,message}` form. */
function readErrorEnvelope(payload: unknown): { code?: string; message?: string; details?: unknown } {
  if (!isObject(payload)) return {};
  const inner = isObject(payload.error) ? payload.error : payload;
  return {
    code: typeof inner.code === 'string' ? inner.code : undefined,
    message: typeof inner.message === 'string' ? inner.message : typeof payload.error === 'string' ? payload.error : undefined,
    details: inner.details,
  };
}

function normalizeCode(candidate: string, fallback: ApiErrorCode): ApiErrorCode {
  return (knownCodes as string[]).includes(candidate) ? (candidate as ApiErrorCode) : fallback;
}

export const apiClient = new ApiClient();
