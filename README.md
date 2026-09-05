# Relay issue operations mockup

An interactive product mockup for a Devin-powered issue intake, reproduction,
and fix workflow. The interface includes:

- an operational analytics dashboard;
- a visual lifecycle designer;
- an issue workbench with reporter guidance, evidence, and owner gates;
- configurable triage, inactivity, safety, and agent-instruction policies.

All content and actions are simulated. The mockup does not connect to GitHub or
start Devin sessions.

## Research

- [Apache Superset issue intake and bug resolution report](public/reports/apache-superset/issue-intake-2025-09-05-to-2026-09-04.html)
  covers 1,030 public issues created during the 365 complete days ending
  September 4, 2026. It documents the measured GitHub data, classification
  proxies, lifecycle timing, backlog age, and automation opportunities that
  informed the mockup.

## Run with Docker

```bash
docker compose up --build
```

Open `http://localhost:4173`.

To use another host port:

```bash
APP_PORT=8080 docker compose up --build
```

## Local development

```bash
npm install
npm run dev
```

## Validation

```bash
npm run typecheck
npm run build
```
