export type ViewKey = 'overview' | 'workflow' | 'issues' | 'settings';

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
