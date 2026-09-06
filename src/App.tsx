import {
  Activity,
  AlertTriangle,
  ArrowDownRight,
  ArrowRight,
  Bell,
  Bot,
  Box,
  BrainCircuit,
  Check,
  ChevronRight,
  CircleDot,
  Clock3,
  FileCheck2,
  Filter,
  GitPullRequest,
  GitPullRequestArrow,
  HelpCircle,
  Inbox,
  LayoutDashboard,
  LockKeyhole,
  Menu,
  MessageCircleMore,
  MoreHorizontal,
  Pause,
  Play,
  Plus,
  RefreshCw,
  Loader2,
  ShieldAlert,
  Search,
  Send,
  Settings2,
  ShieldCheck,
  SlidersHorizontal,
  Sparkles,
  TerminalSquare,
  TestTube2,
  TimerReset,
  Users,
  WandSparkles,
  X,
  Zap,
} from 'lucide-react';
import { useState } from 'react';
import {
  devinSessions,
  issues,
  type Issue,
  type ViewKey,
} from './data';
import {
  fromApiDetail,
  fromApiSummary,
  isWaitingOnHuman,
  fromDemoSession,
  useAnalytics,
  useApiStatus,
  useDashboard,
  useDryRun,
  useNow,
  useSession,
  useSessions,
  type ApiStatus,
  type SessionView,
} from './hooks';
import {
  AutomationsView,
  DataSourceBadge,
  LiveIssueWorkbench,
  RepositorySafetyNotice,
  ResourceNotice,
  SessionDetailView,
  SessionStatusBadge,
  isRepositorySafetyError,
  useLiveIssueList,
  type IssueListFilter,
} from './components';
import { OPERATOR_TOKEN_STORAGE_KEY, type Heartbeat, type ProviderStatus } from './api';
import { HealthDashboard, formatAgo } from './components/HealthDashboard';

const navItems: { key: ViewKey; label: string; icon: typeof LayoutDashboard }[] = [
  { key: 'overview', label: 'Health dashboard', icon: LayoutDashboard },
  { key: 'sessions', label: 'Devin sessions', icon: Activity },
  { key: 'automations', label: 'Devin Automations', icon: Zap },
  { key: 'issues', label: 'Issue workbench', icon: Inbox },
  { key: 'settings', label: 'Connections', icon: Settings2 },
];

const stateClass: Record<Issue['state'], string> = {
  'Needs information': 'amber',
  Reproducing: 'violet',
  'Owner decision': 'green',
  'Fix in progress': 'blue',
  'PR in review': 'blue',
  Redirected: 'gray',
  'Closed · inactive': 'rose',
};

function Logo() {
  return (
    <div className="logo-mark" aria-hidden="true">
      <span />
      <span />
      <span />
    </div>
  );
}

function App() {
  const [view, setView] = useState<ViewKey>('overview');
  const [selectedIssueId, setSelectedIssueId] = useState(43218);
  const [selectedLiveIssueId, setSelectedLiveIssueId] = useState<string | null>(null);
  const [issueFilter, setIssueFilter] = useState<IssueListFilter>('all');
  const [issueSearch, setIssueSearch] = useState('');
  const [toast, setToast] = useState<string | null>(null);
  const [mobileNav, setMobileNav] = useState(false);
  const selectedIssue = issues.find(issue => issue.id === selectedIssueId) ?? issues[0];

  const api = useApiStatus();
  const live = api.mode === 'live' || api.mode === 'degraded';
  const demo = api.mode === 'demo';
  // While checking, or after a reachable API failure, no issue/session records may render — live or demo.
  const gated = api.mode === 'checking' || api.mode === 'error';
  const liveSessions = useSessions({}, live);
  const liveIssues = useLiveIssueList(issueFilter, issueSearch, live);
  const analytics = useAnalytics(live);
  const dashboard = useDashboard(live);
  const attention = useLiveIssueList('attention', '', live);
  const now = useNow(1000);
  const runningSessionCount = live
    ? liveSessions.data?.items.filter(session => session.status === 'running').length ?? 0
    : demo
      ? devinSessions.filter(session => session.status === 'Running').length
      : null;
  const issueCount = live ? liveIssues.data?.total ?? 0 : demo ? issues.length : null;

  const notify = (message: string) => {
    setToast(message);
    window.setTimeout(() => setToast(null), 2600);
  };

  const goToIssue = (id: number | string) => {
    if (typeof id === 'number') setSelectedIssueId(id);
    else if (/^\d+$/.test(id)) setSelectedIssueId(Number(id));
    else setSelectedLiveIssueId(id);
    setView('issues');
  };

  const dryRun = useDryRun(() => notify('Dry-run accepted by the API'));
  const runDryTest = () => {
    setView('sessions');
    if (live) void dryRun.run();
    else if (demo) notify('Dry-run reproduction session queued (simulated)');
    else notify('Relay API is not available; dry-run was not sent');
  };

  return (
    <div className="app-shell">
      <aside className={`sidebar ${mobileNav ? 'sidebar-open' : ''}`}>
        <div className="brand">
          <Logo />
          <div>
            <strong>Relay</strong>
            <span>Issue operations</span>
          </div>
          <button className="mobile-close" onClick={() => setMobileNav(false)} aria-label="Close menu">
            <X size={18} />
          </button>
        </div>

        <nav className="primary-nav" aria-label="Primary">
          <p className="nav-label">Workspace</p>
          {navItems.map(item => {
            const Icon = item.icon;
            return (
              <button
                key={item.key}
                className={view === item.key ? 'nav-item active' : 'nav-item'}
                onClick={() => {
                  setView(item.key);
                  setMobileNav(false);
                }}
              >
                <Icon size={18} strokeWidth={1.9} />
                <span>{item.label}</span>
                {item.key === 'sessions' && <b>{runningSessionCount ?? '–'}</b>}
                {item.key === 'issues' && <b>{issueCount ?? '–'}</b>}
              </button>
            );
          })}
        </nav>

        <div className="sidebar-section">
          <p className="nav-label">Saved views</p>
          {(
            [
              ['waiting', 'amber-dot', 'Waiting on reporter', ['awaiting_reporter'], 8],
              ['decision', 'green-dot', 'Owner decisions', ['needs_owner_decision'], 5],
              ['review', 'blue-dot', 'PRs in review', ['pr_open', 'awaiting_owner', 'changes_requested'], 11],
            ] as [IssueListFilter, string, string, string[], number][]
          ).map(([key, dot, label, states, demoCount]) => {
            const counts = analytics.data?.state_counts;
            const count = live ? states.reduce((sum, state) => sum + (counts?.[state as keyof typeof counts] ?? 0), 0) : demo ? demoCount : null;
            return (
              <button
                className="saved-view"
                key={key}
                onClick={() => {
                  setIssueFilter(key);
                  setView('issues');
                }}
              >
                <span className={`dot ${dot}`} />
                {label}
                <b>{count === null || (live && !analytics.data) ? '–' : count}</b>
              </button>
            );
          })}
        </div>

        <div className="sidebar-spacer" />
        <div className="environment-card">
          <div className="environment-title">
            <span className={`pulse-dot ${api.mode}`} />
            {api.mode === 'checking' ? 'Checking API…' : api.mode === 'error' ? 'Relay API error' : live ? 'Connected to Relay API' : 'Demo environment'}
          </div>
          <p>
            {live ? (
              <>Events, sessions, and PRs are scoped to <strong>exloong/superset</strong>.{api.mode === 'degraded' && api.reason ? <> Degraded: {api.reason}.</> : null}</>
            ) : api.mode === 'error' ? (
              <>
                {api.error?.apiAbsent ? 'The API did not respond' : 'The API response is not usable'}: {api.reason}. No records are shown; demo data is disabled.
              </>
            ) : api.mode === 'checking' ? (
              <>Waiting for the Relay API health and readiness probes.</>
            ) : (
              <>API unreachable; showing committed demo data for <strong>exloong/superset</strong>. Nothing here is live.</>
            )}
          </p>
          <button onClick={() => setView('settings')}>
            Review safeguards <ArrowRight size={14} />
          </button>
        </div>
        <div className="profile">
          <div className="profile-avatar">XZ</div>
          <div>
            <strong>Xiangyu Zhou</strong>
            <span>Workspace admin</span>
          </div>
          <MoreHorizontal size={18} />
        </div>
      </aside>

      <main className="main">
        <header className="topbar">
          <div className="mobile-brand">
            <button onClick={() => setMobileNav(true)} aria-label="Open menu">
              <Menu size={21} />
            </button>
            <Logo />
          </div>
          <div className="top-search">
            <Search size={17} />
            <input aria-label="Search issues" placeholder="Search issues, owners, or runs…" />
            <kbd>⌘ K</kbd>
          </div>
          <div className="top-actions">
            <ModePill api={api} />
            <button className="icon-button" aria-label="Notifications">
              <Bell size={18} />
              <span className="notification-dot" />
            </button>
            <button className="primary-button" onClick={runDryTest} disabled={dryRun.pending}>
              <Play size={16} fill="currentColor" />
              Run dry test
            </button>
          </div>
        </header>

        <div className="page">
          {gated && view !== 'settings' && <ApiGate api={api} />}
          {!gated && view === 'overview' && (
            <HealthDashboard
              dashboard={dashboard}
              live={live}
              attention={attention}
              now={now}
              goToIssue={goToIssue}
              goToSessions={() => setView('sessions')}
              goToIssues={() => setView('issues')}
            />
          )}
          {!gated && view === 'sessions' && <DevinSessions goToIssue={goToIssue} notify={notify} live={live} sessions={liveSessions} onRunDryTest={runDryTest} />}
          {!gated && view === 'automations' && <AutomationsView live={live} goToIssue={goToIssue} notify={notify} />}
          {view === 'issues' && demo && (
            <IssueWorkbench
              selected={selectedIssue}
              onSelect={setSelectedIssueId}
              notify={notify}
            />
          )}
          {view === 'issues' && live && (
            <>
              <PageHeader
                eyebrow="Live workbench"
                title="Issue queue"
                description="Reporter questions, evidence packets, owner routing, and human decisions from the Relay API."
                actions={
                  <button className="secondary-button" onClick={() => void liveIssues.refresh()}>
                    <RefreshCw size={15} /> Refresh
                  </button>
                }
              />
              {liveIssues.state.kind === 'error' && isRepositorySafetyError(liveIssues.state.error) && <RepositorySafetyNotice error={liveIssues.state.error} />}
              <LiveIssueWorkbench
                issues={liveIssues}
                selectedId={selectedLiveIssueId}
                onSelect={setSelectedLiveIssueId}
                filter={issueFilter}
                onFilter={setIssueFilter}
                search={issueSearch}
                onSearch={setIssueSearch}
                notify={notify}
              />
            </>
          )}
          {view === 'settings' && <Configuration notify={notify} dashboard={dashboard} live={live} />}
        </div>
      </main>
      {mobileNav && <button className="nav-scrim" onClick={() => setMobileNav(false)} aria-label="Close menu" />}
      {toast && (
        <div className="toast">
          <Check size={17} />
          {toast}
        </div>
      )}
    </div>
  );
}

function ApiGate({ api }: { api: ApiStatus }) {
  if (api.mode === 'checking') {
    return (
      <section className="api-gate checking" role="status" aria-live="polite">
        <Loader2 size={22} className="spin" />
        <div>
          <strong>Checking the Relay API</strong>
          <p>Waiting for <code>/api/v1/health</code>{api.health ? <> and <code>/api/v1/ready</code></> : null} before showing any records.</p>
        </div>
      </section>
    );
  }
  const absent = api.error?.apiAbsent === true;
  return (
    <section className="api-gate error" role="alert">
      <ShieldAlert size={22} />
      <div>
        <strong>{absent ? 'Relay API did not respond' : 'Relay API returned an error'} — no records shown</strong>
        <p>
          {api.reason ?? (absent ? 'No response was received from the API.' : 'The API response could not be trusted.')}
          {api.error?.status ? <> (HTTP {api.error.status}, {api.error.code})</> : api.error ? <> ({api.error.code})</> : null}
          {api.error?.correlationId ? <> · correlation {api.error.correlationId}</> : null}
        </p>
        <p>{absent ? 'Demo data is disabled in production. Restore the API and retry.' : 'Demo data is disabled while the API rejects or returns an invalid response. Fix the API or authentication and retry.'}</p>
        <button className="secondary-button" onClick={() => void api.refresh()}>
          <RefreshCw size={15} /> Retry health check
        </button>
      </div>
    </section>
  );
}

function ModePill({ api }: { api: ApiStatus }) {
  const label =
    api.mode === 'checking'
      ? 'Checking API'
      : api.mode === 'live'
        ? 'Live · exloong/superset'
        : api.mode === 'degraded'
          ? `Degraded · ${api.reason ?? 'API'}`
          : api.mode === 'error'
            ? `API error · ${api.reason ?? 'unusable response'}`
            : 'Demo data';
  return (
    <div className={`mode-pill ${api.mode}`} title={api.reason ?? (api.health?.version ? `API ${api.health.version}` : undefined)} role="status">
      <span />
      {label}
    </div>
  );
}

function PageHeader({
  eyebrow,
  title,
  description,
  actions,
}: {
  eyebrow: string;
  title: string;
  description: string;
  actions?: React.ReactNode;
}) {
  return (
    <div className="page-header">
      <div>
        <p className="eyebrow">{eyebrow}</p>
        <h1>{title}</h1>
        <p>{description}</p>
      </div>
      {actions && <div className="page-header-actions">{actions}</div>}
    </div>
  );
}

function StateBadge({ state }: { state: Issue['state'] }) {
  return <span className={`state-badge ${stateClass[state]}`}><i />{state}</span>;
}

function Avatar({ initials, size = 'normal' }: { initials: string; size?: 'normal' | 'small' }) {
  return <span className={`avatar ${size}`}>{initials}</span>;
}

function DevinSessions({
  goToIssue,
  notify,
  live,
  sessions,
  onRunDryTest,
}: {
  goToIssue: (id: string) => void;
  notify: (message: string) => void;
  live: boolean;
  sessions: ReturnType<typeof useSessions>;
  onRunDryTest: () => void;
}) {
  const [selectedSessionId, setSelectedSessionId] = useState<string | null>(null);
  const [filter, setFilter] = useState<'live' | 'attention' | 'all'>('live');
  const now = Date.now();

  const allSessions: SessionView[] = live
    ? (sessions.data?.items ?? []).map(session => fromApiSummary(session, now))
    : devinSessions.map(fromDemoSession);

  const visibleSessions = allSessions.filter(session => {
    if (filter === 'attention') return session.status === 'Needs attention' || session.status === 'Failed';
    if (filter === 'all') return true;
    return isWaitingOnHuman(session) || (session.status !== 'Completed' && session.status !== 'Cancelled' && session.status !== 'Failed');
  });
  const selectedSummary = visibleSessions.find(session => session.id === selectedSessionId) ?? visibleSessions[0] ?? allSessions[0] ?? null;
  const detail = useSession(live && selectedSummary ? selectedSummary.id : null, live);
  const selected: SessionView | null =
    live && detail.data && selectedSummary && detail.data.id === selectedSummary.id ? fromApiDetail(detail.data, now) : selectedSummary;

  const count = (status: SessionView['status']) => allSessions.filter(session => session.status === status).length;
  const capacity = live ? sessions.data?.capacity : undefined;
  const slots = capacity?.slots ?? (live ? null : 6);
  const releasedWaiting = allSessions.filter(isWaitingOnHuman);
  const allReleased = releasedWaiting.every(session => session.workspaceReleased);

  return (
    <>
      <PageHeader
        eyebrow="Bounded agent execution"
        title="Devin sessions"
        description="See every agent run created by the issue flow, what triggered it, and where human attention is required."
        actions={
          <>
            <button className="secondary-button" onClick={() => (live ? void sessions.refresh() : notify('Demo data does not refresh'))}>
              <RefreshCw size={15} /> <DataSourceBadge state={live ? sessions.state : { kind: 'demo', reason: 'API unavailable' }} compact />
            </button>
            <button className="primary-button" onClick={onRunDryTest}>
              <Plus size={15} /> Run dry test
            </button>
          </>
        }
      />

      <section className="session-summary" aria-label="Devin session summary">
        <div className="card">
          <span className="session-summary-icon running"><Activity size={17} /></span>
          <div><strong>{count('Running')}</strong><span>Agents running</span></div>
          <small>{slots !== null ? `of ${slots} workspace slots` : 'capacity not reported'}</small>
        </div>
        <div className="card">
          <span className="session-summary-icon queued"><Clock3 size={17} /></span>
          <div><strong>{count('Queued')}</strong><span>Queued trigger</span></div>
          <small>
            {live
              ? capacity?.next_slot_eta_seconds !== undefined
                ? `next slot in ~${Math.max(1, Math.round(capacity.next_slot_eta_seconds / 60))} min`
                : 'next slot ETA not reported'
              : 'next slot in ~4 min'}
          </small>
        </div>
        <div className="card">
          <span className="session-summary-icon attention"><AlertTriangle size={17} /></span>
          <div><strong>{count('Needs attention') + count('Failed')}</strong><span>Needs attention</span></div>
          <small>{live ? 'recovery or retry required' : 'environment recovery'}</small>
        </div>
        <div className="card">
          <span className={`session-summary-icon ${allReleased ? 'released' : 'attention'}`}><Pause size={17} /></span>
          <div><strong>{releasedWaiting.length}</strong><span>Waiting on human</span></div>
          <small>{allReleased ? 'workspace already released' : 'workspace still allocated!'}</small>
        </div>
      </section>

      {live && sessions.state.kind === 'error' && isRepositorySafetyError(sessions.state.error) && <RepositorySafetyNotice error={sessions.state.error} />}

      <section className="session-monitor card">
        <aside className="session-list-panel">
          <div className="session-list-head">
            <div>
              <p className="eyebrow">Flow-triggered runs</p>
              <strong>Session queue</strong>
            </div>
            <DataSourceBadge state={live ? sessions.state : { kind: 'demo', reason: 'API unavailable' }} />
          </div>
          <div className="session-filters">
            <button className={filter === 'live' ? 'active' : ''} onClick={() => setFilter('live')}>Live & waiting</button>
            <button className={filter === 'attention' ? 'active' : ''} onClick={() => setFilter('attention')}>Attention</button>
            <button className={filter === 'all' ? 'active' : ''} onClick={() => setFilter('all')}>All</button>
          </div>
          <div className="session-list">
            {live && <ResourceNotice state={sessions.state} resourceLabel="sessions" />}
            {visibleSessions.length === 0 && allSessions.length > 0 && <p className="session-empty">No sessions match this filter.</p>}
            {visibleSessions.map(session => (
              <button
                className={`session-list-item ${selected?.id === session.id ? 'selected' : ''}`}
                onClick={() => setSelectedSessionId(session.id)}
                key={session.id}
              >
                <div className="session-list-row">
                  <SessionStatusBadge status={session.status} />
                  <small>{session.shortId}</small>
                </div>
                <strong>{session.title}</strong>
                <span>{session.issueKey} · {session.flowStep}</span>
                {session.progress !== null && (
                  <div className="session-mini-progress">
                    <i style={{ width: `${session.progress}%` }} />
                  </div>
                )}
                <div className="session-list-foot">
                  <span><Clock3 size={11} /> {session.elapsed}</span>
                  <span>{session.updated}</span>
                </div>
              </button>
            ))}
          </div>
          <div className="session-list-policy">
            <ShieldCheck size={15} />
            <span><strong>No idle agents.</strong> Human waits release the workspace and resume through a new event.</span>
          </div>
        </aside>

        {selected ? (
          <SessionDetailView
            session={selected}
            detailState={live ? detail.state : undefined}
            goToIssue={goToIssue}
            notify={notify}
            onRefresh={live ? () => void Promise.all([sessions.refresh(), detail.refresh()]) : undefined}
          />
        ) : (
          <div className="session-detail session-detail-empty">
            {live ? <ResourceNotice state={sessions.state} resourceLabel="sessions" /> : null}
            {live && sessions.state.kind === 'empty' && (
              <p className="session-empty">The API is reachable and reports no sessions. Demo sessions are hidden while the API is live.</p>
            )}
          </div>
        )}
      </section>
    </>
  );
}

function IssueWorkbench({
  selected,
  onSelect,
  notify,
}: {
  selected: Issue;
  onSelect: (id: number) => void;
  notify: (message: string) => void;
}) {
  const [tab, setTab] = useState<'conversation' | 'evidence' | 'handoff'>('conversation');
  const [simulationStep, setSimulationStep] = useState(0);
  const simulationLabels = ['Awaiting reporter', 'Reply received', 'Reproduction queued', 'Evidence ready'];

  const advanceSimulation = () => {
    const next = Math.min(simulationStep + 1, simulationLabels.length - 1);
    setSimulationStep(next);
    notify(simulationLabels[next]);
  };

  return (
    <>
      <PageHeader
        eyebrow="Dry-run workbench"
        title="Issue queue"
        description="Review the reporter conversation, evidence package, and human decisions in context."
        actions={
          <>
            <button className="secondary-button"><Filter size={15} /> Filter</button>
            <button className="primary-button" onClick={advanceSimulation}>
              <Play size={16} fill="currentColor" /> Advance simulation
            </button>
          </>
        }
      />

      <section className="workbench card">
        <aside className="issue-list">
          <div className="issue-list-head">
            <strong>Active queue</strong>
            <span>6 issues</span>
          </div>
          <div className="mini-search">
            <Search size={15} />
            <input aria-label="Filter issue queue" placeholder="Filter queue…" />
          </div>
          <div className="issue-filter-tabs">
            <button className="active">Priority</button>
            <button>Recent</button>
            <button>Owner</button>
          </div>
          <div className="issue-list-scroll">
            {issues.map(issue => (
              <button
                key={issue.id}
                className={selected.id === issue.id ? 'issue-list-item selected' : 'issue-list-item'}
                onClick={() => onSelect(issue.id)}
              >
                <div className="list-item-meta">
                  <span>{issue.key}</span>
                  <small>{issue.updated}</small>
                </div>
                <strong>{issue.title}</strong>
                <div className="list-item-footer">
                  <StateBadge state={issue.state} />
                  <Avatar initials={issue.ownerInitials} size="small" />
                </div>
              </button>
            ))}
          </div>
        </aside>

        <div className="issue-detail">
          <div className="issue-detail-header">
            <div>
              <div className="issue-kicker">
                <span>{selected.key}</span>
                <span>·</span>
                <span>{selected.category}</span>
                <span>·</span>
                <span>Opened {selected.age} ago</span>
              </div>
              <h2>{selected.title}</h2>
              <div className="issue-byline">
                <Avatar initials={selected.avatar} size="small" />
                Reported by <strong>{selected.author}</strong>
                <span>·</span>
                <StateBadge state={selected.state} />
              </div>
            </div>
            <div className="detail-actions">
              <button className="secondary-button"><GitPullRequestArrow size={15} /> GitHub</button>
              <button className="icon-button" aria-label="More issue actions"><MoreHorizontal size={18} /></button>
            </div>
          </div>

          <div className="lifecycle-track">
            {['Intake', 'Classify', 'Context', 'Reproduce', 'Confirm', 'Fix', 'Review'].map((label, index) => {
              const baseProgress = Math.ceil(selected.progress / 15);
              const progress = selected.id === 43218 ? Math.max(baseProgress, 2 + simulationStep) : baseProgress;
              return (
                <div className={index < progress ? 'track-step complete' : index === progress ? 'track-step current' : 'track-step'} key={label}>
                  <i>{index < progress ? <Check size={12} /> : index + 1}</i>
                  <span>{label}</span>
                  {index < 6 && <b />}
                </div>
              );
            })}
          </div>

          <div className="detail-tabs">
            <button className={tab === 'conversation' ? 'active' : ''} onClick={() => setTab('conversation')}>
              <MessageCircleMore size={16} /> Conversation
            </button>
            <button className={tab === 'evidence' ? 'active' : ''} onClick={() => setTab('evidence')}>
              <FileCheck2 size={16} /> Evidence pack <span>6</span>
            </button>
            <button className={tab === 'handoff' ? 'active' : ''} onClick={() => setTab('handoff')}>
              <Users size={16} /> Owner handoff
            </button>
          </div>

          <div className="detail-content">
            {tab === 'conversation' && (
              <Conversation issue={selected} simulationStep={simulationStep} advance={advanceSimulation} notify={notify} />
            )}
            {tab === 'evidence' && <EvidencePack issue={selected} notify={notify} />}
            {tab === 'handoff' && <OwnerHandoff issue={selected} notify={notify} />}
          </div>
        </div>

        <aside className="context-panel">
          <div className="context-section">
            <p className="context-title">Current state</p>
            <StateBadge state={selected.state} />
            <div className="progress-line"><i style={{ width: `${selected.progress}%` }} /></div>
            <span className="progress-copy">{selected.progress}% through lifecycle</span>
          </div>
          <div className="context-section">
            <p className="context-title">Next action</p>
            <div className="next-action-box">
              <div className="next-action-icon"><MessageCircleMore size={17} /></div>
              <div><strong>{selected.nextAction}</strong><span>{selected.due}</span></div>
            </div>
          </div>
          <div className="context-section">
            <p className="context-title">Suggested owner</p>
            <div className="owner-profile">
              <Avatar initials={selected.ownerInitials} />
              <div><strong>{selected.owner}</strong><span>Based on component map</span></div>
              <ChevronRight size={16} />
            </div>
          </div>
          <div className="context-section">
            <p className="context-title">Triage confidence</p>
            <div className="confidence-value"><strong>{selected.confidence}%</strong><span>High</span></div>
            <div className="confidence-track"><i style={{ width: `${selected.confidence}%` }} /></div>
            <button className="link-button">View reasoning</button>
          </div>
          <div className="context-section">
            <p className="context-title">Automation safety</p>
            <div className="safety-list">
              <span><Check size={13} /> Fork writes only</span>
              <span><Check size={13} /> Secret scan passed</span>
              <span><Check size={13} /> Human fix gate</span>
            </div>
          </div>
        </aside>
      </section>
    </>
  );
}

function Conversation({
  issue,
  simulationStep,
  advance,
  notify,
}: {
  issue: Issue;
  simulationStep: number;
  advance: () => void;
  notify: (message: string) => void;
}) {
  return (
    <div className="conversation">
      <div className="status-card">
        <div className="status-card-top">
          <div><Bot size={17} /><strong>Relay status</strong><span>Updated 4h ago</span></div>
          <span className="micro-badge amber">Waiting on reporter</span>
        </div>
        <div className="status-grid">
          <div><span>Known</span><strong>Superset 6.1 · Chrome · Docker Compose</strong></div>
          <div><span>Still needed</span><strong>{issue.missing.length ? issue.missing.join(' · ') : 'No blocking context'}</strong></div>
          <div><span>Next checkpoint</span><strong>Sep 6 · Gentle reminder</strong></div>
          <div><span>Closure policy</span><strong>2 reminders · reopen anytime with evidence</strong></div>
        </div>
      </div>

      <ConversationMessage
        avatar={issue.avatar}
        name={issue.author}
        time="12 days ago"
        copy={
          <>
            Force-refreshing a dashboard after applying native filters clears every filter for all
            users. It started after our upgrade to 6.1.
          </>
        }
      />
      <ConversationMessage
        avatar="DV"
        name="Devin triage"
        time="4 hours ago"
        agent
        copy={
          <>
            <p>I can narrow this down, but two details block a reliable reproduction:</p>
            <ol>
              <li><strong>Show the exact refresh path.</strong> Please record 20–30 seconds from applying the filter through the reset. Include whether you click the dashboard refresh icon or use the browser refresh.</li>
              <li><strong>Share the enabled dashboard feature flags.</strong> Run <code>superset shell -c &quot;from superset.extensions import feature_flag_manager; print(feature_flag_manager.get_all_flags())&quot;</code> and redact internal names or URLs.</li>
            </ol>
            <p>I’ll use these to reproduce against 6.1 and current master. Please don’t share credentials, production data, or your full configuration.</p>
          </>
        }
      />

      {simulationStep >= 1 && (
        <ConversationMessage
          avatar={issue.avatar}
          name={issue.author}
          time="just now"
          simulated
          copy={
            <>
              Added a recording. It happens with the dashboard refresh icon when
              <code>DASHBOARD_RBAC</code> is enabled. Browser refresh does not reset the filter.
            </>
          }
        />
      )}
      {simulationStep >= 2 && (
        <ConversationMessage
          avatar="DV"
          name="Devin reproducer"
          time="just now"
          agent
          simulated
          copy={
            <>
              Context is sufficient. I queued a clean 6.1 reproduction with the examples dashboard,
              then a control run on master. The next update will include the exact fixture, logs,
              observed result, and repeat count.
            </>
          }
        />
      )}
      {simulationStep >= 3 && (
        <div className="evidence-ready-callout">
          <TestTube2 size={19} />
          <div>
            <strong>Reproduction evidence is ready</strong>
            <span>Failed 3/3 times on 6.1; control passed 3/3 on master.</span>
          </div>
          <button onClick={() => notify('Evidence pack opened')}>Review evidence</button>
        </div>
      )}

      <div className="conversation-composer">
        <div>
          <WandSparkles size={17} />
          <span>Dry-run action</span>
        </div>
        <p>{simulationStep === 0 ? 'Simulate a complete reporter response to see the next transition.' : 'Advance the issue through the next safe transition.'}</p>
        <div className="composer-actions">
          <button className="secondary-button" onClick={() => notify('Reminder preview opened')}><Bell size={15} /> Preview reminder</button>
          <button className="primary-button" onClick={advance} disabled={simulationStep >= 3}>
            <Send size={15} /> {simulationStep === 0 ? 'Simulate reply' : simulationStep === 1 ? 'Queue reproduction' : 'Complete reproduction'}
          </button>
        </div>
      </div>
    </div>
  );
}

function ConversationMessage({
  avatar,
  name,
  time,
  copy,
  agent,
  simulated,
}: {
  avatar: string;
  name: string;
  time: string;
  copy: React.ReactNode;
  agent?: boolean;
  simulated?: boolean;
}) {
  return (
    <div className={`message ${agent ? 'agent' : ''} ${simulated ? 'simulated' : ''}`}>
      <Avatar initials={avatar} />
      <div className="message-body">
        <div className="message-meta">
          <strong>{name}</strong>
          {agent && <span className="agent-badge"><Sparkles size={11} /> Agent</span>}
          {simulated && <span className="simulated-badge">Simulation</span>}
          <span>{time}</span>
        </div>
        <div className="message-copy">{copy}</div>
      </div>
    </div>
  );
}

function EvidencePack({ issue, notify }: { issue: Issue; notify: (message: string) => void }) {
  const evidence = [
    ['Environment', 'Superset 6.1.0 · Docker Compose · Chrome 128', 'complete'],
    ['Minimal fixture', 'examples_native_filters.zip · 18 KB', 'complete'],
    ['Failure command', 'pytest tests/integration_tests/dashboard_tests.py -k force_refresh', 'complete'],
    ['Repeatability', '3 / 3 failure runs · 3 / 3 control runs', 'complete'],
    ['Regression test', 'Draft generated · fails before fix', 'draft'],
    ['Sensitive data scan', 'No credentials, URLs, or production data detected', 'complete'],
  ];
  return (
    <div className="evidence-view">
      <div className="evidence-summary">
        <div className="evidence-score">
          <svg viewBox="0 0 44 44">
            <circle cx="22" cy="22" r="18" />
            <circle cx="22" cy="22" r="18" className="score-ring" strokeDasharray="105 113" />
          </svg>
          <div><strong>93%</strong><span>complete</span></div>
        </div>
        <div>
          <p className="eyebrow">Evidence quality</p>
          <h3>Ready for owner review</h3>
          <p>The report can be recreated without access to the reporter’s environment.</p>
        </div>
        <button className="secondary-button" onClick={() => notify('Reproduction script copied')}>
          <TerminalSquare size={15} /> Copy repro command
        </button>
      </div>
      <div className="evidence-items">
        {evidence.map(([label, value, status]) => (
          <div className="evidence-item" key={label}>
            <span className={`evidence-check ${status}`}><Check size={14} /></span>
            <div><strong>{label}</strong><span>{value}</span></div>
            <ChevronRight size={16} />
          </div>
        ))}
      </div>
      <div className="run-comparison">
        <div className="card-header">
          <div>
            <h2>Run comparison</h2>
            <p>Controlled reproduction for {issue.key}</p>
          </div>
        </div>
        <div className="run-table">
          <div><span>Target</span><strong>6.1.0</strong><b className="failed-run">Failed 3/3</b><small>Filters reset after refresh</small></div>
          <div><span>Control</span><strong>master</strong><b className="passed-run">Passed 3/3</b><small>Filter state persisted</small></div>
        </div>
      </div>
    </div>
  );
}

function OwnerHandoff({ issue, notify }: { issue: Issue; notify: (message: string) => void }) {
  return (
    <div className="handoff-view">
      <div className="handoff-hero">
        <div className="handoff-icon"><FileCheck2 size={24} /></div>
        <div>
          <p className="eyebrow">Decision requested</p>
          <h3>Reproduction was inconclusive — is this still a supported product defect?</h3>
          <p>A confirmed reproduction starts the fix on its own; owners are only asked when reproduction fails, and again before a PR merges.</p>
        </div>
      </div>
      <div className="decision-context">
        <div>
          <span>Recommendation</span>
          <strong><CircleDot size={15} /> Likely defect</strong>
          <p>Dashboard-scoped filter state is lost during an in-app refresh on a supported release.</p>
        </div>
        <div>
          <span>Confidence</span>
          <strong>{issue.confidence}%</strong>
          <p>Based on deterministic reproduction and a passing control.</p>
        </div>
        <div>
          <span>Likely area</span>
          <strong>dashboard / native filters</strong>
          <p>Suggested by stack trace and the regression window.</p>
        </div>
      </div>
      <div className="decision-actions">
        <button className="decision-button confirm" onClick={() => notify('Demo mode: in live mode this calls POST /issues/{id}/decisions (confirm_bug) to override a failed reproduction and start the fix session')}>
          <Check size={18} />
          <span><strong>Treat as defect · start fix anyway</strong><small>Overrides the failed reproduction and opens a PR</small></span>
        </button>
        <button className="decision-button" onClick={() => notify('Returned to reporter context')}>
          <HelpCircle size={18} />
          <span><strong>Need more evidence</strong><small>Ask one targeted follow-up</small></span>
        </button>
        <button className="decision-button" onClick={() => notify('Reclassification draft opened')}>
          <ArrowDownRight size={18} />
          <span><strong>Reclassify</strong><small>Support, expected behavior, or duplicate</small></span>
        </button>
        <button className="decision-button danger" onClick={() => notify('Public processing stopped; private route displayed')}>
          <ShieldCheck size={18} />
          <span><strong>Security-sensitive</strong><small>Stop public investigation</small></span>
        </button>
      </div>
    </div>
  );
}

function Configuration({ notify, dashboard, live }: { notify: (message: string) => void; dashboard: ReturnType<typeof useDashboard>; live: boolean }) {
  return (
    <>
      <PageHeader
        eyebrow="Workspace configuration"
        title="Connections"
        description="Live provider status from the Relay backend, plus operator access for this browser. Provider secrets are configured server-side only."
        actions={
          <button className="secondary-button" onClick={() => void dashboard.refresh()} disabled={!live}>
            <RefreshCw size={15} /> Refresh
          </button>
        }
      />
      <Connections notify={notify} heartbeat={live ? dashboard.data?.heartbeat ?? null : null} live={live} />
    </>
  );
}

function Connections({ notify, heartbeat, live }: { notify: (message: string) => void; heartbeat: Heartbeat | null; live: boolean }) {
  const now = useNow(1000);
  const [operatorToken, setOperatorToken] = useState('');
  const [hasOperatorToken, setHasOperatorToken] = useState(
    () => window.sessionStorage.getItem(OPERATOR_TOKEN_STORAGE_KEY) !== null,
  );
  const saveOperatorToken = () => {
    const value = operatorToken.trim();
    if (!value) {
      notify('Enter the Relay operator token supplied by the deployment administrator.');
      return;
    }
    window.sessionStorage.setItem(OPERATOR_TOKEN_STORAGE_KEY, value);
    setOperatorToken('');
    setHasOperatorToken(true);
    window.location.reload();
  };
  const clearOperatorToken = () => {
    window.sessionStorage.removeItem(OPERATOR_TOKEN_STORAGE_KEY);
    setHasOperatorToken(false);
    notify('Relay operator access was removed from this browser session.');
  };

  return (
    <div className="connections-grid">
      <section className="connection-access card">
        <div>
          <LockKeyhole size={22} />
          <div>
            <h2>Dashboard operator access</h2>
            <p>
              Enter a Relay API token. It stays in browser session storage and is never
              embedded in the application bundle or used as a GitHub or Devin credential.
            </p>
          </div>
        </div>
        <label htmlFor="operator-token">Relay operator token</label>
        <form
          className="connection-access-control"
          onSubmit={event => {
            event.preventDefault();
            saveOperatorToken();
          }}
        >
          <input
            id="operator-token"
            type="password"
            autoComplete="off"
            value={operatorToken}
            onChange={event => setOperatorToken(event.target.value)}
            placeholder={hasOperatorToken ? 'Access configured for this tab' : 'Paste operator token'}
          />
          <button className="primary-button" type="submit">Connect</button>
          {hasOperatorToken && (
            <button className="secondary-button" type="button" onClick={clearOperatorToken}>Remove</button>
          )}
        </form>
        <span role="status" aria-live="polite">
          {hasOperatorToken ? 'Operator access is configured for this browser session.' : 'Operator access is not configured.'}
        </span>
      </section>
      {connectionCards(heartbeat, live, now).map(item => (
        <div className="connection-card card" key={item.name}>
          <div className="connection-icon">{item.icon}</div>
          <span className={`micro-badge ${item.tone}`}>{item.badge}</span>
          <h2>{item.name}</h2>
          <p>{item.copy}</p>
          <small className="connection-detail">{item.detail}</small>
        </div>
      ))}
      <div className="connection-note card">
        <LockKeyhole size={19} />
        <div><strong>Provider secrets never enter the browser</strong><span>Configure GitHub, Devin, Devin Review, and webhook credentials as runtime environment variables or Docker secrets.</span></div>
      </div>
    </div>
  );
}

function connectionCards(heartbeat: Heartbeat | null, live: boolean, now: number) {
  const providerBadge = (status: ProviderStatus | undefined) =>
    status === 'connected' ? ['Connected', 'green'] : status === 'dry_run' ? ['Dry run', 'amber'] : status === 'stale' ? ['Stale', 'amber'] : live ? ['Not reported', ''] : ['Offline', ''];
  const [githubBadge, githubTone] = providerBadge(heartbeat?.github);
  const [devinBadge, devinTone] = providerBadge(heartbeat?.devin);
  const backendBadge = !live ? ['Offline', ''] : !heartbeat ? ['Checking', ''] : heartbeat.database !== 'ok' ? ['Down', ''] : heartbeat.worker === 'ok' ? ['Connected', 'green'] : ['Degraded', 'amber'];
  return [
    {
      name: 'Relay backend',
      icon: <Box size={22} />,
      badge: backendBadge[0],
      tone: backendBadge[1],
      copy: 'FastAPI + PostgreSQL + background worker. Lifecycle records persist in the deployment volume.',
      detail: heartbeat ? `Worker heartbeat ${formatAgo(heartbeat.last_worker_heartbeat_at, now)} · ${heartbeat.reasons[0] ?? 'all probes ok'}` : 'No live heartbeat.',
    },
    {
      name: 'GitHub',
      icon: <GitPullRequest size={22} />,
      badge: githubBadge,
      tone: githubTone,
      copy: 'Issues on exloong/superset are enrolled by Devin\'s own GitHub connection (github:issues automation); Relay\'s API token stays server-side for comments and evidence.',
      detail: heartbeat ? `Devin automation polled ${formatAgo(heartbeat.last_automation_poll_at, now)}${heartbeat.last_webhook_received_at ? ` · optional webhook ${formatAgo(heartbeat.last_webhook_received_at, now)}` : ''}` : 'No automation poll has been observed.',
    },
    {
      name: 'Devin Automations',
      icon: <Zap size={22} />,
      badge: devinBadge,
      tone: devinTone,
      copy: 'Two Relay-managed Devin Automations, both on native GitHub triggers: triage + reproduction on github:issues (opened) and reporter replies; fix on the “reproduced” comment Devin itself posts. They run without Relay.',
      detail: heartbeat
        ? `Last session launched ${formatAgo(heartbeat.last_session_launched_at, now)}${heartbeat.automations.length ? ` · ${heartbeat.automations.map((a) => `${a.kind} ${a.automation_id}${a.enabled ? '' : ' (disabled)'}`).join(', ')}` : ' · automations not provisioned'}`
        : 'No session has been launched.',
    },
    {
      name: 'Owner routing',
      icon: <Users size={22} />,
      badge: 'Server-managed',
      tone: '',
      copy: 'Trusted CODEOWNERS data is read from the exact pull request head.',
      detail: 'Read-only; no credential required in the browser.',
    },
  ];
}

export default App;
