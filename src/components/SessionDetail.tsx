import {
  Activity,
  AlertTriangle,
  ArrowRight,
  Bot,
  Check,
  ChevronRight,
  Clock3,
  ExternalLink,
  FileCheck2,
  GitCommitHorizontal,
  GitPullRequest,
  Loader2,
  MessageCircleMore,
  Monitor,
  Paperclip,
  Pause,
  RotateCcw,
  Send,
  ShieldCheck,
  Sparkles,
  Square,
  TerminalSquare,
  ShieldAlert,
} from 'lucide-react';
import { useMemo, useState } from 'react';
import type { CommandAccepted } from '../api';
import type { ConversationMessage, DevinReviewState, PullRequestRef } from '../api';
import {
  elapsedSeconds,
  formatBudget,
  formatClock,
  formatDateTime,
  formatDuration,
  formatRelative,
  isActiveStatus,
  parseTime,
  isWaitingOnHuman,
  useCancelSession,
  useNow,
  useRetryIssue,
  useSessionMessage,
  type CommandState,
  type ResourceState,
  type SessionView,
} from '../hooks';
import { DataSourceBadge, ResourceNotice } from './DataSourceBadge';
import { SafeLink } from './SafeLink';
import { safeHref, validateLink, type LinkPolicy } from '../api/links';

export const sessionStateClass: Record<SessionView['status'], string> = {
  Running: 'running',
  Queued: 'queued',
  'Needs attention': 'attention',
  'Waiting on owner': 'waiting',
  Completed: 'completed',
  Failed: 'failed',
  Cancelled: 'cancelled',
};

export function SessionStatusBadge({ status }: { status: SessionView['status'] }) {
  return (
    <span className={`session-status ${sessionStateClass[status]}`}>
      <i />
      {status}
    </span>
  );
}

type DetailTab = 'activity' | 'conversation' | 'outputs' | 'guardrails';

export function SessionDetailView({
  session,
  detailState,
  goToIssue,
  notify,
  onRefresh,
}: {
  session: SessionView;
  detailState?: ResourceState<unknown>;
  goToIssue: (issueId: string) => void;
  notify: (message: string) => void;
  onRefresh?: () => void;
}) {
  const [tab, setTab] = useState<DetailTab>('activity');
  const live = session.source === 'live';
  const now = useNow(1000, live && session.status === 'Running');
  const elapsedLive = live ? elapsedSeconds(session.startedAt, session.endedAt, now) : null;
  const elapsed = elapsedLive === null ? session.elapsed : formatDuration(elapsedLive);
  const overBudget = elapsedLive !== null && session.budgetSeconds !== null && elapsedLive > session.budgetSeconds;

  const accepted = (message: string) => () => {
    notify(message);
    onRefresh?.();
  };
  const sessionTarget = useMemo(
    () => (live ? { id: session.id, version: session.version ?? undefined } : null),
    [live, session.id, session.version],
  );
  const issueTarget = useMemo(() => (live ? { id: session.issueId } : null), [live, session.issueId]);
  const cancel = useCancelSession(sessionTarget, accepted('Cancellation accepted by Relay'));
  const message = useSessionMessage(sessionTarget, accepted('Message accepted; it will appear once Devin echoes it back'));
  const retry = useRetryIssue(issueTarget, accepted('Retry accepted; a new bounded session will be scheduled'));

  const waitingOnHuman = isWaitingOnHuman(session);
  const workspaceInconsistent = waitingOnHuman && live && !session.workspaceReleased;

  const outputsCount =
    (session.outputs?.length ?? 0) + (session.pullRequests?.length ?? 0) + (session.artifacts?.length ?? session.artifactLabels.length);

  return (
    <div className="session-detail">
      <div className="session-detail-head">
        <div className="session-title">
          <span className={`session-agent-icon ${sessionStateClass[session.status]}`}>
            <Bot size={21} />
          </span>
          <div>
            <span>
              {session.shortId} · {session.actor}
              {detailState && <DataSourceBadge state={detailState} compact />}
              {!detailState && session.source === 'demo' && <span className="source-badge demo compact">Demo data</span>}
            </span>
            <h2>{session.title}</h2>
            <button onClick={() => goToIssue(session.issueId)}>
              {session.issueKey} · {session.issueTitle} <ChevronRight size={13} />
            </button>
          </div>
        </div>
        <div className="session-head-actions">
          <SessionStatusBadge status={session.status} />
          {live && isActiveStatus(session.status) && (
            <button
              className="icon-button"
              aria-label="Cancel session"
              disabled={cancel.pending}
              onClick={() => void cancel.run({ reason: 'Operator requested cancellation from the Relay dashboard' })}
            >
              {cancel.pending ? <Loader2 size={16} className="spin" /> : <Square size={16} />}
            </button>
          )}
          {!live && session.status === 'Running' && (
            <button className="icon-button" onClick={() => notify(`${session.id} pause requested (simulated)`)} aria-label="Pause session">
              <Pause size={16} />
            </button>
          )}
          <ExternalSessionLinks session={session} notify={notify} />
        </div>
      </div>

      <CommandFeedback state={cancel.state} label="Cancel" onDismiss={cancel.reset} />

      <div className={`session-current-work ${sessionStateClass[session.status]}`}>
        <span>
          {session.status === 'Running' ? <Activity size={17} /> : session.status === 'Needs attention' || session.status === 'Failed' ? <AlertTriangle size={17} /> : <Clock3 size={17} />}
        </span>
        <div>
          <small>
            {session.status === 'Running'
              ? 'Working on'
              : session.status === 'Needs attention' || session.status === 'Failed'
                ? 'Blocked at'
                : 'Current state'}
          </small>
          <strong>{session.currentAction ?? <span className="muted">Not exposed by the Devin session API</span>}</strong>
        </div>
        <div className="session-elapsed">
          <small>Elapsed / budget</small>
          <strong className={overBudget ? 'over-budget' : undefined}>
            {elapsed} <span>/ {session.budget}</span>
          </strong>
        </div>
      </div>

      {waitingOnHuman && (
        <div className={`session-gate ${workspaceInconsistent ? 'inconsistent' : ''}`} role="status">
          {workspaceInconsistent ? <AlertTriangle size={16} /> : <ShieldCheck size={16} />}
          <div>
            <strong>
              {workspaceInconsistent
                ? 'Waiting on a human but the workspace is still allocated'
                : 'Waiting on a human · compute and workspace released'}
            </strong>
            <span>
              {session.humanGate
                ? `${gateLabel(session.humanGate.kind)}${session.humanGate.waiting_since ? ` since ${formatDateTime(session.humanGate.waiting_since)}` : ''}${session.humanGate.due_at ? ` · due ${formatDateTime(session.humanGate.due_at)}` : ''}${session.humanGate.escalation ? ` · escalation: ${session.humanGate.escalation}` : ''}`
                : 'No agent is running. A new event resumes work in a fresh bounded session.'}
            </span>
          </div>
        </div>
      )}

      <div className="session-facts">
        <span>
          <small>Repository</small>
          <SafeLink href={session.repositoryUrl} policy="github">
            {session.repository} <ExternalLink size={11} />
          </SafeLink>
        </span>
        <span>
          <small>Commit</small>
          {session.commit ? (
            <SafeLink href={`${session.repositoryUrl}/commit/${encodeURIComponent(session.commit)}`} policy="github">
              <GitCommitHorizontal size={12} /> {session.commit.slice(0, 10)}
            </SafeLink>
          ) : (
            <strong className="muted">Not recorded in demo data</strong>
          )}
        </span>
        <span>
          <small>Branch</small>
          {session.branch ? (
            <SafeLink href={`${session.repositoryUrl}/tree/${session.branch.split('/').map(encodeURIComponent).join('/')}`} policy="github">
              {session.branch}
            </SafeLink>
          ) : (
            <strong className="muted">None</strong>
          )}
        </span>
        <span>
          <small>Trigger</small>
          <strong>{session.trigger}</strong>
        </span>
        <span>
          <small>Started</small>
          <strong>{session.started}</strong>
        </span>
        <span>
          <small>Workspace</small>
          <strong>{session.environment}</strong>
        </span>
      </div>

      <div className="session-progress-block">
        <div>
          <span>Session progress</span>
          <strong>{session.progress === null ? <span className="muted">Unavailable</span> : `${session.progress}%`}</strong>
        </div>
        {session.progress === null ? (
          <small className="progress-disclosure">Devin does not report a progress percentage for live sessions; follow the event timeline and status instead.</small>
        ) : (
          <div className="session-progress-track">
            <i style={{ width: `${session.progress}%` }} />
          </div>
        )}
        <small>Next checkpoint: {session.nextCheckpoint ?? 'Not exposed by the Devin session API'}</small>
      </div>

      <div className="session-tabs" role="tablist" aria-label="Session details">
        {(
          [
            ['activity', 'Activity'],
            ['conversation', session.conversation ? `Conversation · ${session.conversation.length}` : 'Conversation'],
            ['outputs', `Outputs · ${outputsCount}`],
            ['guardrails', 'Guardrails'],
          ] as [DetailTab, string][]
        ).map(([key, label]) => (
          <button className={tab === key ? 'active' : ''} onClick={() => setTab(key)} role="tab" aria-selected={tab === key} key={key}>
            {label}
          </button>
        ))}
      </div>

      <div className="session-detail-body">
        <div className="session-detail-primary">
          {detailState && <ResourceNotice state={detailState} resourceLabel="session detail" />}

          {tab === 'activity' && (
            <div className="session-timeline">
              <div className="session-section-title">
                <div>
                  <p className="eyebrow">Execution trace</p>
                  <h3>What this session has done</h3>
                </div>
                <span>Updated {session.updated}</span>
              </div>
              {session.events.length === 0 && <p className="session-empty">No events recorded yet.</p>}
              {session.events.map((event, index) => (
                <div className={`session-event ${event.state}`} key={`${session.id}-${index}-${event.label}`}>
                  <i>{event.state === 'complete' ? <Check size={12} /> : event.state === 'blocked' ? <AlertTriangle size={12} /> : <span />}</i>
                  <div>
                    <strong>{event.label}</strong>
                    <span>{event.detail}</span>
                  </div>
                  <small>{event.time}</small>
                </div>
              ))}
            </div>
          )}

          {tab === 'conversation' && (
            <SessionConversation session={session} messageState={message.state} onSend={body => message.run({ body })} onDismiss={message.reset} notify={notify} />
          )}

          {tab === 'outputs' && <SessionOutputs session={session} notify={notify} />}

          {tab === 'guardrails' && (
            <div className="session-guardrails">
              <div className="session-section-title">
                <div>
                  <p className="eyebrow">Execution contract</p>
                  <h3>Limits applied to this run</h3>
                </div>
              </div>
              {[
                ['Bounded runtime', `${session.budget} maximum; no unattended continuation`],
                ['Repository scope', `Only ${session.repository}${session.commit ? ` at ${session.commit.slice(0, 10)}` : ''}; no other repository may be read or written`],
                ['Isolated workspace', 'No reporter production data or credentials are available'],
                ['Narrow scope', `Only the ${session.flowStep.toLowerCase()} transition is authorized`],
                ['Human gates', 'No merge, issue closure, security publication, or expected-behavior decision'],
              ].map(([label, copy]) => (
                <div className="guardrail-row" key={label}>
                  <ShieldCheck size={16} />
                  <div>
                    <strong>{label}</strong>
                    <span>{copy}</span>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>

        <aside className="session-context">
          <div>
            <p className="eyebrow">Flow linkage</p>
            <span>
              <small>Flow step</small>
              <strong>{session.flowStep}</strong>
            </span>
            <span>
              <small>Budget</small>
              <strong>{session.budgetSeconds !== null ? formatBudget(session.budgetSeconds) : session.budget}</strong>
            </span>
            {session.correlationId && (
              <span>
                <small>Correlation</small>
                <strong className="mono">{session.correlationId}</strong>
              </span>
            )}
            {session.review && session.review.status !== 'not_requested' && (
              <span>
                <small>Devin Review</small>
                <strong>{reviewStatusLabel(session.review)}</strong>
              </span>
            )}
          </div>
          <div>
            <p className="eyebrow">Operator controls</p>
            {session.status === 'Needs attention' || session.status === 'Failed' ? (
              live ? (
                <button
                  className="primary-button"
                  disabled={retry.pending}
                  onClick={() => void retry.run({ reason: 'Operator requested recovery from the session monitor' })}
                >
                  {retry.pending ? <Loader2 size={15} className="spin" /> : <RotateCcw size={15} />} Retry with a new session
                </button>
              ) : (
                <button className="primary-button" onClick={() => notify('Recovery options opened (simulated)')}>
                  Review recovery options
                </button>
              )
            ) : waitingOnHuman ? (
              <button className="primary-button" onClick={() => goToIssue(session.issueId)}>
                Open issue · {gateLabel(session.humanGate?.kind ?? 'owner').toLowerCase()}
              </button>
            ) : (
              <button className="secondary-button" onClick={() => notify(live ? 'Run log export is not exposed by the API yet' : 'Session logs exported (simulated)')}>
                <TerminalSquare size={14} /> Export run log
              </button>
            )}
            <CommandFeedback state={retry.state} label="Retry" onDismiss={retry.reset} />
            <button className="text-button" onClick={() => goToIssue(session.issueId)}>
              Open issue <ArrowRight size={13} />
            </button>
          </div>
        </aside>
      </div>
    </div>
  );
}

function gateLabel(kind: NonNullable<SessionView['humanGate']>['kind']): string {
  switch (kind) {
    case 'reporter':
      return 'Waiting on reporter';
    case 'owner':
      return 'Waiting on owner';
    case 'security':
      return 'Waiting on security review';
    case 'operator':
      return 'Waiting on operator';
    case 'none':
      return 'No human gate';
  }
}

function ExternalSessionLinks({ session, notify }: { session: SessionView; notify: (message: string) => void }) {
  if (session.source === 'demo') {
    return (
      <button className="secondary-button" onClick={() => notify('Demo sessions have no Devin session link')} title="Demo data has no canonical Devin session">
        Open in Devin <ExternalLink size={14} />
      </button>
    );
  }
  const links = session.links;
  return (
    <>
      <SafeLink
        className="secondary-button"
        href={links?.devin_session_url}
        policy="devin"
        fallback={
          <span className="secondary-button disabled" title={linkProblem(links?.devin_session_url, 'devin') ?? 'The API did not supply a canonical Devin session link'}>
            {links?.devin_session_url ? <ShieldAlert size={14} /> : null} {links?.devin_session_url ? 'Devin link withheld' : 'No Devin link'}
          </span>
        }
      >
        Open in Devin <ExternalLink size={14} />
      </SafeLink>
      {links?.devin_desktop_url && (
        <SafeLink
          className="secondary-button"
          href={links.devin_desktop_url}
          policy="devin"
          title="Authenticated Devin Desktop / remote computer"
          fallback={
            <span className="secondary-button disabled" title={linkProblem(links.devin_desktop_url, 'devin') ?? undefined}>
              <ShieldAlert size={14} /> Desktop link withheld
            </span>
          }
        >
          <Monitor size={14} /> Remote computer
        </SafeLink>
      )}
    </>
  );
}

function linkProblem(url: string | null | undefined, policy: LinkPolicy): string | null {
  const check = validateLink(url, policy);
  return check.ok ? null : check.reason;
}

function LinkWithheld({ url, policy }: { url: string; policy: LinkPolicy }) {
  return (
    <small className="unsafe-link" title={url}>
      <ShieldAlert size={11} /> Link withheld · {linkProblem(url, policy)}
    </small>
  );
}

function CommandFeedback({ state, label, onDismiss }: { state: CommandState; label: string; onDismiss: () => void }) {
  if (state.kind === 'idle' || state.kind === 'pending') return null;
  if (state.kind === 'accepted') {
    return (
      <div className="command-feedback accepted" role="status">
        <Check size={14} />
        <span>
          {label} accepted (event {state.result.event_id.slice(0, 8)}, version {state.result.resource_version}
          {state.result.processing === 'async' ? ', processing asynchronously' : ''}).
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
        {label} was not accepted: {state.error.message} ({state.error.code}). No lifecycle change was recorded.
      </span>
      <button onClick={onDismiss} aria-label="Dismiss">
        ×
      </button>
    </div>
  );
}

function SessionConversation({
  session,
  messageState,
  onSend,
  onDismiss,
  notify,
}: {
  session: SessionView;
  messageState: CommandState;
  onSend: (body: string) => Promise<CommandAccepted | null>;
  onDismiss: () => void;
  notify: (message: string) => void;
}) {
  const [draft, setDraft] = useState('');
  const [sending, setSending] = useState(false);
  const live = session.source === 'live';
  const conversation = session.conversation;
  const embeddable = session.links?.conversation_embeddable ?? false;

  return (
    <div className="session-conversation">
      <div className="session-section-title">
        <div>
          <p className="eyebrow">Synchronized conversation</p>
          <h3>Messages exchanged with Devin</h3>
        </div>
        {conversation && <span>{conversation.length} messages</span>}
      </div>

      {(!live || session.dryRun) && (
        <div className="conversation-disclosure" role="status">
          <MessageCircleMore size={16} />
          <div>
            <strong>{session.dryRun ? 'Dry-run conversation' : 'Not available in demo data'}</strong>
            <span>
              {session.dryRun
                ? 'This local adapter does not call Devin or claim synchronized messages.'
                : 'Demo sessions carry no conversation. Relay never renders a fabricated transcript; connect the API to synchronize real messages.'}
            </span>
          </div>
        </div>
      )}

      {live && !session.dryRun && conversation === null && (
        <div className="conversation-disclosure" role="status">
          <ExternalLink size={16} />
          <div>
            <strong>Conversation is only available in Devin</strong>
            <span>
              The approved Devin API does not expose this session&apos;s messages to Relay.{' '}
              {session.links?.devin_session_url ? (
                <SafeLink href={session.links.devin_session_url} policy="devin">
                  Open the authenticated Devin session
                </SafeLink>
              ) : (
                'No authenticated session link was supplied.'
              )}
            </span>
          </div>
        </div>
      )}

      {live && !session.dryRun && conversation !== null && conversation.length === 0 && <p className="session-empty">No messages have been exchanged yet.</p>}

      {live && !session.dryRun && conversation && conversation.length > 0 && (
        <div className="conversation-messages">
          {conversation.map(item => (
            <ConversationBubble key={item.id} message={item} />
          ))}
        </div>
      )}

      {live && !session.dryRun && !embeddable && conversation !== null && (
        <p className="conversation-footnote">
          Messages are synchronized from the Devin API on each refresh. Live streaming and remote-desktop embedding are not exposed; use the authenticated Devin link for those.
        </p>
      )}

      <div className="session-composer">
        <textarea
          aria-label="Message to Devin"
          placeholder={live ? 'Send an instruction to this session…' : 'Messaging requires a live session'}
          value={draft}
          disabled={!live || messageState.kind === 'pending'}
          onChange={event => setDraft(event.target.value)}
          rows={2}
        />
        <button
          className="primary-button"
          disabled={!live || sending || draft.trim().length === 0 || messageState.kind === 'pending'}
          onClick={async () => {
            if (!live) {
              notify('Messaging requires a live session');
              return;
            }
            const body = draft.trim();
            setSending(true);
            try {
              const accepted = await onSend(body);
              if (accepted) setDraft(current => (current.trim() === body ? '' : current));
            } finally {
              setSending(false);
            }
          }}
        >
          {messageState.kind === 'pending' ? <Loader2 size={15} className="spin" /> : <Send size={15} />} Send
        </button>
      </div>
      <CommandFeedback state={messageState} label="Message" onDismiss={onDismiss} />
    </div>
  );
}

function ConversationBubble({ message }: { message: ConversationMessage }) {
  const agent = message.author.kind === 'agent';
  const initials = message.author.display_name
    .split(/\s+/)
    .slice(0, 2)
    .map(part => part[0]?.toUpperCase() ?? '')
    .join('');
  return (
    <div className={`message ${agent ? 'agent' : ''}`}>
      <span className="avatar normal">{initials || '?'}</span>
      <div className="message-body">
        <div className="message-meta">
          <strong>{message.author.display_name}</strong>
          {agent && (
            <span className="agent-badge">
              <Sparkles size={11} /> Devin
            </span>
          )}
          <span title={message.sent_at}>{formatClock(message.sent_at)}</span>
        </div>
        <div className="message-copy">
          <p>{message.body}</p>
          {message.attachments.length > 0 && (
            <ul className="message-attachments">
              {message.attachments.map(attachment => (
                <li key={attachment.id}>
                  <Paperclip size={12} />
                  {attachment.url ? (
                    <SafeLink href={attachment.url} policy="artifact" fallback={<span>{attachment.name}</span>}>
                      {attachment.name}
                    </SafeLink>
                  ) : (
                    attachment.name
                  )}
                  <small>
                    {attachment.content_type} · {Math.max(1, Math.round(attachment.size_bytes / 1024))} KB
                  </small>
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>
    </div>
  );
}

function reviewStatusLabel(review: DevinReviewState): string {
  switch (review.status) {
    case 'not_requested':
      return 'Not requested';
    case 'queued':
      return 'Queued';
    case 'running':
      return 'Running';
    case 'failed':
      return 'Failed';
    case 'completed':
      return review.findings.length === 0 ? 'Completed · no findings' : `Completed · ${review.findings.length} finding${review.findings.length === 1 ? '' : 's'}`;
  }
}

function SessionOutputs({ session, notify }: { session: SessionView; notify: (message: string) => void }) {
  const now = useNow(30_000, session.source === 'live');
  const live = session.source === 'live';
  return (
    <div className="session-outputs">
      <div className="session-section-title">
        <div>
          <p className="eyebrow">Portable evidence</p>
          <h3>Outputs attached to the issue lifecycle</h3>
        </div>
      </div>

      {live && session.outputs && (
        <section>
          <h4>Structured outputs</h4>
          {session.outputs.length === 0 && <p className="session-empty">No validated outputs yet.</p>}
          {session.outputs.map(output => (
            <div className={`output-row ${output.accepted ? 'accepted' : 'rejected'}`} key={output.id}>
              <span>{output.accepted ? <Check size={15} /> : <AlertTriangle size={15} />}</span>
              <div>
                <strong>
                  {output.label} <code>{output.schema}</code>
                </strong>
                <small>
                  {output.accepted ? 'Accepted' : 'Rejected · cannot advance the lifecycle'} · {formatRelative(parseTime(output.produced_at), now)}
                </small>
                {output.summary && <span>{output.summary}</span>}
              </div>
            </div>
          ))}
        </section>
      )}

      <section>
        <h4>Artifacts</h4>
        <div className="session-artifacts">
          {session.artifacts
            ? session.artifacts.map(artifact => (
                <ArtifactRow
                  key={artifact.id}
                  label={artifact.label}
                  meta={`${artifact.kind} · ${artifact.content_type} · ${Math.max(1, Math.round(artifact.size_bytes / 1024))} KB${artifact.retained ? '' : ' · not retained'}`}
                  href={safeHref(artifact.url, 'artifact') ?? undefined}
                  onClick={() => notify(artifact.url ? 'Artifact link withheld: it did not pass the HTTPS link policy' : 'Artifact content is not exposed by the API')}
                />
              ))
            : session.artifactLabels.map((artifact, index) => (
                <ArtifactRow
                  key={artifact}
                  label={artifact}
                  meta={`${index === 0 ? 'Primary output' : 'Supporting evidence'} · demo record`}
                  onClick={() => notify(`${artifact} preview opened (simulated)`)}
                />
              ))}
          {session.artifacts && session.artifacts.length === 0 && <p className="session-empty">No artifacts retained.</p>}
        </div>
      </section>

      <section>
        <h4>Pull requests</h4>
        {!live && <p className="session-empty">Pull-request state is only shown for live sessions.</p>}
        {live && session.pullRequests && session.pullRequests.length === 0 && (
          <p className="session-empty">No pull request exists for this session. Fix sessions open one only after an owner confirms the bug.</p>
        )}
        {live && session.pullRequests?.map(pr => <PullRequestRow key={`${pr.repository}#${pr.number}`} pr={pr} />)}
      </section>

      {live && session.review && <ReviewPanel review={session.review} />}
    </div>
  );
}

function ArtifactRow({ label, meta, href, onClick }: { label: string; meta: string; href?: string; onClick: () => void }) {
  const inner = (
    <>
      <span>
        <FileCheck2 size={17} />
      </span>
      <div>
        <strong>{label}</strong>
        <small>{meta}</small>
      </div>
      <ChevronRight size={15} />
    </>
  );
  if (href) {
    return (
      <a href={href} target="_blank" rel="noreferrer noopener" className="artifact-link">
        {inner}
      </a>
    );
  }
  return <button onClick={onClick}>{inner}</button>;
}

function PullRequestRow({ pr }: { pr: PullRequestRef }) {
  return (
    <div className={`pr-row ${pr.state}`}>
      <span>
        <GitPullRequest size={16} />
      </span>
      <div>
        <SafeLink href={pr.html_url} policy="github" fallback={<strong>{pr.repository}#{pr.number} · {pr.title}</strong>}>
          {pr.repository}#{pr.number} · {pr.title} <ExternalLink size={11} />
        </SafeLink>
        {!safeHref(pr.html_url, 'github') && <LinkWithheld url={pr.html_url} policy="github" />}
        <small>
          {pr.head_branch} → {pr.base_branch} · {pr.head_sha.slice(0, 10)} · {pr.draft ? 'draft' : pr.state}
        </small>
      </div>
      <div className="pr-signals">
        <span className={`pr-check ${pr.checks}`}>checks {pr.checks}</span>
        <span className={`pr-review ${pr.review}`}>{pr.review === 'none' ? 'no human review yet' : `human review ${pr.review.replace('_', ' ')}`}</span>
      </div>
    </div>
  );
}

function ReviewPanel({ review }: { review: DevinReviewState }) {
  return (
    <section className="review-panel">
      <h4>
        Devin Review <em>evidence, not approval</em>
      </h4>
      <div className="review-summary">
        <strong>{reviewStatusLabel(review)}</strong>
        {review.head_sha && <small>head {review.head_sha.slice(0, 10)}</small>}
        {review.completed_at && <small>completed {formatDateTime(review.completed_at)}</small>}
        {review.url && (
          <SafeLink href={review.url} policy="devin">
            Open review <ExternalLink size={11} />
          </SafeLink>
        )}
      </div>
      {review.findings.length > 0 && (
        <ul className="review-findings">
          {review.findings.map(finding => (
            <li key={finding.id} className={finding.severity}>
              <b>{finding.severity}</b>
              <div>
                <strong>{finding.title}</strong>
                {(finding.path || finding.detail) && (
                  <span>
                    {finding.path ? `${finding.path}${finding.line ? `:${finding.line}` : ''}` : ''}
                    {finding.path && finding.detail ? ' · ' : ''}
                    {finding.detail ?? ''}
                  </span>
                )}
              </div>
            </li>
          ))}
        </ul>
      )}
      <p className="review-footnote">Merge still requires an exloong/superset code owner. Devin Review findings never satisfy the human gate.</p>
    </section>
  );
}
