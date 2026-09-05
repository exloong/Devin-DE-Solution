export type ViewKey = 'overview' | 'workflow' | 'experience' | 'sessions' | 'issues' | 'settings';

export type IssueState =
  | 'Needs information'
  | 'Reproducing'
  | 'Owner decision'
  | 'Fix in progress'
  | 'PR in review'
  | 'Redirected'
  | 'Closed · inactive';

export interface Issue {
  id: number;
  key: string;
  title: string;
  author: string;
  avatar: string;
  state: IssueState;
  category: string;
  age: string;
  updated: string;
  owner: string;
  ownerInitials: string;
  confidence: number;
  progress: number;
  accent: string;
  nextAction: string;
  due: string;
  missing: string[];
}

export interface FlowStep {
  id: string;
  label: string;
  description: string;
  kind: 'automation' | 'ai' | 'human' | 'terminal';
  eyebrow: string;
  x: number;
  y: number;
  sla: string;
  actor: string;
  entry: string;
  exit: string;
  fallback: string;
  actions: string[];
}

export type DevinSessionStatus = 'Running' | 'Queued' | 'Needs attention' | 'Waiting on owner' | 'Completed';

export interface DevinSessionEvent {
  label: string;
  detail: string;
  time: string;
  state: 'complete' | 'active' | 'pending' | 'blocked';
}

export interface DevinSession {
  id: string;
  issueId: number;
  issueKey: string;
  issueTitle: string;
  title: string;
  flowStep: string;
  actor: string;
  status: DevinSessionStatus;
  started: string;
  elapsed: string;
  updated: string;
  progress: number;
  budget: string;
  environment: string;
  trigger: string;
  currentAction: string;
  nextCheckpoint: string;
  branch?: string;
  events: DevinSessionEvent[];
  artifacts: string[];
}

export const issues: Issue[] = [
  {
    id: 43218,
    key: 'SUP-43218',
    title: 'Dashboard filters reset after force refresh',
    author: 'Mina K.',
    avatar: 'MK',
    state: 'Needs information',
    category: 'Dashboard',
    age: '12d',
    updated: '4h ago',
    owner: 'Dashboard Experience',
    ownerInitials: 'DX',
    confidence: 72,
    progress: 32,
    accent: '#e99b4b',
    nextAction: 'Reporter reply',
    due: 'Reminder due tomorrow',
    missing: ['Screen recording', 'Feature flag list'],
  },
  {
    id: 43207,
    key: 'SUP-43207',
    title: 'Trino temporal column displays shifted timezone',
    author: 'Owen L.',
    avatar: 'OL',
    state: 'Reproducing',
    category: 'Databases',
    age: '6d',
    updated: '26m ago',
    owner: 'Database Connectivity',
    ownerInitials: 'DB',
    confidence: 84,
    progress: 54,
    accent: '#6b61e8',
    nextAction: 'Devin reproduction',
    due: 'Running · 08:42',
    missing: [],
  },
  {
    id: 43231,
    key: 'SUP-43231',
    title: 'Bulk tag removal returns a 500 response',
    author: 'Ari P.',
    avatar: 'AP',
    state: 'Owner decision',
    category: 'Backend',
    age: '4d',
    updated: '1h ago',
    owner: 'Core Platform',
    ownerInitials: 'CP',
    confidence: 96,
    progress: 68,
    accent: '#1eac83',
    nextAction: 'Confirm bug',
    due: 'Owner SLA · 2d',
    missing: [],
  },
  {
    id: 42991,
    key: 'SUP-42991',
    title: 'CSV export ignores configured row limit',
    author: 'Jo F.',
    avatar: 'JF',
    state: 'PR in review',
    category: 'Data export',
    age: '19d',
    updated: '2h ago',
    owner: 'Core Platform',
    ownerInitials: 'CP',
    confidence: 98,
    progress: 91,
    accent: '#278bd9',
    nextAction: 'Code-owner review',
    due: 'PR #43302 · checks passed',
    missing: [],
  },
  {
    id: 43104,
    key: 'SUP-43104',
    title: 'Custom OAuth callback fails behind proxy',
    author: 'Nora S.',
    avatar: 'NS',
    state: 'Redirected',
    category: 'Configuration',
    age: '14d',
    updated: '3d ago',
    owner: 'Security & Auth',
    ownerInitials: 'SA',
    confidence: 89,
    progress: 100,
    accent: '#8b96a9',
    nextAction: 'Reporter',
    due: 'Guidance delivered',
    missing: [],
  },
  {
    id: 42856,
    key: 'SUP-42856',
    title: 'SQL Lab result disappears intermittently',
    author: 'Ilya R.',
    avatar: 'IR',
    state: 'Closed · inactive',
    category: 'SQL Lab',
    age: '34d',
    updated: '6d ago',
    owner: 'SQL Lab',
    ownerInitials: 'SL',
    confidence: 61,
    progress: 44,
    accent: '#9d7086',
    nextAction: 'Reporter may reopen',
    due: 'Closed after 2 reminders',
    missing: ['Worker logs', 'Reliable trigger'],
  },
];

export const flowSteps: FlowStep[] = [
  {
    id: 'intake',
    label: 'Normalize intake',
    description: 'Parse the report, redact risky content, and build a structured issue record.',
    kind: 'automation',
    eyebrow: 'Deterministic',
    x: 5,
    y: 43,
    sla: '< 1 min',
    actor: 'Workflow controller',
    entry: 'Maintainer enrolls issue',
    exit: 'Valid structured report',
    fallback: 'Flag malformed input',
    actions: ['Parse issue form', 'Detect secrets/security', 'Create status card'],
  },
  {
    id: 'classify',
    label: 'Classify outcome',
    description: 'Distinguish likely bugs from support, feature requests, duplicates, and ambiguity.',
    kind: 'ai',
    eyebrow: 'Devin analysis',
    x: 24,
    y: 43,
    sla: '< 10 min',
    actor: 'Devin triage agent',
    entry: 'Normalized report',
    exit: 'Evidence-backed recommendation',
    fallback: 'Abstain to human triage',
    actions: ['Search related issues', 'Check support policy', 'State confidence and evidence'],
  },
  {
    id: 'needs-info',
    label: 'Guide reporter',
    description: 'Ask only for the smallest missing evidence, with examples and safe commands.',
    kind: 'ai',
    eyebrow: 'Conversation',
    x: 43,
    y: 13,
    sla: '7d reply',
    actor: 'Devin + reporter',
    entry: 'Missing blocking facts',
    exit: 'Reproduction-ready context',
    fallback: 'Two reminders, then close',
    actions: ['Tailored checklist', 'Validate new evidence', 'Never repeat answered questions'],
  },
  {
    id: 'reproduce',
    label: 'Reproduce safely',
    description: 'Build a clean fixture, execute the reported path, and capture repeatable evidence.',
    kind: 'ai',
    eyebrow: 'Devin workspace',
    x: 43,
    y: 43,
    sla: '< 60 min',
    actor: 'Devin reproducer',
    entry: 'Context completeness ≥ 80%',
    exit: 'Repeatable failure or control result',
    fallback: 'Request one missing discriminator',
    actions: ['Create isolated environment', 'Run control and failure cases', 'Draft regression test'],
  },
  {
    id: 'redirect',
    label: 'Resolve without code',
    description: 'Route support, configuration, feature, duplicate, or unsupported-version reports.',
    kind: 'terminal',
    eyebrow: 'Alternate outcome',
    x: 43,
    y: 73,
    sla: '< 1d',
    actor: 'Maintainer-confirmed',
    entry: 'Not a product defect',
    exit: 'Clear destination and rationale',
    fallback: 'Reopen when new evidence arrives',
    actions: ['Suggest canonical resource', 'Link duplicate evidence', 'Preserve reopening path'],
  },
  {
    id: 'validate',
    label: 'Confirm the bug',
    description: 'Give the code owner a compact evidence pack and an explicit decision.',
    kind: 'human',
    eyebrow: 'Human gate',
    x: 62,
    y: 43,
    sla: '3 business days',
    actor: 'Component owner',
    entry: 'Reproduced or high-confidence intermittent',
    exit: 'Fix authorized or reclassified',
    fallback: 'Escalate to triage rotation',
    actions: ['Review evidence pack', 'Confirm expected behavior', 'Authorize code changes'],
  },
  {
    id: 'fix',
    label: 'Prepare the fix',
    description: 'Write the regression test, implement the smallest fix, and verify affected suites.',
    kind: 'ai',
    eyebrow: 'Devin coding',
    x: 78,
    y: 43,
    sla: 'Bounded session',
    actor: 'Devin coding agent',
    entry: 'Owner-authorized defect',
    exit: 'Reviewable, tested branch',
    fallback: 'Return with blocker summary',
    actions: ['Regression test first', 'Focused implementation', 'Lint, typecheck, affected tests'],
  },
  {
    id: 'review',
    label: 'Review & approve',
    description: 'Open a linked PR, summarize evidence, and wait for code-owner approval.',
    kind: 'human',
    eyebrow: 'Human gate',
    x: 94,
    y: 43,
    sla: 'Owner policy',
    actor: 'Code owner',
    entry: 'Checks passed',
    exit: 'Approved or changes requested',
    fallback: 'Track review aging; never auto-merge',
    actions: ['PR template + issue link', 'Respond to scoped feedback', 'No automatic merge'],
  },
];

export const weeklyVolume = [46, 58, 52, 68, 74, 71, 86, 93, 88, 104, 112, 118];

export const outcomeData = [
  { label: 'Confirmed bug', value: 43, color: '#6558e8' },
  { label: 'Support / config', value: 31, color: '#21a581' },
  { label: 'Needs information', value: 28, color: '#ed9a43' },
  { label: 'Duplicate', value: 17, color: '#3d8dcc' },
  { label: 'Feature request', value: 11, color: '#a976d6' },
  { label: 'Inactive', value: 18, color: '#aeb6c4' },
];

export const ownerLoad = [
  { owner: 'Dashboard Experience', initials: 'DX', active: 18, waiting: 7, sla: 88 },
  { owner: 'Database Connectivity', initials: 'DB', active: 14, waiting: 5, sla: 92 },
  { owner: 'Core Platform', initials: 'CP', active: 12, waiting: 3, sla: 96 },
  { owner: 'SQL Lab', initials: 'SL', active: 9, waiting: 4, sla: 81 },
];

export const devinSessions: DevinSession[] = [
  {
    id: 'DEV-8472',
    issueId: 43207,
    issueKey: 'SUP-43207',
    issueTitle: 'Trino temporal column displays shifted timezone',
    title: 'Reproduce timezone shift',
    flowStep: 'Reproduce safely',
    actor: 'Devin reproducer',
    status: 'Running',
    started: '9 minutes ago',
    elapsed: '08:42',
    updated: '18s ago',
    progress: 64,
    budget: '60 min',
    environment: 'superset-repro-43207',
    trigger: 'Context completeness reached 88%',
    currentAction: 'Comparing Trino timestamp fixtures across target and control builds',
    nextCheckpoint: 'Attach minimal fixture and result matrix',
    events: [
      { label: 'Session started', detail: 'Clean workspace created from the approved reproduction image.', time: '08:42', state: 'complete' },
      { label: 'Fixture built', detail: 'Created a two-row temporal dataset without reporter data.', time: '06:18', state: 'complete' },
      { label: 'Target run', detail: 'Timezone shift reproduced twice on Superset 6.1.0.', time: '02:11', state: 'complete' },
      { label: 'Control run', detail: 'Testing the same fixture on current master.', time: 'Now', state: 'active' },
      { label: 'Evidence packet', detail: 'Publish result matrix or request one discriminator.', time: 'Next', state: 'pending' },
    ],
    artifacts: ['Reproduction plan', 'Fixture manifest', 'Target run log'],
  },
  {
    id: 'DEV-8469',
    issueId: 42991,
    issueKey: 'SUP-42991',
    issueTitle: 'CSV export ignores configured row limit',
    title: 'Await row-limit PR review',
    flowStep: 'Review & approve',
    actor: 'Devin PR handoff',
    status: 'Waiting on owner',
    started: '2 hours ago',
    elapsed: '21:16',
    updated: '16m ago',
    progress: 100,
    budget: '90 min',
    environment: 'Workspace released',
    trigger: 'Core Platform confirmed expected behavior',
    currentAction: 'No agent is running; draft PR #43302 is waiting for Core Platform review',
    nextCheckpoint: 'Owner approves the PR or requests scoped changes',
    branch: 'devin/42991-csv-row-limit',
    events: [
      { label: 'Fix authorized', detail: 'Owner approved the bounded coding contract.', time: '21:16', state: 'complete' },
      { label: 'Regression test', detail: 'Added a test that fails when the configured limit is ignored.', time: '15:04', state: 'complete' },
      { label: 'Scoped implementation', detail: 'Applied the limit at the export query boundary.', time: '08:27', state: 'complete' },
      { label: 'Affected checks', detail: 'Unit tests and type checks passed.', time: '02:08', state: 'complete' },
      { label: 'Draft PR opened', detail: 'Linked PR #43302 to the issue and released the workspace.', time: '00:00', state: 'complete' },
      { label: 'Owner review', detail: 'No merge or issue closure can happen automatically.', time: 'Waiting', state: 'active' },
    ],
    artifacts: ['Regression test', 'PR diff', 'Test output'],
  },
  {
    id: 'DEV-8475',
    issueId: 43218,
    issueKey: 'SUP-43218',
    issueTitle: 'Dashboard filters reset after force refresh',
    title: 'Classify new reporter evidence',
    flowStep: 'Classify outcome',
    actor: 'Devin triage agent',
    status: 'Running',
    started: '3 minutes ago',
    elapsed: '03:12',
    updated: '12s ago',
    progress: 38,
    budget: '10 min',
    environment: 'superset-triage-43218',
    trigger: 'Reporter submitted two requested details',
    currentAction: 'Checking the new feature-flag context against the remaining reproduction requirements',
    nextCheckpoint: 'Validate whether the report is reproduction-ready',
    events: [
      { label: 'Trigger accepted', detail: 'Reporter reply matched the active information request.', time: '01:04', state: 'complete' },
      { label: 'Idempotency check', detail: 'No duplicate triage session exists for this issue revision.', time: '00:58', state: 'complete' },
      { label: 'Evidence classification', detail: 'Comparing the response with the minimum reproduction contract.', time: 'Now', state: 'active' },
      { label: 'Outcome transition', detail: 'Queue reproduction or ask one focused follow-up.', time: 'Next', state: 'pending' },
    ],
    artifacts: ['Reporter response snapshot'],
  },
  {
    id: 'DEV-8476',
    issueId: 43104,
    issueKey: 'SUP-43104',
    issueTitle: 'Custom OAuth callback fails behind proxy',
    title: 'Recheck new proxy evidence',
    flowStep: 'Classify outcome',
    actor: 'Devin triage agent',
    status: 'Queued',
    started: 'Not started',
    elapsed: '00:00',
    updated: '1m ago',
    progress: 8,
    budget: '10 min',
    environment: 'Waiting for slot',
    trigger: 'Reporter added evidence after configuration guidance',
    currentAction: 'Queued within the configured workspace limit',
    nextCheckpoint: 'Decide whether the new evidence reopens bug triage',
    events: [
      { label: 'Trigger accepted', detail: 'The new comment contains evidence not present in the prior outcome.', time: '01:04', state: 'complete' },
      { label: 'Idempotency check', detail: 'No session exists for this issue revision.', time: '00:58', state: 'complete' },
      { label: 'Workspace slot', detail: 'Waiting within the configured concurrency limit.', time: 'Now', state: 'active' },
      { label: 'Evidence classification', detail: 'Start a bounded triage session.', time: 'Next', state: 'pending' },
    ],
    artifacts: ['New evidence snapshot', 'Prior classification'],
  },
  {
    id: 'DEV-8458',
    issueId: 43231,
    issueKey: 'SUP-43231',
    issueTitle: 'Bulk tag removal returns a 500 response',
    title: 'Reproduce bulk tag failure',
    flowStep: 'Reproduce safely',
    actor: 'Devin reproducer',
    status: 'Needs attention',
    started: '34 minutes ago',
    elapsed: '34:09',
    updated: '6m ago',
    progress: 46,
    budget: '60 min',
    environment: 'superset-repro-43231',
    trigger: 'Owner requested a database-backed confirmation run',
    currentAction: 'Blocked: fixture migration cannot reach the expected test database',
    nextCheckpoint: 'Operator chooses retry, alternate image, or human handoff',
    events: [
      { label: 'Session started', detail: 'Clean workspace created from the standard image.', time: '34:09', state: 'complete' },
      { label: 'API path isolated', detail: 'Minimal bulk tag request prepared.', time: '25:44', state: 'complete' },
      { label: 'Environment check', detail: 'Test database readiness failed after the bounded retry policy.', time: '06:03', state: 'blocked' },
      { label: 'Operator decision', detail: 'No further compute runs until a recovery path is selected.', time: 'Now', state: 'pending' },
    ],
    artifacts: ['Request fixture', 'Environment diagnostic'],
  },
  {
    id: 'DEV-8431',
    issueId: 43231,
    issueKey: 'SUP-43231',
    issueTitle: 'Bulk tag removal returns a 500 response',
    title: 'Build owner evidence packet',
    flowStep: 'Confirm the bug',
    actor: 'Devin evidence agent',
    status: 'Completed',
    started: '2 hours ago',
    elapsed: '12:07',
    updated: '2h ago',
    progress: 100,
    budget: '20 min',
    environment: 'Workspace released',
    trigger: 'Reproduction completed with 3 matching failures',
    currentAction: 'Completed: evidence packet attached to the owner decision',
    nextCheckpoint: 'No agent action unless the owner requests another discriminator',
    events: [
      { label: 'Evidence normalized', detail: 'Separated observed behavior from expected behavior.', time: '12:07', state: 'complete' },
      { label: 'Packet published', detail: 'Attached result matrix, minimal request, and regression window.', time: '00:18', state: 'complete' },
      { label: 'Workspace released', detail: 'Session ended before the human decision began.', time: '00:00', state: 'complete' },
    ],
    artifacts: ['Evidence packet', 'Result matrix', 'Owner decision request'],
  },
  {
    id: 'DEV-8422',
    issueId: 43104,
    issueKey: 'SUP-43104',
    issueTitle: 'Custom OAuth callback fails behind proxy',
    title: 'Classify proxy callback report',
    flowStep: 'Classify outcome',
    actor: 'Devin triage agent',
    status: 'Completed',
    started: '3 hours ago',
    elapsed: '07:33',
    updated: '3h ago',
    progress: 100,
    budget: '10 min',
    environment: 'Workspace released',
    trigger: 'Maintainer enrolled issue for triage',
    currentAction: 'Completed: configuration guidance delivered with a reopen path',
    nextCheckpoint: 'None unless the reporter adds contradictory evidence',
    events: [
      { label: 'Policy checked', detail: 'Compared the report with supported reverse-proxy configuration.', time: '07:33', state: 'complete' },
      { label: 'Outcome classified', detail: 'Evidence supported a configuration outcome, not a product defect.', time: '03:02', state: 'complete' },
      { label: 'Guidance published', detail: 'Linked canonical setup guidance and preserved the reopen path.', time: '00:14', state: 'complete' },
      { label: 'Workspace released', detail: 'No coding or reproduction session was started.', time: '00:00', state: 'complete' },
    ],
    artifacts: ['Classification evidence', 'Reporter guidance'],
  },
];
