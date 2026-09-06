import { AlertTriangle, Database, FlaskConical, Inbox, Loader2, Radio } from 'lucide-react';
import type { ResourceState } from '../hooks';

export type SourceTone = 'demo' | 'live' | 'loading' | 'stale' | 'error' | 'empty';

export function toneForState(state: ResourceState<unknown>): SourceTone {
  return state.kind;
}

export function DataSourceBadge({
  state,
  compact = false,
  title,
}: {
  state: ResourceState<unknown>;
  compact?: boolean;
  title?: string;
}) {
  const tone = toneForState(state);
  const label =
    tone === 'demo'
      ? 'Demo data'
      : tone === 'live'
        ? 'Live'
        : tone === 'loading'
          ? 'Loading'
          : tone === 'stale'
            ? 'Stale'
            : tone === 'empty'
              ? 'No records'
              : 'Error';
  const Icon =
    tone === 'demo' ? FlaskConical : tone === 'live' ? Radio : tone === 'loading' ? Loader2 : tone === 'stale' ? Database : tone === 'empty' ? Inbox : AlertTriangle;
  const detail =
    state.kind === 'demo'
      ? `Committed mock records · ${state.reason}`
      : state.kind === 'stale'
        ? `Last live update ${new Date(state.fetchedAt).toLocaleTimeString()} · ${state.error.message}`
        : state.kind === 'error'
          ? `${state.error.code}: ${state.error.message}`
          : state.kind === 'live'
            ? `Fetched ${new Date(state.fetchedAt).toLocaleTimeString()}`
            : undefined;
  return (
    <span className={`source-badge ${tone} ${compact ? 'compact' : ''}`} title={title ?? detail} role="status">
      <Icon size={12} className={tone === 'loading' ? 'spin' : undefined} />
      {label}
    </span>
  );
}

export function ResourceNotice({ state, resourceLabel }: { state: ResourceState<unknown>; resourceLabel: string }) {
  if (state.kind === 'live' || state.kind === 'demo') return null;
  if (state.kind === 'loading') {
    return (
      <div className="resource-notice loading" role="status">
        <Loader2 size={16} className="spin" />
        <span>Loading {resourceLabel} from the Relay API…</span>
      </div>
    );
  }
  if (state.kind === 'empty') {
    return (
      <div className="resource-notice empty" role="status">
        <Inbox size={16} />
        <span>The Relay API returned no {resourceLabel}. Demo records are not shown while the API is reachable.</span>
      </div>
    );
  }
  if (state.kind === 'stale') {
    return (
      <div className="resource-notice stale" role="status">
        <Database size={16} />
        <span>
          Showing {resourceLabel} from {new Date(state.fetchedAt).toLocaleTimeString()}; the latest refresh failed ({state.error.message}).
        </span>
      </div>
    );
  }
  return (
    <div className="resource-notice error" role="alert">
      <AlertTriangle size={16} />
      <span>
        Could not load {resourceLabel}: {state.error.message}
        {state.error.correlationId ? ` · correlation ${state.error.correlationId}` : ''}
      </span>
    </div>
  );
}
