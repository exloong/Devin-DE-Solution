import { useEffect, useMemo, useState } from 'react';
import { AlertTriangle, Bot, ExternalLink, Loader2, Lock, Pencil, Plus, RefreshCw, TestTube2, Trash2, Webhook, Workflow, X } from 'lucide-react';
import { ApiError, type Automation, type AutomationCreate, type AutomationUpdate, type ProviderSession, type SessionSummary } from '../api';
import { useAutomation, useAutomationMutations, useAutomations } from '../hooks';
import { DataSourceBadge, ResourceNotice } from './DataSourceBadge';
import { SafeLink } from './SafeLink';
import { formatAgo } from './HealthDashboard';

type Editor = { mode: 'create' } | { mode: 'edit'; automation: Automation } | null;

/**
 * Manages the organization's Devin Automations through Relay's proxy. Devin
 * owns the records; Relay only adds who may edit them.
 */
/** Flatten Devin trigger conditions ({any:[{all:[{field,operator,value}]}]}) into readable lines. */
function describeConditions(conditions: Record<string, unknown> | null): string[] {
  if (!conditions) return [];
  const lines: string[] = [];
  const walk = (node: unknown): void => {
    if (Array.isArray(node)) {
      node.forEach(walk);
      return;
    }
    if (!node || typeof node !== 'object') return;
    const record = node as Record<string, unknown>;
    if (typeof record.field === 'string') {
      lines.push(`${record.field} ${String(record.operator ?? 'eq')} ${JSON.stringify(record.value)}`);
      return;
    }
    Object.values(record).forEach(walk);
  };
  walk(conditions);
  return lines;
}

export function AutomationsView({ live, goToIssue, notify }: { live: boolean; goToIssue: (id: string) => void; notify: (message: string) => void }) {
  const list = useAutomations(live);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [editor, setEditor] = useState<Editor>(null);
  const items = list.data?.items ?? [];

  // A freshly created id may not be in the (still polling) list yet, so fall back for display only.
  const selected = items.find(a => a.automation_id === selectedId) ?? items[0] ?? null;
  const detailId = selectedId ?? selected?.automation_id ?? null;
  useEffect(() => {
    if (selectedId === null && items.length > 0) setSelectedId(items[0].automation_id);
  }, [items, selectedId]);

  const detail = useAutomation(detailId, live);
  const mutations = useAutomationMutations(() => {
    void list.refresh();
    void detail.refresh();
  });

  if (!live) {
    return (
      <section className="automations-view">
        <div className="page-header">
          <div>
            <p className="eyebrow">Devin</p>
            <h1>Devin Automations</h1>
            <p>Automations are read from and written to Devin's Automation API; the Relay API must be reachable.</p>
          </div>
        </div>
        <div className="resource-notice error" role="alert">
          <AlertTriangle size={16} />
          <span>The Relay API is not available, so automations cannot be shown or changed.</span>
        </div>
      </section>
    );
  }

  return (
    <section className="automations-view">
      <div className="page-header">
        <div>
          <p className="eyebrow">Devin</p>
          <h1>Devin Automations</h1>
          <p>Every automation in the Devin organization, with its workflow and the sessions it launched. Relay dispatches gated reproduction/fix work to the two it manages.</p>
        </div>
        <div className="page-header-actions">
          <DataSourceBadge state={list.state} compact />
          <button className="secondary-button" onClick={() => void list.refresh()} disabled={list.state.kind === 'loading'}>
            <RefreshCw size={14} /> Refresh
          </button>
          <button className="primary-button" onClick={() => setEditor({ mode: 'create' })}>
            <Plus size={14} /> New automation
          </button>
        </div>
      </div>

      <AccessNotice state={list.state} />
      <ResourceNotice state={list.state} resourceLabel="automations" />

      {editor && (
        <AutomationEditor
          editor={editor}
          pending={mutations.pending}
          error={mutations.state.kind === 'error' ? mutations.state.error : null}
          onCancel={() => {
            mutations.reset();
            setEditor(null);
          }}
          onSubmit={async body => {
            const result =
              editor.mode === 'create'
                ? await mutations.create(body as AutomationCreate)
                : await mutations.update(editor.automation.automation_id, body as AutomationUpdate);
            if (result) {
              notify(editor.mode === 'create' ? 'Automation created in Devin' : 'Automation updated in Devin');
              if (editor.mode === 'create') setSelectedId(result.automation_id);
              setEditor(null);
            }
          }}
        />
      )}

      {items.length > 0 && (
        <div className="automations-layout">
          <div className="card automation-list">
            <div className="card-header">
              <div>
                <h2>Automations</h2>
                <p>{list.data?.total ?? items.length} in the organization</p>
              </div>
            </div>
            <ul>
              {items.map(automation => (
                <li key={automation.automation_id}>
                  <button className={automation.automation_id === selected?.automation_id ? 'automation-row active' : 'automation-row'} onClick={() => setSelectedId(automation.automation_id)}>
                    <KindIcon automation={automation} />
                    <span className="automation-row-body">
                      <strong>{automation.name}</strong>
                      <small>
                        {automation.enabled ? 'Enabled' : 'Disabled'} · {automation.event_types.join(', ') || 'no trigger'}
                        {automation.managed_by_relay && ' · Relay-managed'}
                      </small>
                    </span>
                    <span className={`state-dot ${automation.enabled ? 'on' : 'off'}`} aria-label={automation.enabled ? 'enabled' : 'disabled'} />
                  </button>
                </li>
              ))}
            </ul>
          </div>

          {selected && (
            <AutomationDetailPanel
              automation={selected}
              detailState={detail}
              pending={mutations.pending}
              mutationError={editor === null && mutations.state.kind === 'error' ? mutations.state.error : null}
              onEdit={() => {
                mutations.reset();
                setEditor({ mode: 'edit', automation: selected });
              }}
              onToggle={async () => {
                const result = await mutations.update(selected.automation_id, { enabled: !selected.enabled });
                if (result) notify(result.enabled ? 'Automation enabled' : 'Automation disabled');
              }}
              onDelete={async () => {
                const ok = await mutations.remove(selected.automation_id);
                if (ok !== null) {
                  notify('Automation deleted in Devin');
                  setSelectedId(items.find(a => a.automation_id !== selected.automation_id)?.automation_id ?? null);
                }
              }}
              goToIssue={goToIssue}
            />
          )}
        </div>
      )}
    </section>
  );
}

function AccessNotice({ state }: { state: ReturnType<typeof useAutomations>['state'] }) {
  if (state.kind !== 'error') return null;
  const error = state.error;
  if (error.code === 'unauthorized') {
    return (
      <div className="resource-notice error" role="alert">
        <Lock size={16} />
        <span>
          {error.status === 401
            ? 'Sign in with an operator token to view automations.'
            : error.message.includes('not configured')
              ? 'Devin automations are not configured on this deployment (set DEVIN_API_TOKEN and DEVIN_ORG_ID).'
              : `Not permitted: ${error.message}. The Devin token needs ViewOrgAutomations/ManageOrgAutomations and the Relay principal must be an operator.`}
        </span>
      </div>
    );
  }
  return null;
}

function KindIcon({ automation }: { automation: Automation }) {
  if (automation.relay_kind === 'reproduction') return <TestTube2 size={16} className="kind-icon reproduction" />;
  if (automation.relay_kind === 'fix') return <Bot size={16} className="kind-icon fix" />;
  return <Workflow size={16} className="kind-icon" />;
}

function AutomationDetailPanel({
  automation,
  detailState,
  pending,
  mutationError,
  onEdit,
  onToggle,
  onDelete,
  goToIssue,
}: {
  automation: Automation;
  detailState: ReturnType<typeof useAutomation>;
  pending: boolean;
  mutationError: ApiError | null;
  onEdit: () => void;
  onToggle: () => void;
  onDelete: () => void;
  goToIssue: (id: string) => void;
}) {
  const now = Date.now();
  const sessions = detailState.data?.sessions;
  const metadata = Object.entries(automation.metadata);
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  useEffect(() => setConfirmingDelete(false), [automation.automation_id]);
  return (
    <div className="automation-detail">
      <div className="card">
        <div className="card-header">
          <div>
            <h2>
              <KindIcon automation={automation} /> {automation.name}
            </h2>
          </div>
          <div className="automation-actions">
            <button className="secondary-button" onClick={onToggle} disabled={pending}>
              {automation.enabled ? 'Disable' : 'Enable'}
            </button>
            <button className="secondary-button" onClick={onEdit} disabled={pending}>
              <Pencil size={14} /> Edit
            </button>
            <button className="secondary-button danger" onClick={() => setConfirmingDelete(true)} disabled={pending || confirmingDelete}>
              <Trash2 size={14} /> Delete
            </button>
          </div>
        </div>
        {confirmingDelete && (
          <div className="automation-confirm" role="alertdialog">
            <AlertTriangle size={16} />
            <p>
              {automation.managed_by_relay
                ? `Delete "${automation.name}" from Devin? Relay recreates it the next time the worker starts.`
                : `Delete "${automation.name}" from Devin? This cannot be undone from Relay.`}
            </p>
            <button className="secondary-button" onClick={() => setConfirmingDelete(false)} disabled={pending}>
              Cancel
            </button>
            <button
              className="primary-button danger"
              disabled={pending}
              onClick={() => {
                setConfirmingDelete(false);
                onDelete();
              }}
            >
              Delete permanently
            </button>
          </div>
        )}
        {mutationError && <MutationError error={mutationError} />}
        <dl className="automation-facts">
          <div>
            <dt>Automation ID</dt>
            <dd>
              <code>{automation.automation_id}</code>
            </dd>
          </div>
          <div>
            <dt>State</dt>
            <dd>{automation.enabled ? 'Enabled' : 'Disabled'}</dd>
          </div>
          <div>
            <dt>Created</dt>
            <dd>
              {automation.created_at ? formatAgo(automation.created_at, now) : '–'}
              {automation.created_by && <small> by {automation.created_by}</small>}
            </dd>
          </div>
          <div>
            <dt>Updated</dt>
            <dd>{automation.updated_at ? formatAgo(automation.updated_at, now) : '–'}</dd>
          </div>
          <div>
            <dt>Last invocation</dt>
            <dd>
              {automation.last_invocation_status ?? 'never'}
              {automation.last_invocation_at && <small> · {formatAgo(automation.last_invocation_at, now)}</small>}
            </dd>
          </div>
          <div>
            <dt>Relay role</dt>
            <dd>
              {automation.relay_kind === 'reproduction'
                ? 'Triage + reproduction — fired by Devin on every new Superset issue and on reporter follow-ups'
                : automation.relay_kind === 'fix'
                  ? 'Fix — fired by Devin on its own “reproduced” issue comment; opens a PR linked to the issue, never merges'
                  : 'Not used by Relay’s gated pipeline'}
            </dd>
          </div>
        </dl>
      </div>

      <div className="card">
        <div className="card-header">
          <div>
            <h2>Workflow</h2>
            <p>Trigger → action as defined in Devin</p>
          </div>
        </div>
        <ol className="automation-workflow">
          <li>
            <span className="step-icon">
              <Webhook size={14} />
            </span>
            <div>
              <strong>Trigger</strong>
              <p>
                {automation.event_types.length === 0 ? 'No trigger configured' : automation.event_types.join(', ')}
                {automation.has_inbox && <small> · inbox URL and secret are kept server-side</small>}
                {automation.event_types.includes('github:issues') && <small> · fired by Devin's GitHub connection, no Relay webhook needed</small>}
              </p>
              {automation.triggers.some((t) => t.conditions) && (
                <ul className="trigger-conditions">
                  {automation.triggers.flatMap((t) => describeConditions(t.conditions)).map((line) => (
                    <li key={line}>
                      <code>{line}</code>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </li>
          <li>
            <span className="step-icon">
              <Bot size={14} />
            </span>
            <div>
              <strong>Action: start_session</strong>
              <pre className="automation-prompt">{automation.prompt ?? 'No prompt returned by Devin'}</pre>
            </div>
          </li>
        </ol>
        {metadata.length > 0 && (
          <div className="automation-metadata">
            {metadata.map(([key, value]) => (
              <span key={key} className="meta-chip">
                <b>{key}</b>={value}
              </span>
            ))}
          </div>
        )}
      </div>

      <div className="card">
        <div className="card-header">
          <div>
            <h2>Devin sessions</h2>
            <p>Sessions Devin lists under this automation, plus Relay’s own records where it dispatched them</p>
          </div>
          <DataSourceBadge state={detailState.state} compact />
        </div>
        {detailState.state.kind === 'loading' && (
          <p className="session-empty">
            <Loader2 size={14} className="spin" /> Loading sessions…
          </p>
        )}
        {sessions?.provider_error && (
          <div className="resource-notice stale" role="status">
            <AlertTriangle size={16} />
            <span>Devin did not return this automation’s sessions: {sessions.provider_error}</span>
          </div>
        )}
        {sessions && <SessionTable provider={sessions.provider_sessions} relay={sessions.relay_sessions} goToIssue={goToIssue} now={now} />}
      </div>
    </div>
  );
}

function SessionTable({ provider, relay, goToIssue, now }: { provider: ProviderSession[]; relay: SessionSummary[]; goToIssue: (id: string) => void; now: number }) {
  const relayByUrl = useMemo(() => {
    const map = new Map<string, SessionSummary>();
    for (const s of relay) if (s.devin_session_url) map.set(s.devin_session_url, s);
    return map;
  }, [relay]);
  const unmatched = relay.filter(s => !s.devin_session_url || !provider.some(p => p.url === s.devin_session_url));
  if (provider.length === 0 && relay.length === 0) return <p className="session-empty">No sessions have been started by this automation.</p>;
  return (
    <div className="recent-sessions">
      <div className="recent-sessions-head">
        <span>Session</span>
        <span>Issue</span>
        <span>Status</span>
        <span>Started</span>
        <span>Link</span>
      </div>
      {provider.map(session => {
        const local = relayByUrl.get(session.url);
        return (
          <div className="recent-sessions-row" key={session.session_id}>
            <span>
              {session.title}
              <small>{session.session_id}</small>
            </span>
            {local ? (
              <button className="link-button" onClick={() => goToIssue(local.issue_id)}>
                {local.issue_key}
                <small>{local.issue_title}</small>
              </button>
            ) : (
              <small>{session.tags.find(t => t.startsWith('task:')) ?? 'not tracked by Relay'}</small>
            )}
            <span className="session-status">{session.status}</span>
            <span>{formatAgo(session.created_at, now)}</span>
            <span>
              <SafeLink href={session.url} policy="devin" className="text-button">
                Open <ExternalLink size={13} />
              </SafeLink>
            </span>
          </div>
        );
      })}
      {unmatched.map(session => (
        <div className="recent-sessions-row" key={session.id}>
          <span>
            {session.title}
            <small>{session.dry_run ? 'dry run' : 'awaiting Devin session'}</small>
          </span>
          <button className="link-button" onClick={() => goToIssue(session.issue_id)}>
            {session.issue_key}
            <small>{session.issue_title}</small>
          </button>
          <span className="session-status">{session.status}</span>
          <span>{formatAgo(session.created_at, now)}</span>
          <span>
            <small>Relay record only</small>
          </span>
        </div>
      ))}
    </div>
  );
}

function MutationError({ error }: { error: ApiError }) {
  const hint =
    error.code === 'unauthorized'
      ? 'Only operators may manage automations, and the Devin token needs ManageOrgAutomations.'
      : error.code === 'unavailable'
        ? 'Devin’s API did not answer.'
        : error.code === 'not_found'
          ? 'This automation no longer exists in Devin.'
          : null;
  return (
    <div className="resource-notice error" role="alert">
      <AlertTriangle size={16} />
      <span>
        {error.message}
        {hint && <> — {hint}</>}
      </span>
    </div>
  );
}

function AutomationEditor({
  editor,
  pending,
  error,
  onCancel,
  onSubmit,
}: {
  editor: NonNullable<Editor>;
  pending: boolean;
  error: ApiError | null;
  onCancel: () => void;
  onSubmit: (body: AutomationCreate | AutomationUpdate) => Promise<void>;
}) {
  const existing = editor.mode === 'edit' ? editor.automation : null;
  const [name, setName] = useState(existing?.name ?? '');
  const [prompt, setPrompt] = useState(existing?.prompt ?? '');
  const [enabled, setEnabled] = useState(existing?.enabled ?? true);
  const [metadata, setMetadata] = useState('');
  const [validation, setValidation] = useState<string | null>(null);

  const submit = () => {
    if (!name.trim()) return setValidation('Name is required.');
    if (!prompt.trim()) return setValidation('Prompt is required.');
    setValidation(null);
    if (existing) {
      const patch: AutomationUpdate = {};
      if (name !== existing.name) patch.name = name;
      if (prompt !== (existing.prompt ?? '')) patch.prompt = prompt;
      if (enabled !== existing.enabled) patch.enabled = enabled;
      if (Object.keys(patch).length === 0) return setValidation('Nothing changed.');
      return void onSubmit(patch);
    }
    const meta: Record<string, string> = {};
    for (const line of metadata.split('\n')) {
      const trimmed = line.trim();
      if (!trimmed) continue;
      const eq = trimmed.indexOf('=');
      if (eq <= 0) return setValidation(`Metadata line "${trimmed}" must be key=value.`);
      const key = trimmed.slice(0, eq).trim();
      if (key === 'relay_kind' || key === 'relay_repo') return setValidation(`Metadata key ${key} is reserved for Relay.`);
      meta[key] = trimmed.slice(eq + 1).trim();
    }
    void onSubmit({ name, prompt, enabled, event_type: 'webhook:incoming', metadata: meta });
  };

  return (
    <div className="card automation-editor">
      <div className="card-header">
        <div>
          <h2>{existing ? `Edit ${existing.name}` : 'New automation'}</h2>
          <p>
            {existing
              ? 'Changes are written to Devin. The webhook trigger and its secret are left untouched.'
              : 'Created in Devin with a webhook:incoming trigger and a start_session action that runs as the organization and never bypasses approval.'}
          </p>
        </div>
        <button className="icon-button" onClick={onCancel} aria-label="Close editor">
          <X size={16} />
        </button>
      </div>
      <div className="automation-form">
        <label>
          <span>Name</span>
          <input value={name} onChange={e => setName(e.target.value)} maxLength={200} disabled={pending} />
        </label>
        <label>
          <span>Prompt (start_session action)</span>
          <textarea value={prompt} onChange={e => setPrompt(e.target.value)} rows={8} maxLength={20000} disabled={pending} />
        </label>
        {!existing && (
          <label>
            <span>Metadata (one key=value per line)</span>
            <textarea value={metadata} onChange={e => setMetadata(e.target.value)} rows={3} disabled={pending} placeholder="team=qa" />
          </label>
        )}
        <label className="checkbox">
          <input type="checkbox" checked={enabled} onChange={e => setEnabled(e.target.checked)} disabled={pending} />
          <span>Enabled</span>
        </label>
        {validation && (
          <div className="resource-notice error" role="alert">
            <AlertTriangle size={16} />
            <span>{validation}</span>
          </div>
        )}
        {error && <MutationError error={error} />}
        <div className="automation-actions">
          <button className="secondary-button" onClick={onCancel} disabled={pending}>
            Cancel
          </button>
          <button className="primary-button" onClick={submit} disabled={pending}>
            {pending ? <Loader2 size={14} className="spin" /> : null} {existing ? 'Save changes' : 'Create in Devin'}
          </button>
        </div>
      </div>
    </div>
  );
}
