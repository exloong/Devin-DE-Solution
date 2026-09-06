import { useCallback, useEffect, useState } from 'react';
import {
  ApiError,
  apiClient,
  type AnalyticsSummary,
  type CancelSessionCommand,
  type Health,
  type IssueDetail,
  type IssueFilters,
  type IssueSummary,
  type MutationTarget,
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
} from '../api';
import { useCommand } from './useCommand';
import { useResource, type Resource } from './useResource';

const LIST_POLL_MS = 15_000;
const DETAIL_POLL_MS = 5_000;
const STALE_AFTER_MS = 60_000;

/**
 * `demo`     — no Relay API answered at all; the committed demo dataset is shown.
 * `checking` — health and readiness have not both resolved yet.
 * `live`     — API healthy and ready.
 * `degraded` — API reachable but reports degraded health or readiness.
 * `error`    — API reachable but health failed (401/403, malformed JSON, policy or server error).
 *              No demo records may be shown in this mode.
 */
export type ApiMode = 'checking' | 'live' | 'degraded' | 'demo' | 'error';

export interface ApiStatus {
  mode: ApiMode;
  health?: Health;
  readiness?: Readiness;
  reason?: string;
  error?: ApiError;
  checkedAt?: number;
  refresh: () => Promise<void>;
}

/** Probes /health and /ready to decide whether the dashboard runs live, on demo data, or must show an API error. */
export function useApiStatus(pollMs = 30_000): ApiStatus {
  const health = useResource(() => apiClient.health(), [], { pollMs });
  const readiness = useResource(() => apiClient.ready(), [], { pollMs, enabled: health.isLive });

  const refresh = useCallback(async () => {
    await health.refresh();
    if (health.isLive) await readiness.refresh();
  }, [health, readiness]);

  if (health.state.kind === 'loading') return { mode: 'checking', refresh };
  if (health.state.kind === 'demo') return { mode: 'demo', reason: health.state.reason, refresh };
  if (health.state.kind === 'error') {
    return { mode: 'error', reason: health.state.error.message, error: health.state.error, refresh };
  }
  if (health.state.kind === 'empty') {
    const error = new ApiError('malformed_response', 'Health endpoint returned no payload');
    return { mode: 'error', reason: error.message, error, refresh };
  }

  const healthData = health.state.data;
  const checkedAt = health.state.fetchedAt;

  if (readiness.state.kind === 'loading') return { mode: 'checking', health: healthData, checkedAt, refresh };
  if (readiness.state.kind === 'error' || readiness.state.kind === 'demo' || readiness.state.kind === 'empty') {
    const reason =
      readiness.state.kind === 'error'
        ? `Readiness check failed: ${readiness.state.error.message}`
        : readiness.state.kind === 'demo'
          ? `Readiness check unreachable: ${readiness.state.reason}`
          : 'Readiness endpoint returned no payload';
    return {
      mode: 'degraded',
      health: healthData,
      reason,
      error: readiness.state.kind === 'error' ? readiness.state.error : undefined,
      checkedAt,
      refresh,
    };
  }

  const readinessData = readiness.state.data;
  const staleProbe = health.state.kind === 'stale' ? health.state.error : readiness.state.kind === 'stale' ? readiness.state.error : null;
  const degraded = staleProbe !== null || healthData.status === 'degraded' || readinessData.database !== 'ok' || readinessData.worker !== 'ok';
  return {
    mode: degraded ? 'degraded' : 'live',
    health: healthData,
    readiness: readinessData,
    reason: staleProbe ? `Health probe is stale: ${staleProbe.message}` : degraded ? describeDegraded(healthData, readinessData) : undefined,
    error: staleProbe ?? undefined,
    checkedAt,
    refresh,
  };
}

function describeDegraded(health: Health, readiness?: Readiness): string {
  if (readiness?.database !== 'ok') return 'Database unavailable';
  if (readiness?.worker === 'stale') return 'Worker heartbeat is stale';
  if (readiness?.worker === 'unavailable') return 'Worker unavailable';
  return health.status === 'degraded' ? 'API reports degraded status' : 'Degraded';
}

export function useIssues(filters: IssueFilters = {}, enabled = true): Resource<Page<IssueSummary>> {
  const key = JSON.stringify(filters);
  return useResource(() => apiClient.listIssues(filters), [key], {
    pollMs: LIST_POLL_MS,
    staleAfterMs: STALE_AFTER_MS,
    isEmpty: page => page.items.length === 0,
    enabled,
  });
}

export function useIssue(id: string | null, enabled = true): Resource<IssueDetail> {
  return useResource(() => apiClient.getIssue(id ?? ''), [id], {
    pollMs: DETAIL_POLL_MS,
    staleAfterMs: STALE_AFTER_MS,
    enabled: enabled && id !== null,
  });
}

export type SessionPage = Page<SessionSummary> & { capacity?: SessionCapacity };

export function useSessions(filters: SessionFilters = {}, enabled = true): Resource<SessionPage> {
  const key = JSON.stringify(filters);
  return useResource(() => apiClient.listSessions(filters), [key], {
    pollMs: LIST_POLL_MS,
    staleAfterMs: STALE_AFTER_MS,
    isEmpty: page => page.items.length === 0,
    enabled,
  });
}

export function useSession(id: string | null, enabled = true): Resource<SessionDetail> {
  return useResource(() => apiClient.getSession(id ?? ''), [id], {
    pollMs: DETAIL_POLL_MS,
    staleAfterMs: STALE_AFTER_MS,
    enabled: enabled && id !== null,
  });
}

export function useWorkflow(enabled = true): Resource<WorkflowDefinition> {
  return useResource(() => apiClient.workflow(), [], { staleAfterMs: STALE_AFTER_MS * 10, enabled });
}

export function useAnalytics(enabled = true): Resource<AnalyticsSummary> {
  return useResource(() => apiClient.analyticsSummary(), [], { pollMs: LIST_POLL_MS * 4, staleAfterMs: STALE_AFTER_MS * 5, enabled });
}

export function useReporterResponse(target: MutationTarget | null, onAccepted?: () => void) {
  const id = target?.id ?? null;
  const version = target?.version;
  return useCommand(
    useCallback(
      (key: string, command: ReporterResponseCommand) => {
        if (!id) return Promise.reject(new Error('No issue selected'));
        return apiClient.respondToIssue({ id, version }, command, key);
      },
      [id, version],
    ),
    onAccepted,
  );
}

export function useOwnerDecision(target: MutationTarget | null, onAccepted?: () => void) {
  const id = target?.id ?? null;
  const version = target?.version;
  return useCommand(
    useCallback(
      (key: string, command: OwnerDecisionCommand) => {
        if (!id) return Promise.reject(new Error('No issue selected'));
        return apiClient.decideIssue({ id, version }, command, key);
      },
      [id, version],
    ),
    onAccepted,
  );
}

export function useRetryIssue(target: MutationTarget | null, onAccepted?: () => void) {
  const id = target?.id ?? null;
  const version = target?.version;
  return useCommand(
    useCallback(
      (key: string, command: RetryCommand) => {
        if (!id) return Promise.reject(new Error('No issue selected'));
        return apiClient.retryIssue({ id, version }, command, key);
      },
      [id, version],
    ),
    onAccepted,
  );
}

export function useCancelSession(target: MutationTarget | null, onAccepted?: () => void) {
  const id = target?.id ?? null;
  const version = target?.version;
  return useCommand(
    useCallback(
      (key: string, command: CancelSessionCommand) => {
        if (!id) return Promise.reject(new Error('No session selected'));
        return apiClient.cancelSession({ id, version }, command, key);
      },
      [id, version],
    ),
    onAccepted,
  );
}

export function useSessionMessage(target: MutationTarget | null, onAccepted?: () => void) {
  const id = target?.id ?? null;
  const version = target?.version;
  return useCommand(
    useCallback(
      (key: string, command: SessionMessageCommand) => {
        if (!id) return Promise.reject(new Error('No session selected'));
        return apiClient.messageSession({ id, version }, command, key);
      },
      [id, version],
    ),
    onAccepted,
  );
}

export function useDryRun(onAccepted?: () => void) {
  return useCommand(useCallback((key: string) => apiClient.createDryRun(key), []), onAccepted);
}

/** Ticks every `intervalMs` so elapsed-time displays stay current. */
export function useNow(intervalMs = 1000, active = true): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return;
    const timer = window.setInterval(() => setNow(Date.now()), intervalMs);
    return () => window.clearInterval(timer);
  }, [intervalMs, active]);
  return now;
}
