/**
 * Resource schemas for the Relay `/api/v1` contract.
 *
 * Wire format: UUID ids, UTC ISO-8601 timestamps, snake_case fields,
 * explicit `version` on mutable resources. These mirror
 * docs/architecture/issue-automation-platform.md.
 */

export const TARGET_REPOSITORY = 'exloong/superset';

export type UUID = string;
export type IsoTimestamp = string;

export type LifecycleState =
  | 'new'
  | 'triage'
  | 'awaiting_reporter'
  | 'reproducing'
  | 'blocked_environment'
  | 'needs_owner_decision'
  | 'fix_authorized'
  | 'fixing'
  | 'pr_open'
  | 'awaiting_owner'
  | 'changes_requested'
  | 'completed'
  | 'duplicate'
  | 'not_a_bug'
  | 'unsupported'
  | 'closed_inactive'
  | 'security_private'
  | 'automation_error';

export type AgentSessionStatus =
  | 'queued'
  | 'running'
  | 'completed'
  | 'failed'
  | 'needs_attention'
  | 'cancelled';

export interface RepositoryRef {
  full_name: string;
  html_url: string;
  default_branch?: string;
  dry_run: boolean;
}

export interface Actor {
  kind: 'reporter' | 'owner' | 'operator' | 'agent' | 'system' | 'security';
  display_name: string;
  login?: string;
  avatar_url?: string;
}

export interface LifecycleEvent {
  id: UUID;
  issue_id: UUID;
  correlation_id: string;
  occurred_at: IsoTimestamp;
  kind: string;
  summary: string;
  detail?: string;
  from_state?: LifecycleState;
  to_state?: LifecycleState;
  actor?: Actor;
  session_id?: UUID;
  outcome: 'accepted' | 'rejected' | 'duplicate' | 'pending' | 'failed';
}

export type QuestionStatus = 'open' | 'answered' | 'unavailable' | 'invalid' | 'expired';

export interface InformationRequest {
  id: UUID;
  issue_id: UUID;
  issue_revision: number;
  field: string;
  prompt: string;
  rationale: string;
  safe_example?: string;
  prohibited_data: string[];
  required: boolean;
  status: QuestionStatus;
  answer?: string;
  answered_at?: IsoTimestamp;
  reminder_due_at?: IsoTimestamp;
  inactivity_close_at?: IsoTimestamp;
}

export type EvidenceStatus = 'complete' | 'draft' | 'missing' | 'invalid';

export interface EvidenceItem {
  id: UUID;
  label: string;
  value: string;
  status: EvidenceStatus;
  artifact_id?: UUID;
}

export interface EvidencePacket {
  completeness: number;
  reproduction_ready: boolean;
  items: EvidenceItem[];
  observed_behavior?: string;
  expected_behavior_evidence?: string;
  target_version?: string;
  control_version?: string;
  target_result?: string;
  control_result?: string;
  repeat_count?: number;
  regression_window?: string;
  minimal_condition?: string;
  security_classification: 'none' | 'suspected' | 'confirmed_private';
  remaining_uncertainty?: string;
}

export type DecisionKind =
  | 'confirm_bug'
  | 'request_discriminator'
  | 'reclassify'
  | 'route_security_private'
  | 'approve_pr'
  | 'request_changes';

export interface HumanDecision {
  id: UUID;
  issue_id: UUID;
  issue_revision: number;
  kind: DecisionKind;
  actor: Actor;
  decided_at: IsoTimestamp;
  rationale?: string;
  superseded_by?: UUID;
  authorization?: {
    scope: string;
    expires_at: IsoTimestamp;
  };
}

export interface OwnerCandidate {
  team: string;
  initials?: string;
  rule: string;
  rationale: string;
  paths?: string[];
  selected: boolean;
  review_requested?: 'accepted' | 'rejected' | 'pending' | 'not_requested';
}

export interface OwnerRouting {
  state: 'resolved' | 'ambiguous' | 'no_owner';
  candidates: OwnerCandidate[];
  escalation?: string;
}

export interface HumanGate {
  kind: 'reporter' | 'owner' | 'security' | 'operator' | 'none';
  waiting_since?: IsoTimestamp;
  due_at?: IsoTimestamp;
  workspace_released: boolean;
  escalation?: string;
}

export interface IssueSummary {
  id: UUID;
  version: number;
  repository: RepositoryRef;
  external_number: number;
  key: string;
  title: string;
  html_url: string;
  reporter: Actor;
  state: LifecycleState;
  category?: string;
  opened_at: IsoTimestamp;
  updated_at: IsoTimestamp;
  owner_routing: OwnerRouting;
  confidence?: number;
  progress: number;
  next_action: string;
  next_action_due?: string;
  missing_fields: string[];
  human_gate: HumanGate;
}

export interface IssueDetail extends IssueSummary {
  revision: number;
  body_excerpt?: string;
  events: LifecycleEvent[];
  questions: InformationRequest[];
  evidence: EvidencePacket;
  decisions: HumanDecision[];
  session_ids: UUID[];
  pull_requests: PullRequestRef[];
}

export interface ConversationAttachment {
  id: UUID;
  name: string;
  content_type: string;
  size_bytes: number;
  url?: string;
}

export interface ConversationMessage {
  id: UUID;
  author: Actor;
  sent_at: IsoTimestamp;
  body: string;
  attachments: ConversationAttachment[];
}

export interface SessionEvent {
  id: UUID;
  occurred_at: IsoTimestamp;
  label: string;
  detail?: string;
  state: 'complete' | 'active' | 'pending' | 'blocked';
}

export interface SessionOutput {
  id: UUID;
  schema: string;
  label: string;
  summary?: string;
  produced_at: IsoTimestamp;
  accepted: boolean;
}

export interface Artifact {
  id: UUID;
  kind: string;
  label: string;
  content_type: string;
  size_bytes: number;
  url?: string;
  retained: boolean;
}

export interface PullRequestRef {
  repository: string;
  number: number;
  title: string;
  html_url: string;
  head_branch: string;
  base_branch: string;
  head_sha: string;
  draft: boolean;
  state: 'open' | 'closed' | 'merged';
  checks: 'pending' | 'passed' | 'failed' | 'unknown';
  review: 'none' | 'requested' | 'approved' | 'changes_requested';
}

export type ReviewFindingSeverity = 'low' | 'medium' | 'high' | 'critical';

export interface ReviewFinding {
  id: UUID;
  severity: ReviewFindingSeverity;
  title: string;
  path?: string;
  line?: number;
  detail?: string;
}

export interface DevinReviewState {
  status: 'not_requested' | 'queued' | 'running' | 'completed' | 'failed';
  pull_request_number?: number;
  head_sha?: string;
  url?: string;
  findings: ReviewFinding[];
  completed_at?: IsoTimestamp;
}

export interface SessionBudget {
  wall_seconds: number;
  retry_limit: number;
  retries_used: number;
  allowed_capabilities: string[];
  max_output_bytes: number;
}

export interface SessionLinks {
  devin_session_url?: string;
  devin_desktop_url?: string;
  conversation_embeddable: boolean;
  desktop_embeddable: boolean;
}

export interface SessionSummary {
  id: UUID;
  version: number;
  issue_id: UUID;
  issue_key: string;
  issue_title: string;
  title: string;
  kind: SessionKind;
  transition: string;
  actor: string;
  status: AgentSessionStatus;
  dry_run: boolean;
  created_at: IsoTimestamp;
  started_at?: IsoTimestamp;
  ended_at?: IsoTimestamp;
  last_heartbeat_at?: IsoTimestamp;
  /** 0–100 only when Relay holds an explicitly synchronized value; Devin v3 session data documents no progress percentage. */
  progress?: number | null;
  budget: SessionBudget;
  repository: RepositoryRef;
  target_commit: string;
  branch?: string;
  workspace: { id?: string; released: boolean; released_at?: IsoTimestamp };
  trigger: string;
  /** Relay-internal fields; absent when the upstream session does not expose them. */
  current_action?: string | null;
  next_checkpoint?: string | null;
  correlation_id: string;
  human_gate?: HumanGate;
  devin_session_url?: string | null;
}

export interface SessionDetail extends SessionSummary {
  conversation: ConversationMessage[] | null;
  events: SessionEvent[];
  outputs: SessionOutput[];
  artifacts: Artifact[];
  pull_requests: PullRequestRef[];
  review: DevinReviewState;
  links: SessionLinks;
  human_gate: HumanGate;
}

export interface SessionCapacity {
  running: number;
  slots: number;
  queued: number;
  next_slot_eta_seconds?: number;
}

export interface WorkflowStep {
  id: string;
  label: string;
  kind: 'automation' | 'ai' | 'human' | 'terminal';
  states: LifecycleState[];
  sla?: string;
  actor: string;
}

export interface WorkflowDefinition {
  version: string;
  status: 'draft' | 'active';
  steps: WorkflowStep[];
  updated_at: IsoTimestamp;
}

export interface AnalyticsSummary {
  period: { from: IsoTimestamp; to: IsoTimestamp };
  issues_processed: number;
  confirmed_bugs: number;
  reproduced_autonomously_pct: number;
  median_to_owner_decision_hours: number | null;
  state_counts: Partial<Record<LifecycleState, number>>;
  outcome_mix: { label: string; value: number }[];
  owner_load: { owner: string; initials: string; active: number; waiting: number; sla: number }[];
  generated_at: IsoTimestamp;
}

export type SessionKind = 'reproduction' | 'fix';
export type ProbeStatus = 'ok' | 'stale' | 'unavailable';
export type ProviderStatus = 'connected' | 'dry_run' | 'stale' | 'unconfigured';
export type SystemStatus = 'healthy' | 'degraded' | 'down';

export interface ThroughputBucket {
  day: IsoTimestamp;
  entered: number;
  completed: number;
}

export interface StageCount {
  id: string;
  label: string;
  kind: 'automation' | 'ai' | 'human' | 'terminal';
  actor: string;
  count: number;
}

export interface SessionHealth {
  kind: SessionKind;
  queued: number;
  running: number;
  completed: number;
  failed: number;
  needs_attention: number;
  cancelled: number;
  total: number;
  success_rate_pct: number | null;
  median_duration_seconds: number | null;
  last_launched_at: IsoTimestamp | null;
}

export interface RecentSession {
  id: UUID;
  kind: SessionKind;
  issue_id: UUID;
  issue_key: string;
  issue_title: string;
  status: AgentSessionStatus;
  dry_run: boolean;
  created_at: IsoTimestamp;
  started_at: IsoTimestamp | null;
  ended_at: IsoTimestamp | null;
  duration_seconds: number | null;
  devin_session_url: string | null;
}

export interface AutomationStatus {
  kind: 'reproduction' | 'fix';
  automation_id: string;
  enabled: boolean;
  updated_at: IsoTimestamp;
}

/** A Devin automation as exposed by Relay; inbox URL and secret are never sent to the browser. */
export interface Automation {
  automation_id: string;
  name: string;
  description: string | null;
  enabled: boolean;
  event_types: string[];
  prompt: string | null;
  metadata: Record<string, string>;
  relay_kind: SessionKind | null;
  managed_by_relay: boolean;
  created_at: IsoTimestamp | null;
  updated_at: IsoTimestamp | null;
  created_by: string | null;
  last_invocation_status: string | null;
  last_invocation_at: IsoTimestamp | null;
  has_inbox: boolean;
}

export interface AutomationPage {
  items: Automation[];
  total: number;
  generated_at: IsoTimestamp;
}

export interface AutomationCreate {
  name: string;
  prompt: string;
  description?: string | null;
  enabled?: boolean;
  event_type?: 'webhook:incoming';
  metadata?: Record<string, string>;
}

export interface AutomationUpdate {
  name?: string;
  prompt?: string;
  description?: string | null;
  enabled?: boolean;
}

/** A session Devin lists under an automation; it may predate Relay or lack Relay tags. */
export interface ProviderSession {
  session_id: string;
  title: string;
  status: string;
  url: string;
  created_at: IsoTimestamp;
  updated_at: IsoTimestamp;
  tags: string[];
}

export interface AutomationSessions {
  automation_id: string;
  relay_sessions: SessionSummary[];
  provider_sessions: ProviderSession[];
  provider_error: string | null;
  generated_at: IsoTimestamp;
}

export interface AutomationDetail {
  automation: Automation;
  sessions: AutomationSessions;
}

export interface Heartbeat {
  database: ProbeStatus;
  worker: ProbeStatus;
  github: ProviderStatus;
  devin: ProviderStatus;
  last_worker_heartbeat_at: IsoTimestamp | null;
  last_webhook_received_at: IsoTimestamp | null;
  last_webhook_event: string | null;
  last_session_launched_at: IsoTimestamp | null;
  automations: AutomationStatus[];
  overall: SystemStatus;
  reasons: string[];
}

export interface DashboardSummary {
  period: { from: IsoTimestamp; to: IsoTimestamp };
  throughput: { entered: number; completed: number; buckets: ThroughputBucket[] };
  in_flight: StageCount[];
  sessions: SessionHealth[];
  recent_sessions: RecentSession[];
  heartbeat: Heartbeat;
  generated_at: IsoTimestamp;
}

export interface Page<T> {
  items: T[];
  total: number;
  generated_at: IsoTimestamp;
}

export interface Health {
  status: 'ok' | 'degraded';
  version?: string;
}

export interface Readiness {
  database: 'ok' | 'unavailable';
  worker: 'ok' | 'stale' | 'unavailable';
  last_worker_heartbeat_at?: IsoTimestamp;
}

export interface CommandAccepted {
  event_id: UUID;
  resource_id: UUID;
  resource_version: number;
  accepted_at: IsoTimestamp;
  processing: 'complete' | 'async';
}

export interface ReporterResponseCommand {
  question_id: UUID;
  issue_revision: number;
  response: { kind: 'answer'; value: string } | { kind: 'unavailable'; reason?: string };
}

export interface OwnerDecisionCommand {
  issue_revision: number;
  kind: DecisionKind;
  rationale?: string;
  reclassify_as?: 'duplicate' | 'not_a_bug' | 'unsupported' | 'support';
  field?: string;
  prompt?: string;
  pr_number?: number;
  head_sha?: string;
}

export interface RetryCommand {
  reason: string;
}

export interface CancelSessionCommand {
  reason: string;
}

export interface SessionMessageCommand {
  body: string;
}

export interface IssueFilters {
  state?: LifecycleState[];
  owner?: string;
  search?: string;
}

export interface SessionFilters {
  status?: AgentSessionStatus[];
  issue_id?: UUID;
  kind?: SessionKind;
}
