# System Design Practice

A collection of hands-on system design exercises. Each topic is an independent git repo with its own infrastructure, scaffold, and notes.

## Exercises

| # | Topic | Status | Key Concepts |
|---|-------|--------|--------------|
| 1 | [qr_code_generator](./qr_code_generator/) | ✅ Complete | REST API, PostgreSQL, Nginx, Varnish CDN, HAProxy, Prometheus/Grafana |
| 2 | [notification_system](./notification_system/) | 🔨 In Progress | Async delivery, fan-out, queues, rate limiting, retry, user preferences |

## How Each Exercise Works

Every exercise follows the same structure:

```
<topic>/
├── PROMPT.md          # Design questions + system requirements + curl verification tests
├── README.md          # Setup instructions and track guide
├── docker-compose.yml # Full infrastructure (DB, cache, app, worker, monitoring)
└── scaffold/          # Guided track: fill in the TODOs
    ├── app/           # FastAPI application
    └── init.sql       # Database schema
```

### Two Tracks

**Challenge Track** — Read `PROMPT.md`, answer the design questions, then build from scratch.

**Guided Track** — Go to `scaffold/`, fill in the TODO-marked functions. The structure and boilerplate are already there.

## Directory Layout

```
sd-practice/
├── README.md                     ← You are here
├── qr_code_generator/            ← Exercise 1 (own git repo)
│   ├── PROMPT.md
│   ├── docker-compose.yml
│   ├── scaffold/
│   └── notes/                    ← Phase-by-phase experiment notes
└── notification_system/          ← Exercise 2 (own git repo)
    ├── PROMPT.md
    ├── docker-compose.yml
    └── scaffold/
```
