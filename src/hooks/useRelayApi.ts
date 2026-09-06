import { useCallback, useEffect, useState } from 'react';
import {
  apiClient,
  type AnalyticsSummary,
  type CancelSessionCommand,
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
} from '../api';
import { useCommand } from './useCommand';
import { useResource, type Resource } from './useResource';

const LIST_POLL_MS = 15_000;
const DETAIL_POLL_MS = 5_000;
const STALE_AFTER_MS = 60_000;

export type ApiMode = 'checking' | 'live' | 'degraded' | 'demo';

export interface ApiStatus {
  mode: ApiMode;
  health?: Health;
  readiness?: Readiness;
  reason?: string;
  checkedAt?: number;
  refresh: () => Promise<void>;
}

/** Probes /health and /ready to decide whether the dashboard runs live or on demo data. */
export function useApiStatus(pollMs = 30_000): ApiStatus {
  const health = useResource(() => apiClient.health(), [], { pollMs });
  const readiness = useResource(() => apiClient.ready(), [], { pollMs, enabled: health.isLive });

  const refresh = useCallback(async () => {
    await health.refresh();
    if (health.isLive) await readiness.refresh();
  }, [health, readiness]);

  if (health.state.kind === 'loading') return { mode: 'checking', refresh };
  if (health.state.kind === 'demo') return { mode: 'demo', reason: health.state.reason, refresh };
  if (health.state.kind === 'error') return { mode: 'demo', reason: health.state.error.message, refresh };
  if (health.state.kind === 'empty') return { mode: 'demo', reason: 'Health endpoint returned nothing', refresh };

  const healthData = health.state.data;
  const readinessData = readiness.data;
  const degraded = healthData.status === 'degraded' || (readinessData && (readinessData.database !== 'ok' || readinessData.worker !== 'ok'));
  return {
    mode: degraded ? 'degraded' : 'live',
    health: healthData,
    readiness: readinessData,
    reason: degraded ? describeDegraded(healthData, readinessData) : undefined,
    checkedAt: health.state.fetchedAt,
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

export function useReporterResponse(issueId: string | null, onAccepted?: () => void) {
  return useCommand(
    useCallback(
      (key: string, command: ReporterResponseCommand) => {
        if (!issueId) return Promise.reject(new Error('No issue selected'));
        return apiClient.respondToIssue(issueId, command, key);
      },
      [issueId],
    ),
    onAccepted,
  );
}

export function useOwnerDecision(issueId: string | null, onAccepted?: () => void) {
  return useCommand(
    useCallback(
      (key: string, command: OwnerDecisionCommand) => {
        if (!issueId) return Promise.reject(new Error('No issue selected'));
        return apiClient.decideIssue(issueId, command, key);
      },
      [issueId],
    ),
    onAccepted,
  );
}

export function useRetryIssue(issueId: string | null, onAccepted?: () => void) {
  return useCommand(
    useCallback(
      (key: string, command: RetryCommand) => {
        if (!issueId) return Promise.reject(new Error('No issue selected'));
        return apiClient.retryIssue(issueId, command, key);
      },
      [issueId],
    ),
    onAccepted,
  );
}

export function useCancelSession(sessionId: string | null, onAccepted?: () => void) {
  return useCommand(
    useCallback(
      (key: string, command: CancelSessionCommand) => {
        if (!sessionId) return Promise.reject(new Error('No session selected'));
        return apiClient.cancelSession(sessionId, command, key);
      },
      [sessionId],
    ),
    onAccepted,
  );
}

export function useSessionMessage(sessionId: string | null, onAccepted?: () => void) {
  return useCommand(
    useCallback(
      (key: string, command: SessionMessageCommand) => {
        if (!sessionId) return Promise.reject(new Error('No session selected'));
        return apiClient.messageSession(sessionId, command, key);
      },
      [sessionId],
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
