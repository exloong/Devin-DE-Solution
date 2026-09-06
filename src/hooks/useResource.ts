import { useCallback, useEffect, useRef, useState } from 'react';
import { ApiError } from '../api';

/**
 * A resource is either backed by live API data or by the committed demo
 * dataset. The two never coexist in one value: `demo` carries no records and
 * callers must fall back to `src/data.ts` explicitly.
 */
export type ResourceState<T> =
  | { kind: 'loading' }
  | { kind: 'demo'; reason: string }
  | { kind: 'live'; data: T; fetchedAt: number; stale: false }
  | { kind: 'stale'; data: T; fetchedAt: number; stale: true; error: ApiError }
  | { kind: 'empty'; fetchedAt: number }
  | { kind: 'error'; error: ApiError };

export interface ResourceOptions<T> {
  /** Poll interval in ms; 0 disables polling. */
  pollMs?: number;
  /** Age in ms after which live data is flagged stale even without a failure. */
  staleAfterMs?: number;
  /** Return true when a successful response should be treated as empty. */
  isEmpty?: (data: T) => boolean;
  /** Skip fetching entirely (e.g. no selected id). */
  enabled?: boolean;
}

export interface Resource<T> {
  state: ResourceState<T>;
  refresh: () => Promise<void>;
  /** Live data when available (also during stale), otherwise undefined. */
  data: T | undefined;
  isLive: boolean;
  isDemo: boolean;
}

/** Anything thrown after a response arrived (schema/mapping bugs, safety checks) is a reachable-API failure, never "absent". */
export function toApiError(error: unknown): ApiError {
  if (error instanceof ApiError) return error;
  return new ApiError('malformed_response', error instanceof Error ? error.message : 'Unexpected error', null, null, error, true);
}

/**
 * Decides the resource state after a failed fetch. Demo is chosen only when
 * no API answered (`apiAbsent`); every reachable failure — 401/403, malformed
 * JSON, wrong repository, policy/server errors — is an explicit `error`.
 */
export function resolveFailureState<T>(previous: ResourceState<T>, error: ApiError): ResourceState<T> {
  if (previous.kind === 'live' || previous.kind === 'stale') {
    return { kind: 'stale', data: previous.data, fetchedAt: previous.fetchedAt, stale: true, error };
  }
  if (error.apiAbsent) return { kind: 'demo', reason: error.message };
  return { kind: 'error', error };
}

export function useResource<T>(fetcher: () => Promise<T>, deps: readonly unknown[], options: ResourceOptions<T> = {}): Resource<T> {
  const { pollMs = 0, staleAfterMs = 0, isEmpty, enabled = true } = options;
  const [state, setState] = useState<ResourceState<T>>({ kind: 'loading' });
  const latest = useRef<ResourceState<T>>(state);
  latest.current = state;
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;
  const isEmptyRef = useRef(isEmpty);
  isEmptyRef.current = isEmpty;
  const requestId = useRef(0);

  const load = useCallback(async () => {
    if (!enabled) return;
    const id = ++requestId.current;
    try {
      const data = await fetcherRef.current();
      if (id !== requestId.current) return;
      const fetchedAt = Date.now();
      if (isEmptyRef.current?.(data)) setState({ kind: 'empty', fetchedAt });
      else setState({ kind: 'live', data, fetchedAt, stale: false });
    } catch (raw) {
      if (id !== requestId.current) return;
      setState(resolveFailureState(latest.current, toApiError(raw)));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled, ...deps]);

  useEffect(() => {
    if (!enabled) return;
    setState({ kind: 'loading' });
    void load();
  }, [load, enabled]);

  useEffect(() => {
    if (!enabled || pollMs <= 0) return;
    if (state.kind === 'demo') return;
    const timer = window.setInterval(() => void load(), pollMs);
    return () => window.clearInterval(timer);
  }, [enabled, pollMs, load, state.kind]);

  useEffect(() => {
    if (staleAfterMs <= 0 || state.kind !== 'live') return;
    const wait = Math.max(0, state.fetchedAt + staleAfterMs - Date.now());
    const timer = window.setTimeout(() => {
      setState(current =>
        current.kind === 'live'
          ? { kind: 'stale', data: current.data, fetchedAt: current.fetchedAt, stale: true, error: new ApiError('timeout', 'No fresh data received') }
          : current,
      );
    }, wait);
    return () => window.clearTimeout(timer);
  }, [staleAfterMs, state]);

  const data = state.kind === 'live' || state.kind === 'stale' ? state.data : undefined;
  return { state, refresh: load, data, isLive: state.kind === 'live' || state.kind === 'stale', isDemo: state.kind === 'demo' };
}
