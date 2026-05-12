---
allowed-tools: Bash, Write, Edit, Read
description: Scaffold a new system design exercise. Creates the topic directory, PROMPT.md, README.md, updates root README, and commits. Usage: /new-exercise <topic_name>
---

Create a new system design exercise for the topic: **$ARGUMENTS**

Follow every step below in order. Do not skip any step.

---

## Step 1 — Create the directory

```bash
mkdir -p $ARGUMENTS
```

---

## Step 2 — Write `$ARGUMENTS/PROMPT.md`

Generate the file content based on the topic. Follow the rules below exactly.

**PROMPT.md structure:**

```
# <Topic Name> Prototype

## System Requirements

Build a <topic> system where:
- <concrete requirement 1 — what the API accepts and returns>
- <concrete requirement 2>
- <concrete requirement 3>
- <concrete requirement 4>
- <concrete requirement 5>

## Design Questions

Answer these before you start coding:

1. **<Label>:** <Question that names two options and their trade-offs>

->

2. **<Label>:** <Question that names two options and their trade-offs>

->

3. **<Label>:** <Question that names two options and their trade-offs>

->

4. **<Label>:** <Question that names two options and their trade-offs>

->

5. **<Label>:** <Question that names two options and their trade-offs>

->

## Verification

Your prototype should pass all of these:

```bash
# <describe what this curl does>
curl ...
# → <expected response>

# <describe what this curl does>
curl ...
# → <expected response>

# <error case>
curl ...
# → <expected error code or body>
```

## Suggested Tech Stack

Python + FastAPI recommended, but any language/framework is fine.

---

## Later Phases (do not implement yet)

- <future concern 1>
- <future concern 2>
- <future concern 3>
- <future concern 4>
```

**Rules for generating content:**

- System requirements: describe what the API concretely does (inputs, outputs, side effects). Not abstract goals.
- Design questions: pick exactly 5 from the universal Phase-1 decision categories below, adapted to this specific topic. Each question must name the two sides of a trade-off. No question should be about scaling, performance, or anything deferred to later phases.
- Verification curls: use `localhost:8000`. Cover the happy path end-to-end plus at least two error cases. Make them runnable.
- Later phases: short bullet list only. No explanation. Things like caching, rate limiting, fan-out, replication, monitoring — whatever is NOT needed for the first working prototype.

**The 5 universal Phase-1 decision categories — adapt each to the topic:**

1. **Storage/state model** — What gets persisted? What does one row/document look like? What is the primary key?
2. **Sync vs async** — Does the API block until the slow operation finishes, or return immediately with a pending ID?
3. **Uniqueness / identity** — How are IDs or tokens generated? What happens on collision or duplicate input?
4. **Failure semantics** — What HTTP status codes and response bodies signal each failure mode? Are failures distinguishable from each other?
5. **Core abstraction** — What interface or data model makes the system easy to extend (e.g. adding a new channel, a new storage backend, a new action type)?

---

## Step 3 — Write `$ARGUMENTS/README.md`

Write this file with the topic name filled in:

```markdown
# <Topic Name> — Exercise

## How to Use

1. Read `PROMPT.md`
2. Answer the Design Questions — fill in your choices directly in `PROMPT.md`
3. Build the prototype:
   - **Challenge Track:** Build from scratch based on your design decisions
   - **Guided Track:** `scaffold/` will be provided once design is finalised
4. Verify with the curl tests at the bottom of `PROMPT.md`

## Key Design Decisions to Make First

Before writing any code, answer all five questions in `PROMPT.md`.
Each one directly affects your initial schema, API contract, or code structure.
```

---

## Step 4 — Update root `README.md`

Read the root `README.md` to find the exercises table (it has columns `#`, `Topic`, `Status`, `Key Concepts`).

Append a new row for this exercise. Determine the next sequential number from the existing rows.
For the Key Concepts column, list 3–5 short terms that are central to this topic's design.

New row format:
```
| N | [$ARGUMENTS](./$ARGUMENTS/) | 🔨 In Progress | concept1, concept2, concept3 |
```

---

## Step 5 — Git commit

```bash
cd /Users/tygrus/Desktop/projects/sd-practice && \
git add $ARGUMENTS/ README.md && \
git commit -m "feat($ARGUMENTS): add exercise scaffold"
```
