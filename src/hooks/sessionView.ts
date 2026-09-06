import type {
  AgentSessionStatus,
  Artifact,
  ConversationMessage,
  DevinReviewState,
  HumanGate,
  PullRequestRef,
  SessionDetail,
  SessionEvent,
  SessionLinks,
  SessionOutput,
  SessionSummary,
} from '../api';
import { TARGET_REPOSITORY_URL, safeHref } from '../api/links';
import { TARGET_REPOSITORY } from '../api';
import type { DevinSession, DevinSessionEvent, DevinSessionStatus } from '../data';

export type DataSource = 'demo' | 'live';

/**
 * Presentation model shared by the demo dataset and live API sessions. Live-only
 * sections are `null` for demo records so the UI can disclose their absence
 * instead of inventing content.
 */
export interface SessionView {
  source: DataSource;
  dryRun: boolean;
  id: string;
  version: number | null;
  shortId: string;
  issueId: string;
  issueKey: string;
  issueTitle: string;
  title: string;
  flowStep: string;
  actor: string;
  status: DevinSessionStatus;
  apiStatus: AgentSessionStatus | null;
  started: string;
  startedAt: number | null;
  endedAt: number | null;
  elapsed: string;
  updated: string;
  /** null when the source has no synchronized progress value (live Devin sessions). */
  progress: number | null;
  budget: string;
  budgetSeconds: number | null;
  environment: string;
  workspaceReleased: boolean;
  trigger: string;
  currentAction: string | null;
  nextCheckpoint: string | null;
  repository: string;
  repositoryUrl: string;
  commit: string | null;
  branch: string | null;
  events: DevinSessionEvent[];
  artifactLabels: string[];
  artifacts: Artifact[] | null;
  conversation: ConversationMessage[] | null;
  outputs: SessionOutput[] | null;
  pullRequests: PullRequestRef[] | null;
  review: DevinReviewState | null;
  links: SessionLinks | null;
  humanGate: HumanGate | null;
  correlationId: string | null;
}

export function fromDemoSession(session: DevinSession): SessionView {
  return {
    source: 'demo',
    dryRun: true,
    id: session.id,
    version: null,
    shortId: session.id,
    issueId: String(session.issueId),
    issueKey: session.issueKey,
    issueTitle: session.issueTitle,
    title: session.title,
    flowStep: session.flowStep,
    actor: session.actor,
    status: session.status,
    apiStatus: null,
    started: session.started,
    startedAt: null,
    endedAt: null,
    elapsed: session.elapsed,
    updated: session.updated,
    progress: normalizeProgress(session.progress),
    budget: session.budget,
    budgetSeconds: null,
    environment: session.environment,
    workspaceReleased: session.environment === 'Workspace released',
    trigger: session.trigger,
    currentAction: session.currentAction,
    nextCheckpoint: session.nextCheckpoint,
    repository: TARGET_REPOSITORY,
    repositoryUrl: TARGET_REPOSITORY_URL,
    commit: null,
    branch: session.branch ?? null,
    events: session.events,
    artifactLabels: session.artifacts,
    artifacts: null,
    conversation: null,
    outputs: null,
    pullRequests: null,
    review: null,
    links: null,
    humanGate: null,
    correlationId: null,
  };
}

const transitionLabels: Record<string, string> = {
  intake: 'Intake & classify',
  classify: 'Intake & classify',
  triage: 'Intake & classify',
  needs_info: 'Intake & classify',
  guide_reporter: 'Intake & classify',
  reproduce: 'Reproduce safely',
  start_reproduction: 'Reproduce safely',
  redirect: 'Intake & classify',
  evidence: 'Confirm the bug',
  confirm_bug: 'Confirm the bug',
  start_fix: 'Prepare the fix',
  validate: 'Confirm the bug',
  fix: 'Prepare the fix',
  review: 'Review & approve',
};

export function actorLabel(session: Pick<SessionSummary, 'kind' | 'actor'>): string {
  if (session.kind === 'reproduction') return 'Devin reproducer';
  if (session.kind === 'fix') return 'Devin coding agent';
  return session.actor;
}

export function transitionLabel(transition: string): string {
  return transitionLabels[transition] ?? transition.replace(/_/g, ' ');
}

export function mapApiStatus(status: AgentSessionStatus): DevinSessionStatus {
  switch (status) {
    case 'running':
      return 'Running';
    case 'queued':
      return 'Queued';
    case 'needs_attention':
      return 'Needs attention';
    case 'failed':
      return 'Failed';
    case 'cancelled':
      return 'Cancelled';
    case 'completed':
      return 'Completed';
  }
}

export function parseTime(value?: string | null): number | null {
  if (!value) return null;
  const ms = Date.parse(value);
  return Number.isNaN(ms) ? null : ms;
}

export function formatDuration(seconds: number): string {
  const total = Math.max(0, Math.floor(seconds));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  const mm = String(minutes).padStart(2, '0');
  const ss = String(secs).padStart(2, '0');
  return hours > 0 ? `${hours}:${mm}:${ss}` : `${mm}:${ss}`;
}

export function formatBudget(seconds: number): string {
  if (seconds % 3600 === 0) return `${seconds / 3600} h`;
  return `${Math.round(seconds / 60)} min`;
}

export function formatRelative(from: number | null, now: number): string {
  if (from === null) return 'Unknown';
  const diff = Math.max(0, Math.round((now - from) / 1000));
  if (diff < 5) return 'just now';
  if (diff < 60) return `${diff}s ago`;
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86_400) return `${Math.floor(diff / 3600)}h ago`;
  return `${Math.floor(diff / 86_400)}d ago`;
}

export function formatClock(value?: string | null): string {
  const ms = parseTime(value);
  if (ms === null) return 'Unknown';
  return new Date(ms).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false, timeZone: 'UTC' }) + ' UTC';
}

export function formatDateTime(value?: string | null): string {
  const ms = parseTime(value);
  if (ms === null) return 'Unknown';
  return new Date(ms).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short', timeZone: 'UTC' }) + ' UTC';
}

export function elapsedSeconds(startedAt: number | null, endedAt: number | null, now: number): number | null {
  if (startedAt === null) return null;
  return ((endedAt ?? now) - startedAt) / 1000;
}

function eventsFromApi(events: SessionEvent[], now: number): DevinSessionEvent[] {
  return events.map(event => ({
    label: event.label,
    detail: event.detail ?? '',
    time: event.state === 'pending' ? 'Next' : event.state === 'active' ? 'Now' : formatRelative(parseTime(event.occurred_at), now),
    state: event.state,
  }));
}

export function fromApiSummary(session: SessionSummary, now: number): SessionView {
  const startedAt = parseTime(session.started_at);
  const endedAt = parseTime(session.ended_at);
  const elapsed = elapsedSeconds(startedAt, endedAt, now);
  const updatedAt = parseTime(session.last_heartbeat_at) ?? endedAt ?? startedAt ?? parseTime(session.created_at);
  return {
    source: 'live',
    dryRun: session.dry_run,
    id: session.id,
    version: session.version,
    shortId: `DEV-${session.id.slice(0, 8)}`,
    issueId: session.issue_id,
    issueKey: session.issue_key,
    issueTitle: session.issue_title,
    title: session.title,
    flowStep: transitionLabel(session.transition),
    actor: actorLabel(session),
    status: mapApiStatus(session.status),
    apiStatus: session.status,
    started: startedAt === null ? 'Not started' : formatRelative(startedAt, now),
    startedAt,
    endedAt,
    elapsed: elapsed === null ? '00:00' : formatDuration(elapsed),
    updated: formatRelative(updatedAt, now),
    progress: normalizeProgress(session.progress),
    budget: formatBudget(session.budget.wall_seconds),
    budgetSeconds: session.budget.wall_seconds,
    environment: session.workspace.released ? 'Workspace released' : session.workspace.id ?? 'Waiting for slot',
    workspaceReleased: session.workspace.released,
    trigger: session.trigger,
    currentAction: session.current_action ?? null,
    nextCheckpoint: session.next_checkpoint ?? null,
    repository: session.repository.full_name,
    repositoryUrl: safeHref(session.repository.html_url, 'github') ?? TARGET_REPOSITORY_URL,
    commit: session.target_commit,
    branch: session.branch ?? null,
    events: [],
    artifactLabels: [],
    artifacts: null,
    conversation: null,
    outputs: null,
    pullRequests: null,
    review: null,
    links: null,
    humanGate: session.human_gate ?? null,
    correlationId: session.correlation_id,
  };
}

export function fromApiDetail(session: SessionDetail, now: number): SessionView {
  const base = fromApiSummary(session, now);
  return {
    ...base,
    status: mapApiStatus(session.status),
    events: eventsFromApi(session.events, now),
    artifactLabels: session.artifacts.map(artifact => artifact.label),
    artifacts: session.artifacts,
    conversation: session.conversation,
    outputs: session.outputs,
    pullRequests: session.pull_requests,
    review: session.review,
    links: session.links,
    humanGate: session.human_gate,
  };
}

export function isActiveStatus(status: DevinSessionStatus): boolean {
  return status === 'Running' || status === 'Queued';
}

/** Accepts only an explicit finite 0–100 value; anything else is "unavailable" rather than 0%. */
export function normalizeProgress(value: number | null | undefined): number | null {
  if (typeof value !== 'number' || !Number.isFinite(value)) return null;
  return Math.min(100, Math.max(0, value));
}

export function isWaitingOnHuman(session: Pick<SessionView, 'status' | 'humanGate'>): boolean {
  return session.status === 'Waiting on owner' || (session.humanGate !== null && session.humanGate.kind !== 'none');
}
