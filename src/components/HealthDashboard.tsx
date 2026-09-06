import {
  Activity,
  ArrowRight,
  Bot,
  CheckCircle2,
  Clock3,
  ExternalLink,
  Inbox,
  RefreshCw,
  ShieldAlert,
  TestTube2,
  Webhook,
} from 'lucide-react';
import type {
  AgentSessionStatus,
  DashboardSummary,
  Heartbeat,
  IssueSummary,
  ProviderStatus,
  RecentSession,
  SessionHealth,
  SessionKind,
  StageCount,
  SystemStatus,
} from '../api/types';
import { devinSessions, issues, type DevinSession, type Issue } from '../data';
import type { Resource } from '../hooks';
import { DataSourceBadge, ResourceNotice } from './DataSourceBadge';
import { LifecycleBadge } from './LiveIssueWorkbench';
import { SafeLink } from './SafeLink';

const STAGES: Omit<StageCount, 'count'>[] = [
  { id: 'intake', label: 'Intake & triage', kind: 'automation', actor: 'relay' },
  { id: 'clarify', label: 'Reporter clarification', kind: 'human', actor: 'reporter' },
  { id: 'reproduce', label: 'Bounded reproduction', kind: 'ai', actor: 'devin' },
  { id: 'confirm', label: 'Owner bug confirmation', kind: 'human', actor: 'owner' },
  { id: 'fix', label: 'Authorized fix', kind: 'ai', actor: 'devin' },
  { id: 'review', label: 'Review & merge approval', kind: 'human', actor: 'owner' },
  { id: 'done', label: 'Terminal outcome', kind: 'terminal', actor: 'relay' },
];

const demoStageFor: Record<Issue['state'], string> = {
  'Needs information': 'clarify',
  Reproducing: 'reproduce',
  'Owner decision': 'confirm',
  'Fix in progress': 'fix',
  'PR in review': 'review',
  Redirected: 'done',
  'Closed · inactive': 'done',
};

const demoStatusFor: Record<DevinSession['status'], AgentSessionStatus> = {
  Running: 'running',
  Queued: 'queued',
  'Needs attention': 'needs_attention',
  'Waiting on owner': 'completed',
  Completed: 'completed',
  Failed: 'failed',
  Cancelled: 'cancelled',
};

function demoKindFor(session: DevinSession): SessionKind | null {
  if (session.flowStep === 'Reproduce safely') return 'reproduction';
  if (session.flowStep === 'Prepare the fix') return 'fix';
  return null;
}

function parseElapsed(elapsed: string): number | null {
  const parts = elapsed.split(':').map(Number);
  if (parts.some(Number.isNaN)) return null;
  return parts.reduce((total, part) => total * 60 + part, 0);
}

function healthFor(kind: SessionKind, sessions: RecentSession[]): SessionHealth {
  const own = sessions.filter(session => session.kind === kind);
  const count = (status: AgentSessionStatus) => own.filter(session => session.status === status).length;
  const finished = count('completed') + count('failed');
  const durations = own
    .filter(session => session.status === 'completed' && session.duration_seconds !== null)
    .map(session => session.duration_seconds as number)
    .sort((a, b) => a - b);
  const median = durations.length ? durations[Math.floor(durations.length / 2)] : null;
  return {
    kind,
    queued: count('queued'),
    running: count('running'),
    completed: count('completed'),
    failed: count('failed'),
    needs_attention: count('needs_attention'),
    cancelled: count('cancelled'),
    total: own.length,
    success_rate_pct: finished ? Math.round((count('completed') / finished) * 1000) / 10 : null,
    median_duration_seconds: median,
    last_launched_at: own.length ? own[0].created_at : null,
  };
}

/**
 * Derives a dashboard shape from the committed mockup dataset. Used only when
 * the build explicitly allows demo data and no API answers; every liveness
 * signal is reported as absent because nothing is actually connected.
 */
export function demoDashboard(now = Date.now()): DashboardSummary {
  const generated = new Date(now).toISOString();
  const recent: RecentSession[] = devinSessions.flatMap(session => {
    const kind = demoKindFor(session);
    if (kind === null) return [];
    const status = demoStatusFor[session.status];
    const duration = status === 'completed' ? parseElapsed(session.elapsed) : null;
    return [
      {
        id: session.id,
        kind,
        issue_id: String(session.issueId),
        issue_key: session.issueKey,
        issue_title: session.issueTitle,
        status,
        dry_run: true,
        created_at: generated,
        started_at: status === 'queued' ? null : generated,
        ended_at: duration === null ? null : generated,
        duration_seconds: duration,
        devin_session_url: null,
      },
    ];
  });
  const completed = issues.filter(issue => demoStageFor[issue.state] === 'done').length;
  return {
    period: { from: generated, to: generated },
    throughput: {
      entered: issues.length,
      completed,
      buckets: [{ day: generated, entered: issues.length, completed }],
    },
    in_flight: STAGES.map(stage => ({
      ...stage,
      count: issues.filter(issue => demoStageFor[issue.state] === stage.id).length,
    })),
    sessions: [healthFor('reproduction', recent), healthFor('fix', recent)],
    recent_sessions: recent,
    heartbeat: {
      database: 'unavailable',
      worker: 'unavailable',
      github: 'unconfigured',
      devin: 'unconfigured',
      last_worker_heartbeat_at: null,
      last_webhook_received_at: null,
      last_webhook_event: null,
      last_session_launched_at: null,
      intake: 'none',
      last_automation_poll_at: null,
      polled_automation_id: null,
      automations: [],
      overall: 'down',
      reasons: ['Demo data: no Relay API, worker, GitHub webhook, or Devin connection is present.'],
    },
    generated_at: generated,
  };
}

export function formatDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return '–';
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ${Math.round(seconds % 60)}s`;
  const hours = Math.floor(minutes / 60);
  return `${hours}h ${minutes % 60}m`;
}

export function formatAgo(iso: string | null | undefined, now: number): string {
  if (!iso) return 'never';
  const delta = Math.max(0, now - new Date(iso).getTime());
  if (delta < 60_000) return `${Math.round(delta / 1000)}s ago`;
  if (delta < 3_600_000) return `${Math.round(delta / 60_000)}m ago`;
  if (delta < 86_400_000) return `${Math.round(delta / 3_600_000)}h ago`;
  return `${Math.round(delta / 86_400_000)}d ago`;
}

const providerLabel: Record<ProviderStatus, string> = {
  connected: 'Connected',
  dry_run: 'Dry run',
  stale: 'Stale',
  unconfigured: 'Not reported',
};

export function providerTone(status: ProviderStatus): 'ok' | 'warn' | 'down' {
  return status === 'connected' ? 'ok' : status === 'unconfigured' ? 'down' : 'warn';
}

const systemLabel: Record<SystemStatus, string> = { healthy: 'System healthy', degraded: 'System degraded', down: 'System down' };

export function SystemStatusPill({ status, reasons }: { status: SystemStatus; reasons: string[] }) {
  const Icon = status === 'healthy' ? CheckCircle2 : status === 'degraded' ? Activity : ShieldAlert;
  return (
    <span className={`system-status ${status}`} title={reasons.join('\n') || undefined} role="status">
      <Icon size={15} />
      {systemLabel[status]}
    </span>
  );
}

/** Issue intake is Devin-native (github:issues); Relay never enrolls issues from webhooks. */
function githubSignal(heartbeat: Heartbeat, now: number): string {
  if (heartbeat.intake === 'native' || heartbeat.intake === 'stale') {
    const poll = `Devin github:issues automation polled ${formatAgo(heartbeat.last_automation_poll_at, now)}`;
    return heartbeat.intake === 'stale' ? `${poll} (stale)` : poll;
  }
  return 'No Devin automation poll observed yet';
}

function HeartbeatPanel({ heartbeat, now }: { heartbeat: Heartbeat; now: number }) {
  const backend = heartbeat.database === 'ok' && heartbeat.worker === 'ok' ? 'ok' : heartbeat.database !== 'ok' || heartbeat.worker === 'unavailable' ? 'down' : 'warn';
  const backendLabel =
    heartbeat.database !== 'ok' ? 'Database unavailable' : heartbeat.worker === 'ok' ? 'API + worker online' : heartbeat.worker === 'stale' ? 'Worker heartbeat stale' : 'Worker not running';
  const rows: [string, 'ok' | 'warn' | 'down', string, string][] = [
    ['Backend', backend, backendLabel, `Worker heartbeat ${formatAgo(heartbeat.last_worker_heartbeat_at, now)}`],
    ['GitHub', providerTone(heartbeat.github), providerLabel[heartbeat.github], githubSignal(heartbeat, now)],
    ['Devin', providerTone(heartbeat.devin), providerLabel[heartbeat.devin], `Last session launched ${formatAgo(heartbeat.last_session_launched_at, now)}`],
  ];
  return (
    <div className="card heartbeat-card">
      <div className="card-header">
        <div>
          <h2>Liveness</h2>
          <p>Connection status and last observed signal per component</p>
        </div>
        <SystemStatusPill status={heartbeat.overall} reasons={heartbeat.reasons} />
      </div>
      <div className="heartbeat-rows">
        {rows.map(([name, tone, label, detail]) => (
          <div className="heartbeat-row" key={name}>
            <span className={`pulse-dot ${tone}`} />
            <div>
              <strong>{name}</strong>
              <span>{detail}</span>
            </div>
            <b className={`heartbeat-label ${tone}`}>{label}</b>
          </div>
        ))}
      </div>
      {heartbeat.reasons.length > 0 && (
        <ul className="heartbeat-reasons">
          {heartbeat.reasons.map(reason => (
            <li key={reason}>{reason}</li>
          ))}
        </ul>
      )}
    </div>
  );
}

function Metric({ label, value, note, icon, tone }: { label: string; value: string; note: string; icon: React.ReactNode; tone: string }) {
  return (
    <div className="metric-card card">
      <div className={`metric-icon ${tone}`}>{icon}</div>
      <div className="metric-copy">
        <span>{label}</span>
        <strong>{value}</strong>
        <p>{note}</p>
      </div>
    </div>
  );
}

function ThroughputChart({ buckets }: { buckets: DashboardSummary['throughput']['buckets'] }) {
  const max = Math.max(1, ...buckets.map(bucket => Math.max(bucket.entered, bucket.completed)));
  return (
    <div className="throughput-chart" role="img" aria-label="Issues entered and completed per day">
      {buckets.map(bucket => {
        const day = new Date(bucket.day);
        return (
          <div className="throughput-day" key={bucket.day} title={`${day.toLocaleDateString()} · ${bucket.entered} entered · ${bucket.completed} completed`}>
            <div className="throughput-bars">
              <i className="entered" style={{ height: `${(bucket.entered / max) * 100}%` }} />
              <i className="completed" style={{ height: `${(bucket.completed / max) * 100}%` }} />
            </div>
            <span>{day.toLocaleDateString(undefined, { month: 'numeric', day: 'numeric' })}</span>
          </div>
        );
      })}
    </div>
  );
}

const sessionKindLabel: Record<SessionKind, string> = { reproduction: 'Reproduction sessions', fix: 'Fix sessions' };
const sessionKindNote: Record<SessionKind, string> = {
  reproduction: 'Auto-launched when triage finds a likely defect with context completeness ≥ 80%',
  fix: 'Launched only after a human owner authorizes the fix',
};

function SessionHealthCard({ health, onOpen }: { health: SessionHealth; onOpen: () => void }) {
  return (
    <div className="card session-health-card">
      <div className="card-header">
        <div>
          <h2>{sessionKindLabel[health.kind]}</h2>
          <p>{sessionKindNote[health.kind]}</p>
        </div>
        <button className="text-button" onClick={onOpen}>
          Sessions <ArrowRight size={14} />
        </button>
      </div>
      <div className="session-health-counts">
        {(
          [
            ['Queued', health.queued, 'queued'],
            ['Running', health.running, 'running'],
            ['Succeeded', health.completed, 'completed'],
            ['Failed', health.failed + health.needs_attention, 'failed'],
          ] as [string, number, string][]
        ).map(([label, value, tone]) => (
          <div className={`session-count ${tone}`} key={label}>
            <strong>{value}</strong>
            <span>{label}</span>
          </div>
        ))}
      </div>
      <div className="session-health-footer">
        <span>
          Success rate <strong>{health.success_rate_pct === null ? '–' : `${health.success_rate_pct}%`}</strong>
        </span>
        <span>
          Median duration <strong>{formatDuration(health.median_duration_seconds)}</strong>
        </span>
        <span>
          Total <strong>{health.total}</strong>
        </span>
      </div>
    </div>
  );
}

const statusLabel: Record<AgentSessionStatus, string> = {
  queued: 'Queued',
  running: 'Running',
  completed: 'Succeeded',
  failed: 'Failed',
  needs_attention: 'Needs attention',
  cancelled: 'Cancelled',
};

function RecentSessions({ sessions, now, goToIssue }: { sessions: RecentSession[]; now: number; goToIssue: (id: string) => void }) {
  return (
    <div className="card recent-sessions-card">
      <div className="card-header">
        <div>
          <h2>Recent Devin sessions</h2>
          <p>Type, linked issue, status, and duration from persisted session records</p>
        </div>
      </div>
      <div className="recent-sessions">
        <div className="recent-sessions-head">
          <span>Type</span>
          <span>Issue</span>
          <span>Status</span>
          <span>Duration</span>
          <span>Link</span>
        </div>
        {sessions.map(session => (
          <div className="recent-sessions-row" key={session.id}>
            <span className={`kind-chip ${session.kind}`}>
              {session.kind === 'reproduction' ? <TestTube2 size={13} /> : <Bot size={13} />}
              {session.kind === 'reproduction' ? 'Reproduction' : 'Fix'}
            </span>
            <button className="link-button" onClick={() => goToIssue(session.issue_id)}>
              {session.issue_key}
              <small>{session.issue_title}</small>
            </button>
            <span className={`session-status ${session.status.replace('needs_attention', 'attention')}`}>
              <i />
              {statusLabel[session.status]}
              {session.dry_run && <em> · dry run</em>}
            </span>
            <span>{session.duration_seconds === null ? (session.started_at ? `${formatDuration((now - new Date(session.started_at).getTime()) / 1000)} elapsed` : '–') : formatDuration(session.duration_seconds)}</span>
            <span>
              {session.devin_session_url ? (
                <SafeLink href={session.devin_session_url} policy="devin" className="text-button">
                  Open <ExternalLink size={13} />
                </SafeLink>
              ) : (
                <small>{session.dry_run ? 'no external session' : 'pending'}</small>
              )}
            </span>
          </div>
        ))}
        {sessions.length === 0 && <p className="session-empty">No Devin sessions have been recorded yet.</p>}
      </div>
    </div>
  );
}

export function HealthDashboard({
  dashboard,
  live,
  attention,
  now,
  goToIssue,
  goToSessions,
  goToIssues,
}: {
  dashboard: Resource<DashboardSummary>;
  live: boolean;
  attention: Resource<{ items: IssueSummary[] }>;
  now: number;
  goToIssue: (id: string) => void;
  goToSessions: () => void;
  goToIssues: () => void;
}) {
  const data = live ? dashboard.data : demoDashboard(now);
  const attentionItems = live ? attention.data?.items ?? [] : [];
  const inFlight = data ? data.in_flight.filter(stage => stage.id !== 'done').reduce((sum, stage) => sum + stage.count, 0) : null;
  const running = data ? data.sessions.reduce((sum, health) => sum + health.running, 0) : null;
  const funnel = data ? data.in_flight.filter(stage => stage.id !== 'done') : [];
  const funnelMax = Math.max(1, ...funnel.map(stage => stage.count));
  return (
    <>
      <div className="page-header">
        <div>
          <p className="eyebrow">{data ? `Since ${new Date(data.period.from).toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' })}` : 'Waiting for live data'}</p>
          <h1>System health & throughput</h1>
          <p>Is the pipeline working? Every value below is computed from Relay's persisted issue, session, and heartbeat records.</p>
        </div>
        <div className="page-header-actions">
          {data && <SystemStatusPill status={data.heartbeat.overall} reasons={data.heartbeat.reasons} />}
          <DataSourceBadge state={live ? dashboard.state : { kind: 'demo', reason: 'API unavailable' }} />
          <button className="secondary-button" onClick={() => void dashboard.refresh()} disabled={!live}>
            <RefreshCw size={15} /> Refresh
          </button>
        </div>
      </div>

      {live && <ResourceNotice state={dashboard.state} resourceLabel="dashboard" />}

      <section className="metric-grid" aria-label="Throughput">
        <Metric label="Issues entered" value={data ? String(data.throughput.entered) : '–'} note="enrolled by the Devin github:issues automation" icon={<Inbox size={19} />} tone="violet" />
        <Metric label="Issues completed" value={data ? String(data.throughput.completed) : '–'} note="reached a terminal outcome" icon={<CheckCircle2 size={19} />} tone="green" />
        <Metric label="In flight" value={inFlight === null ? '–' : String(inFlight)} note="across non-terminal stages" icon={<Clock3 size={19} />} tone="blue" />
        <Metric label="Devin sessions running" value={running === null ? '–' : String(running)} note="reproduction + fix" icon={<Bot size={19} />} tone="amber" />
      </section>

      <section className="dashboard-grid">
        <div className="card volume-card">
          <div className="card-header">
            <div>
              <h2>Pipeline throughput</h2>
              <p>Issues entered vs. completed per day (last {data?.throughput.buckets.length ?? 14} days)</p>
            </div>
            <div className="chart-legend">
              <span><i className="legend-swatch entered" /> Entered</span>
              <span><i className="legend-swatch completed" /> Completed</span>
            </div>
          </div>
          {data ? <ThroughputChart buckets={data.throughput.buckets} /> : <p className="session-empty">Waiting for dashboard data.</p>}
        </div>

        {data ? <HeartbeatPanel heartbeat={data.heartbeat} now={now} /> : <div className="card heartbeat-card"><p className="session-empty">Waiting for heartbeat data.</p></div>}

        <div className="card funnel-card">
          <div className="card-header">
            <div>
              <h2>In flight by stage</h2>
              <p>Current issue count per flow stage</p>
            </div>
            <button className="text-button" onClick={goToIssues}>
              Workbench <ArrowRight size={14} />
            </button>
          </div>
          <div className="funnel">
            {funnel.map(stage => (
              <div className="funnel-row" key={stage.id}>
                <span>{stage.label}</span>
                <div className="funnel-track">
                  <i className={`stage-${stage.kind}`} style={{ width: `${Math.round((stage.count / funnelMax) * 100)}%` }} />
                </div>
                <strong>{stage.count}</strong>
              </div>
            ))}
          </div>
        </div>

        <div className="session-health-stack">
          {(data?.sessions ?? []).map(health => (
            <SessionHealthCard key={health.kind} health={health} onOpen={goToSessions} />
          ))}
        </div>
      </section>

      <section className="lower-grid">
        <RecentSessions sessions={data?.recent_sessions ?? []} now={now} goToIssue={goToIssue} />

        <div className="card active-table-card">
          <div className="card-header">
            <div>
              <h2>Needs attention</h2>
              <p>Blocked, failed, or waiting on a human decision</p>
            </div>
            <button className="text-button" onClick={goToIssues}>
              Open workbench <ArrowRight size={14} />
            </button>
          </div>
          <div className="attention-list">
            {live ? (
              attentionItems.slice(0, 6).map(issue => (
                <button className="attention-row" key={issue.id} onClick={() => goToIssue(issue.id)}>
                  <span className="issue-title-cell">
                    <small>{issue.key}</small>
                    <strong>{issue.title}</strong>
                  </span>
                  <LifecycleBadge state={issue.state} />
                  <span className="next-cell">{issue.next_action}</span>
                </button>
              ))
            ) : (
              <p className="session-empty">
                <Webhook size={14} /> Demo mode: no live issue records. Connect the Relay API to see issues that need attention.
              </p>
            )}
            {live && attentionItems.length === 0 && <p className="session-empty">No blocked, failed, or decision-pending issues need attention.</p>}
          </div>
        </div>
      </section>
    </>
  );
}
