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
  | 'route_security_private';

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
  transition: string;
  actor: string;
  status: AgentSessionStatus;
  created_at: IsoTimestamp;
  started_at?: IsoTimestamp;
  ended_at?: IsoTimestamp;
  last_heartbeat_at?: IsoTimestamp;
  progress: number;
  budget: SessionBudget;
  repository: RepositoryRef;
  target_commit: string;
  branch?: string;
  workspace: { id?: string; released: boolean; released_at?: IsoTimestamp };
  trigger: string;
  current_action: string;
  next_checkpoint?: string;
  correlation_id: string;
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
  median_to_owner_decision_hours: number;
  state_counts: Partial<Record<LifecycleState, number>>;
  outcome_mix: { label: string; value: number }[];
  owner_load: { owner: string; initials: string; active: number; waiting: number; sla: number }[];
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
}
