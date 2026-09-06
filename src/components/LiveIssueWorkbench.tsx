import {
  AlertTriangle,
  ArrowDownRight,
  Bot,
  Check,
  ChevronRight,
  ExternalLink,
  FileCheck2,
  GitPullRequest,
  HelpCircle,
  Loader2,
  MessageCircleMore,
  RotateCcw,
  Search,
  Send,
  ShieldCheck,
  Users,
} from 'lucide-react';
import { useMemo, useState } from 'react';
import type { InformationRequest, IssueDetail, IssueSummary, LifecycleEvent, LifecycleState, OwnerDecisionCommand } from '../api';
import {
  formatDateTime,
  formatRelative,
  useIssue,
  useIssues,
  parseTime,
  useNow,
  useOwnerDecision,
  useReporterResponse,
  useRetryIssue,
  type CommandState,
  type Resource,
} from '../hooks';
import type { Page } from '../api';
import { DataSourceBadge, ResourceNotice } from './DataSourceBadge';
import { OwnerRoutingPanel } from './OwnerRoutingPanel';
import { RepositorySafetyNotice, isRepositorySafetyError } from './RepositorySafetyNotice';
import { SafeLink } from './SafeLink';

export const lifecycleLabel: Record<LifecycleState, string> = {
  new: 'New',
  triage: 'Triage',
  awaiting_reporter: 'Needs information',
  reproducing: 'Reproducing',
  blocked_environment: 'Blocked · environment',
  needs_owner_decision: 'Owner decision',
  fix_authorized: 'Fix authorized',
  fixing: 'Fix in progress',
  pr_open: 'PR open',
  awaiting_owner: 'PR in review',
  changes_requested: 'Changes requested',
  completed: 'Completed',
  duplicate: 'Duplicate',
  not_a_bug: 'Not a bug',
  unsupported: 'Unsupported',
  closed_inactive: 'Closed · inactive',
  security_private: 'Security · private',
  automation_error: 'Automation error',
};

export const lifecycleTone: Record<LifecycleState, string> = {
  new: 'gray',
  triage: 'violet',
  awaiting_reporter: 'amber',
  reproducing: 'violet',
  blocked_environment: 'rose',
  needs_owner_decision: 'green',
  fix_authorized: 'blue',
  fixing: 'blue',
  pr_open: 'blue',
  awaiting_owner: 'blue',
  changes_requested: 'amber',
  completed: 'green',
  duplicate: 'gray',
  not_a_bug: 'gray',
  unsupported: 'gray',
  closed_inactive: 'rose',
  security_private: 'rose',
  automation_error: 'rose',
};

const waitingOnHumanStates: LifecycleState[] = ['awaiting_reporter', 'needs_owner_decision', 'awaiting_owner', 'changes_requested', 'security_private'];

const trackSteps: { label: string; states: LifecycleState[] }[] = [
  { label: 'Intake', states: ['new'] },
  { label: 'Classify', states: ['triage'] },
  { label: 'Context', states: ['awaiting_reporter'] },
  { label: 'Reproduce', states: ['reproducing', 'blocked_environment'] },
  { label: 'Confirm', states: ['needs_owner_decision'] },
  { label: 'Fix', states: ['fix_authorized', 'fixing'] },
  { label: 'Review', states: ['pr_open', 'awaiting_owner', 'changes_requested'] },
];

const terminalStates: LifecycleState[] = ['completed', 'duplicate', 'not_a_bug', 'unsupported', 'closed_inactive', 'security_private'];

export function LifecycleBadge({ state }: { state: LifecycleState }) {
  return (
    <span className={`state-badge ${lifecycleTone[state]}`}>
      <i />
      {lifecycleLabel[state]}
    </span>
  );
}

function initialsOf(name: string): string {
  return (
    name
      .split(/\s+/)
      .slice(0, 2)
      .map(part => part[0]?.toUpperCase() ?? '')
      .join('') || '?'
  );
}

type WorkbenchTab = 'conversation' | 'evidence' | 'handoff';
type ListFilter = 'all' | 'waiting' | 'decision' | 'review' | 'attention';

const listFilterStates: Record<ListFilter, LifecycleState[] | undefined> = {
  all: undefined,
  waiting: ['awaiting_reporter'],
  decision: ['needs_owner_decision'],
  review: ['pr_open', 'awaiting_owner', 'changes_requested'],
  attention: ['blocked_environment', 'automation_error'],
};

export function LiveIssueWorkbench({
  issues,
  selectedId,
  onSelect,
  filter,
  onFilter,
  search,
  onSearch,
  notify,
}: {
  issues: Resource<Page<IssueSummary>>;
  selectedId: string | null;
  onSelect: (id: string) => void;
  filter: ListFilter;
  onFilter: (filter: ListFilter) => void;
  search: string;
  onSearch: (value: string) => void;
  notify: (message: string) => void;
}) {
  const items = issues.data?.items ?? [];
  const effectiveId = selectedId && items.some(issue => issue.id === selectedId) ? selectedId : items[0]?.id ?? null;
  const detail = useIssue(effectiveId);
  const now = useNow(30_000);
  const [tab, setTab] = useState<WorkbenchTab>('conversation');

  return (
    <section className="workbench card">
      <aside className="issue-list">
        <div className="issue-list-head">
          <strong>Active queue</strong>
          <span>
            {issues.data ? `${issues.data.total} issues` : ''} <DataSourceBadge state={issues.state} compact />
          </span>
        </div>
        <div className="mini-search">
          <Search size={15} />
          <input aria-label="Filter issue queue" placeholder="Filter queue…" value={search} onChange={event => onSearch(event.target.value)} />
        </div>
        <div className="issue-filter-tabs">
          {(
            [
              ['all', 'All'],
              ['waiting', 'Reporter'],
              ['decision', 'Decision'],
              ['review', 'Review'],
              ['attention', 'Attention'],
            ] as [ListFilter, string][]
          ).map(([key, label]) => (
            <button className={filter === key ? 'active' : ''} onClick={() => onFilter(key)} key={key}>
              {label}
            </button>
          ))}
        </div>
        <div className="issue-list-scroll">
          <ResourceNotice state={issues.state} resourceLabel="issues" />
          {items.map(issue => (
            <button key={issue.id} className={effectiveId === issue.id ? 'issue-list-item selected' : 'issue-list-item'} onClick={() => onSelect(issue.id)}>
              <div className="list-item-meta">
                <span>{issue.key}</span>
                <small>{formatRelative(parseTime(issue.updated_at), now)}</small>
              </div>
              <strong>{issue.title}</strong>
              <div className="list-item-footer">
                <LifecycleBadge state={issue.state} />
                <span className="avatar small">{issue.owner_routing.candidates.find(candidate => candidate.selected)?.initials ?? '—'}</span>
              </div>
            </button>
          ))}
        </div>
      </aside>

      {effectiveId === null ? (
        <div className="issue-detail issue-detail-empty">
          <ResourceNotice state={issues.state} resourceLabel="issues" />
          {issues.state.kind === 'empty' && (
            <p className="session-empty">The API is reachable and returned no issues for exloong/superset. Demo records are not shown while the API is live.</p>
          )}
        </div>
      ) : (
        <LiveIssueDetail detail={detail} tab={tab} setTab={setTab} notify={notify} now={now} summary={items.find(issue => issue.id === effectiveId) ?? null} />
      )}
    </section>
  );
}

function LiveIssueDetail({
  detail,
  summary,
  tab,
  setTab,
  notify,
  now,
}: {
  detail: Resource<IssueDetail>;
  summary: IssueSummary | null;
  tab: WorkbenchTab;
  setTab: (tab: WorkbenchTab) => void;
  notify: (message: string) => void;
  now: number;
}) {
  const issue = detail.data;
  const head = issue ?? summary;
  const target = useMemo(() => (head ? { id: head.id, version: head.version } : null), [head]);

  const accepted = (message: string) => () => {
    notify(message);
    void detail.refresh();
  };
  const respond = useReporterResponse(target, accepted('Reporter response accepted'));
  const decide = useOwnerDecision(target, accepted('Owner decision recorded'));
  const retry = useRetryIssue(target, accepted('Retry accepted'));

  if (detail.state.kind === 'error' && isRepositorySafetyError(detail.state.error)) {
    return (
      <div className="issue-detail issue-detail-empty">
        <RepositorySafetyNotice error={detail.state.error} />
      </div>
    );
  }

  if (!head) {
    return (
      <div className="issue-detail issue-detail-empty">
        <ResourceNotice state={detail.state} resourceLabel="issue" />
      </div>
    );
  }

  const currentStep = trackSteps.findIndex(step => step.states.includes(head.state));
  const terminal = terminalStates.includes(head.state);
  const gate = head.human_gate;
  const waiting = gate.kind !== 'none' || waitingOnHumanStates.includes(head.state);
  const openQuestions = issue?.questions.filter(question => question.status === 'open') ?? [];
  const canRetry = head.state === 'automation_error' || head.state === 'blocked_environment';

  return (
    <>
      <div className="issue-detail">
        <div className="issue-detail-header">
          <div>
            <div className="issue-kicker">
              <span>{head.key}</span>
              <span>·</span>
              <span>{head.category ?? 'Uncategorized'}</span>
              <span>·</span>
              <span>Opened {formatRelative(parseTime(head.opened_at), now)}</span>
              <DataSourceBadge state={detail.state} compact />
            </div>
            <h2>{head.title}</h2>
            <div className="issue-byline">
              <span className="avatar small">{initialsOf(head.reporter.display_name)}</span>
              Reported by <strong>{head.reporter.display_name}</strong>
              <span>·</span>
              <LifecycleBadge state={head.state} />
              <span>·</span>
              <span>v{head.version}</span>
            </div>
          </div>
          <div className="detail-actions">
            <SafeLink className="secondary-button" href={head.html_url} policy="github">
              <ExternalLink size={15} /> {head.repository.full_name}#{head.external_number}
            </SafeLink>
          </div>
        </div>

        <ResourceNotice state={detail.state} resourceLabel="issue detail" />

        {waiting && (
          <div className={`session-gate ${gate.workspace_released ? '' : 'inconsistent'}`} role="status">
            {gate.workspace_released ? <ShieldCheck size={16} /> : <AlertTriangle size={16} />}
            <div>
              <strong>{gate.workspace_released ? 'Waiting on a human · workspace released' : 'Waiting on a human but a workspace is still allocated'}</strong>
              <span>
                {gateKindLabel(gate.kind)}
                {gate.waiting_since ? ` since ${formatDateTime(gate.waiting_since)}` : ''}
                {gate.due_at ? ` · next checkpoint ${formatDateTime(gate.due_at)}` : ''}
                {gate.escalation ? ` · ${gate.escalation}` : ''}
              </span>
            </div>
          </div>
        )}

        <div className="lifecycle-track">
          {trackSteps.map((step, index) => {
            const complete = terminal || (currentStep >= 0 && index < currentStep);
            const current = !terminal && index === currentStep;
            return (
              <div className={complete ? 'track-step complete' : current ? 'track-step current' : 'track-step'} key={step.label}>
                <i>{complete ? <Check size={12} /> : index + 1}</i>
                <span>{step.label}</span>
                {index < trackSteps.length - 1 && <b />}
              </div>
            );
          })}
        </div>

        <div className="detail-tabs">
          <button className={tab === 'conversation' ? 'active' : ''} onClick={() => setTab('conversation')}>
            <MessageCircleMore size={16} /> Questions & timeline {issue && <span>{issue.events.length}</span>}
          </button>
          <button className={tab === 'evidence' ? 'active' : ''} onClick={() => setTab('evidence')}>
            <FileCheck2 size={16} /> Evidence pack {issue && <span>{issue.evidence.items.length}</span>}
          </button>
          <button className={tab === 'handoff' ? 'active' : ''} onClick={() => setTab('handoff')}>
            <Users size={16} /> Owner handoff
          </button>
        </div>

        <div className="detail-content">
          {!issue && <p className="session-empty">Loading issue detail…</p>}
          {issue && tab === 'conversation' && (
            <div className="conversation">
              <div className="status-card">
                <div className="status-card-top">
                  <div>
                    <Bot size={17} />
                    <strong>Relay status</strong>
                    <span>Updated {formatRelative(parseTime(issue.updated_at), now)}</span>
                  </div>
                  <span className={`micro-badge ${lifecycleTone[issue.state]}`}>{lifecycleLabel[issue.state]}</span>
                </div>
                <div className="status-grid">
                  <div>
                    <span>Still needed</span>
                    <strong>{issue.missing_fields.length ? issue.missing_fields.join(' · ') : 'No blocking context'}</strong>
                  </div>
                  <div>
                    <span>Next action</span>
                    <strong>{issue.next_action}</strong>
                  </div>
                  <div>
                    <span>Due</span>
                    <strong>{issue.next_action_due ?? '—'}</strong>
                  </div>
                  <div>
                    <span>Sessions</span>
                    <strong>{issue.session_ids.length}</strong>
                  </div>
                </div>
              </div>

              {issue.body_excerpt && (
                <div className="message">
                  <span className="avatar normal">{initialsOf(issue.reporter.display_name)}</span>
                  <div className="message-body">
                    <div className="message-meta">
                      <strong>{issue.reporter.display_name}</strong>
                      <span>{formatDateTime(issue.opened_at)}</span>
                    </div>
                    <div className="message-copy">
                      <p>{issue.body_excerpt}</p>
                    </div>
                  </div>
                </div>
              )}

              {issue.questions.length > 0 && (
                <div className="question-list">
                  <h4>Information requests</h4>
                  {issue.questions.map(question => (
                    <QuestionRow
                      key={question.id}
                      question={question}
                      revision={issue.revision}
                      state={respond.state}
                      onAnswer={value => void respond.run({ question_id: question.id, issue_revision: issue.revision, response: { kind: 'answer', value } })}
                      onUnavailable={() =>
                        void respond.run({ question_id: question.id, issue_revision: issue.revision, response: { kind: 'unavailable', reason: 'Reporter cannot provide this' } })
                      }
                    />
                  ))}
                  <CommandFeedback state={respond.state} label="Reporter response" onDismiss={respond.reset} />
                </div>
              )}
              {openQuestions.length === 0 && issue.questions.length > 0 && (
                <p className="conversation-footnote">
                  {issue.missing_fields.length > 0
                    ? `No unanswered requests remain; unavailable required fields still block reproduction: ${issue.missing_fields.join(', ')}.`
                    : 'All required information requests are answered.'}
                </p>
              )}

              <div className="lifecycle-events">
                <h4>Lifecycle timeline</h4>
                {issue.events.length === 0 && <p className="session-empty">No lifecycle events recorded.</p>}
                {issue.events.map(event => (
                  <LifecycleEventRow key={event.id} event={event} />
                ))}
              </div>

              {canRetry && (
                <div className="conversation-composer">
                  <div>
                    <RotateCcw size={17} />
                    <span>Recovery</span>
                  </div>
                  <p>Automation stopped in a bounded session. Retrying schedules a new session; the lifecycle only advances once the API confirms.</p>
                  <div className="composer-actions">
                    <button className="primary-button" disabled={retry.pending} onClick={() => void retry.run({ reason: 'Operator retry from workbench' })}>
                      {retry.pending ? <Loader2 size={15} className="spin" /> : <RotateCcw size={15} />} Retry
                    </button>
                  </div>
                  <CommandFeedback state={retry.state} label="Retry" onDismiss={retry.reset} />
                </div>
              )}
            </div>
          )}

          {issue && tab === 'evidence' && <LiveEvidence issue={issue} />}

          {issue && tab === 'handoff' && (
            <LiveHandoff
              issue={issue}
              state={decide.state}
              onDecide={command => void decide.run(command)}
              onDismiss={decide.reset}
            />
          )}
        </div>
      </div>

      <aside className="context-panel">
        <div className="context-section">
          <p className="context-title">Current state</p>
          <LifecycleBadge state={head.state} />
          <div className="progress-line">
            <i style={{ width: `${head.progress}%` }} />
          </div>
          <span className="progress-copy">{head.progress}% through lifecycle</span>
        </div>
        <div className="context-section">
          <p className="context-title">Next action</p>
          <div className="next-action-box">
            <div className="next-action-icon">
              <MessageCircleMore size={17} />
            </div>
            <div>
              <strong>{head.next_action}</strong>
              <span>{head.next_action_due ?? 'No due date'}</span>
            </div>
          </div>
        </div>
        <div className="context-section">
          <p className="context-title">Owner routing</p>
          <OwnerRoutingPanel routing={head.owner_routing} compact />
        </div>
        {head.confidence !== undefined && (
          <div className="context-section">
            <p className="context-title">Triage confidence</p>
            <div className="confidence-value">
              <strong>{head.confidence}%</strong>
              <span>{head.confidence >= 80 ? 'High' : head.confidence >= 50 ? 'Medium' : 'Low'}</span>
            </div>
            <div className="confidence-track">
              <i style={{ width: `${head.confidence}%` }} />
            </div>
          </div>
        )}
        {issue && issue.pull_requests.length > 0 && (
          <div className="context-section">
            <p className="context-title">Pull requests</p>
            {issue.pull_requests.map(pr => (
              <SafeLink className="context-pr" key={`${pr.repository}#${pr.number}`} href={pr.html_url} policy="github">
                <GitPullRequest size={14} /> {pr.repository}#{pr.number} <small>{pr.state}</small>
              </SafeLink>
            ))}
          </div>
        )}
        <div className="context-section">
          <p className="context-title">Automation safety</p>
          <div className="safety-list">
            <span>
              <Check size={13} /> Only {head.repository.full_name}
            </span>
            <span>
              <Check size={13} /> {head.repository.dry_run ? 'Dry-run writes' : 'Live writes gated by humans'}
            </span>
            <span>
              <Check size={13} /> Human fix gate
            </span>
          </div>
        </div>
      </aside>
    </>
  );
}

function gateKindLabel(kind: IssueSummary['human_gate']['kind']): string {
  switch (kind) {
    case 'reporter':
      return 'Waiting on reporter';
    case 'owner':
      return 'Waiting on code owner';
    case 'security':
      return 'Waiting on security';
    case 'operator':
      return 'Waiting on operator';
    case 'none':
      return 'No agent is running';
  }
}

function QuestionRow({
  question,
  revision,
  state,
  onAnswer,
  onUnavailable,
}: {
  question: InformationRequest;
  revision: number;
  state: CommandState;
  onAnswer: (value: string) => void;
  onUnavailable: () => void;
}) {
  const [value, setValue] = useState('');
  const open = question.status === 'open';
  const pending = state.kind === 'pending';
  const staleRevision = question.issue_revision !== revision;
  return (
    <div className={`question-row ${question.status}`}>
      <div className="question-head">
        <strong>
          {question.required ? 'Required' : 'Optional'} · {question.field}
        </strong>
        <span className={`micro-badge ${question.status === 'open' ? 'amber' : question.status === 'answered' ? 'green' : 'gray'}`}>{question.status}</span>
      </div>
      <p>{question.prompt}</p>
      <small>{question.rationale}</small>
      {question.safe_example && (
        <small>
          Safe example: <code>{question.safe_example}</code>
        </small>
      )}
      {question.prohibited_data.length > 0 && <small className="prohibited">Never share: {question.prohibited_data.join(', ')}</small>}
      {question.answer && (
        <div className="question-answer">
          <Check size={13} /> {question.answer}
        </div>
      )}
      {question.status === 'unavailable' && (
        <div className="question-answer">
          <ShieldCheck size={13} /> Reporter cannot provide this; Relay is using a safe alternative path.
        </div>
      )}
      {open && (
        <div className="question-actions">
          <textarea aria-label={`Answer for ${question.field}`} rows={2} value={value} disabled={pending} onChange={event => setValue(event.target.value)} placeholder="Record the reporter's answer…" />
          <div>
            <button className="primary-button" disabled={pending || value.trim().length === 0 || staleRevision} onClick={() => onAnswer(value.trim())}>
              {pending ? <Loader2 size={14} className="spin" /> : <Send size={14} />} Submit answer
            </button>
            <button className="secondary-button" disabled={pending || staleRevision} onClick={onUnavailable}>
              I cannot provide this
            </button>
          </div>
          {staleRevision && <small className="prohibited">This question targets revision {question.issue_revision}; the issue is now at revision {revision}. Refresh before answering.</small>}
        </div>
      )}
    </div>
  );
}

function LifecycleEventRow({ event }: { event: LifecycleEvent }) {
  return (
    <div className={`session-event ${event.outcome === 'accepted' ? 'complete' : event.outcome === 'failed' || event.outcome === 'rejected' ? 'blocked' : 'pending'}`}>
      <i>{event.outcome === 'accepted' ? <Check size={12} /> : event.outcome === 'failed' || event.outcome === 'rejected' ? <AlertTriangle size={12} /> : <span />}</i>
      <div>
        <strong>
          {event.summary}
          {event.to_state && (
            <>
              {' '}
              <em>
                {event.from_state ? `${lifecycleLabel[event.from_state]} → ` : ''}
                {lifecycleLabel[event.to_state]}
              </em>
            </>
          )}
        </strong>
        <span>
          {event.actor ? `${event.actor.display_name} · ` : ''}
          {event.kind}
          {event.detail ? ` · ${event.detail}` : ''}
          {event.outcome !== 'accepted' ? ` · ${event.outcome}` : ''}
        </span>
      </div>
      <small title={event.correlation_id}>{formatDateTime(event.occurred_at)}</small>
    </div>
  );
}

function LiveEvidence({ issue }: { issue: IssueDetail }) {
  const evidence = issue.evidence;
  const circumference = 113;
  return (
    <div className="evidence-view">
      <div className="evidence-summary">
        <div className="evidence-score">
          <svg viewBox="0 0 44 44">
            <circle cx="22" cy="22" r="18" />
            <circle cx="22" cy="22" r="18" className="score-ring" strokeDasharray={`${Math.round((evidence.completeness / 100) * circumference)} ${circumference}`} />
          </svg>
          <div>
            <strong>{evidence.completeness}%</strong>
            <span>complete</span>
          </div>
        </div>
        <div>
          <p className="eyebrow">Evidence quality</p>
          <h3>{evidence.reproduction_ready ? 'Ready for owner review' : 'Not yet reproducible'}</h3>
          <p>
            {evidence.reproduction_ready
              ? 'The report can be recreated without access to the reporter’s environment.'
              : evidence.remaining_uncertainty ?? 'Relay is still collecting portable reproduction evidence.'}
          </p>
        </div>
        {evidence.security_classification !== 'none' && (
          <span className="micro-badge rose">
            <ShieldCheck size={12} /> {evidence.security_classification === 'confirmed_private' ? 'Private security route' : 'Suspected security issue'}
          </span>
        )}
      </div>
      <div className="evidence-items">
        {evidence.items.length === 0 && <p className="session-empty">No evidence items yet.</p>}
        {evidence.items.map(item => (
          <div className="evidence-item" key={item.id}>
            <span className={`evidence-check ${item.status}`}>{item.status === 'missing' || item.status === 'invalid' ? <AlertTriangle size={14} /> : <Check size={14} />}</span>
            <div>
              <strong>{item.label}</strong>
              <span>{item.value}</span>
            </div>
            <ChevronRight size={16} />
          </div>
        ))}
      </div>
      {(evidence.target_version || evidence.control_version) && (
        <div className="run-comparison">
          <div className="card-header">
            <div>
              <h3>Run comparison</h3>
              <p>Controlled reproduction for {issue.key}{evidence.repeat_count ? ` · ${evidence.repeat_count} repeats` : ''}</p>
            </div>
          </div>
          <div className="run-table">
            <div>
              <span>Target</span>
              <strong>{evidence.target_version ?? '—'}</strong>
              <b className="failed-run">{evidence.target_result ?? 'No result'}</b>
              <small>{evidence.observed_behavior ?? ''}</small>
            </div>
            <div>
              <span>Control</span>
              <strong>{evidence.control_version ?? '—'}</strong>
              <b className="passed-run">{evidence.control_result ?? 'No result'}</b>
              <small>{evidence.expected_behavior_evidence ?? ''}</small>
            </div>
          </div>
        </div>
      )}
      {(evidence.regression_window || evidence.minimal_condition) && (
        <div className="status-grid">
          {evidence.regression_window && (
            <div>
              <span>Regression window</span>
              <strong>{evidence.regression_window}</strong>
            </div>
          )}
          {evidence.minimal_condition && (
            <div>
              <span>Minimal condition</span>
              <strong>{evidence.minimal_condition}</strong>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function LiveHandoff({
  issue,
  state,
  onDecide,
  onDismiss,
}: {
  issue: IssueDetail;
  state: CommandState;
  onDecide: (command: OwnerDecisionCommand) => void;
  onDismiss: () => void;
}) {
  const [rationale, setRationale] = useState('');
  const [discriminatorField, setDiscriminatorField] = useState('');
  const [discriminatorPrompt, setDiscriminatorPrompt] = useState('');
  const pending = state.kind === 'pending';
  const bugDecisionOpen = issue.state === 'needs_owner_decision';
  const prDecisionOpen = issue.state === 'awaiting_owner';
  const pullRequest = issue.pull_requests.at(-1);
  const latest = issue.decisions.filter(decision => !decision.superseded_by).at(-1);
  const run = (command: Omit<OwnerDecisionCommand, 'issue_revision' | 'rationale'>) =>
    onDecide({ ...command, issue_revision: issue.revision, rationale: rationale.trim() || undefined });

  return (
    <div className="handoff-view">
      <div className="handoff-hero">
        <div className="handoff-icon">
          <FileCheck2 size={24} />
        </div>
        <div>
          <p className="eyebrow">{bugDecisionOpen || prDecisionOpen ? 'Decision requested' : 'Owner routing'}</p>
          <h3>
            {bugDecisionOpen
              ? 'Does this evidence establish a supported product defect?'
              : prDecisionOpen
                ? `Review pull request #${pullRequest?.number ?? '—'} at its exact head`
                : `No decision is open (${lifecycleLabel[issue.state]})`}
          </h3>
          <p>Owner review starts only after a portable reproduction exists. Devin Review output is evidence for the owner, never a substitute for this decision.</p>
        </div>
      </div>

      <OwnerRoutingPanel routing={issue.owner_routing} />

      {latest && (
        <div className="decision-record">
          <strong>Latest decision · {latest.kind.replace(/_/g, ' ')}</strong>
          <span>
            {latest.actor.display_name} · {formatDateTime(latest.decided_at)}
            {latest.rationale ? ` · ${latest.rationale}` : ''}
            {latest.authorization ? ` · authorizes ${latest.authorization.scope} until ${formatDateTime(latest.authorization.expires_at)}` : ''}
          </span>
        </div>
      )}

      {(bugDecisionOpen || prDecisionOpen) && (
        <>
          <textarea
            className="decision-rationale"
            aria-label="Decision rationale"
            rows={2}
            placeholder="Rationale (recorded with the decision)…"
            value={rationale}
            disabled={pending}
            onChange={event => setRationale(event.target.value)}
          />
          <div className="decision-actions">
            {bugDecisionOpen ? (
              <>
                <button className="decision-button confirm" disabled={pending} onClick={() => run({ kind: 'confirm_bug' })}>
                  {pending ? <Loader2 size={18} className="spin" /> : <Check size={18} />}
                  <span>
                    <strong>Confirm bug & authorize fix</strong>
                    <small>Starts a bounded coding session on {issue.repository.full_name}</small>
                  </span>
                </button>
                <div className="decision-follow-up">
                  <input
                    aria-label="Evidence field"
                    placeholder="Evidence field, e.g. feature_flags"
                    value={discriminatorField}
                    disabled={pending}
                    onChange={event => setDiscriminatorField(event.target.value)}
                  />
                  <textarea
                    aria-label="Evidence question"
                    rows={2}
                    placeholder="One targeted question for the reporter…"
                    value={discriminatorPrompt}
                    disabled={pending}
                    onChange={event => setDiscriminatorPrompt(event.target.value)}
                  />
                  <button
                    className="decision-button"
                    disabled={pending || !discriminatorField.trim() || !discriminatorPrompt.trim()}
                    onClick={() =>
                      run({
                        kind: 'request_discriminator',
                        field: discriminatorField.trim(),
                        prompt: discriminatorPrompt.trim(),
                      })
                    }
                  >
                    <HelpCircle size={18} />
                    <span>
                      <strong>Need more evidence</strong>
                      <small>Ask one targeted follow-up</small>
                    </span>
                  </button>
                </div>
                <button className="decision-button" disabled={pending} onClick={() => run({ kind: 'reclassify', reclassify_as: 'not_a_bug' })}>
                  <ArrowDownRight size={18} />
                  <span>
                    <strong>Reclassify as not a bug</strong>
                    <small>Expected behavior; no fix session</small>
                  </span>
                </button>
                <button className="decision-button danger" disabled={pending} onClick={() => run({ kind: 'route_security_private' })}>
                  <ShieldCheck size={18} />
                  <span>
                    <strong>Security-sensitive</strong>
                    <small>Stop public investigation</small>
                  </span>
                </button>
              </>
            ) : (
              <>
                <button
                  className="decision-button confirm"
                  disabled={pending || !pullRequest}
                  onClick={() =>
                    pullRequest &&
                    run({
                      kind: 'approve_pr',
                      pr_number: pullRequest.number,
                      head_sha: pullRequest.head_sha,
                    })
                  }
                >
                  {pending ? <Loader2 size={18} className="spin" /> : <Check size={18} />}
                  <span>
                    <strong>Approve exact PR head</strong>
                    <small>{pullRequest ? `#${pullRequest.number} · ${pullRequest.head_sha.slice(0, 12)}` : 'PR binding unavailable'}</small>
                  </span>
                </button>
                <button
                  className="decision-button"
                  disabled={pending || !pullRequest}
                  onClick={() =>
                    pullRequest &&
                    run({
                      kind: 'request_changes',
                      pr_number: pullRequest.number,
                      head_sha: pullRequest.head_sha,
                    })
                  }
                >
                  <HelpCircle size={18} />
                  <span>
                    <strong>Request changes</strong>
                    <small>Return the exact head to a bounded fix session</small>
                  </span>
                </button>
              </>
            )}
          </div>
        </>
      )}
      <CommandFeedback state={state} label="Decision" onDismiss={onDismiss} />
    </div>
  );
}

function CommandFeedback({ state, label, onDismiss }: { state: CommandState; label: string; onDismiss: () => void }) {
  if (state.kind === 'idle' || state.kind === 'pending') return null;
  if (state.kind === 'accepted') {
    return (
      <div className="command-feedback accepted" role="status">
        <Check size={14} />
        <span>
          {label} accepted · resource version {state.result.resource_version}
          {state.result.processing === 'async' ? ' · processing asynchronously' : ''}
        </span>
        <button onClick={onDismiss} aria-label="Dismiss">
          ×
        </button>
      </div>
    );
  }
  return (
    <div className="command-feedback error" role="alert">
      <AlertTriangle size={14} />
      <span>
        {label} was not accepted: {state.error.message} ({state.error.code}). The lifecycle did not change.
      </span>
      <button onClick={onDismiss} aria-label="Dismiss">
        ×
      </button>
    </div>
  );
}

export function useLiveIssueList(filter: ListFilter, search: string, enabled: boolean) {
  return useIssues({ state: listFilterStates[filter], search: search.trim() || undefined }, enabled);
}

export type { ListFilter as IssueListFilter };
