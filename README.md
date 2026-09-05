# Relay issue operations mockup

An interactive product mockup for a Devin-powered issue intake, reproduction,
and fix workflow. The interface includes:

- an operational analytics dashboard;
- a visual lifecycle designer;
- an issue workbench with reporter guidance, evidence, and owner gates;
- configurable triage, inactivity, safety, and agent-instruction policies.

All content and actions are simulated. The mockup does not connect to GitHub or
start Devin sessions.

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
