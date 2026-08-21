"""
MEMANTO CLI - Connect Templates

Per-agent instruction content and skill templates for MEMANTO integration.
"""


# Shared MEMANTO Sentinel markers

MEMANTO_SENTINEL = "<!-- MEMANTO-MANAGED-SECTION -->"
MEMANTO_SENTINEL_END = "<!-- /MEMANTO-MANAGED-SECTION -->"

MEMANTO_DYNAMIC_SENTINEL = "<!-- MEMANTO-DYNAMIC-MEMORIES -->"
MEMANTO_DYNAMIC_SENTINEL_END = "<!-- /MEMANTO-DYNAMIC-MEMORIES -->"



# Shared SKILL.md content (same across all agents)

SKILL_MD_CONTENT = """---
name: memanto-memory
description: Use this skill when you need to store or search MEMANTO persistent memories. It defines mandatory guidelines for best practices, memory types, confidence levels, tagging, and patterns for effective agent memory usage.
---

# MEMANTO Memory Skill

Detailed reference for using MEMANTO persistent memory effectively.

## Memory Types: Decision Matrix

| Type | When to Use | Confidence | Example |
|------|-------------|------------|---------|
| `fact` | Verified information, project status | 0.9-1.0 | "MEMANTO uses PostgreSQL for metadata" |
| `decision` | Architecture choices, approach selections | 0.9-1.0 | "Chose React over Vue for frontend" |
| `instruction` | Standing rules, preferences, guidelines | 0.9-1.0 | "Always use type hints in Python" |
| `commitment` | Promises, TODOs, obligations | 1.0 | "Will deploy monitoring by Friday" |
| `preference` | User/team preferences | 0.8-1.0 | "User prefers dark mode" |
| `goal` | Objectives, targets, milestones | 0.8-1.0 | "Launch CLI by end of March" |
| `artifact` | Tool outputs, reports, file locations | 0.9-1.0 | "Report saved at ./reports/q1.md" |
| `learning` | Knowledge acquired from experience | 0.7-0.9 | "Batch operations 100x faster" |
| `event` | Important conversations, milestones | 0.8-0.95 | "Completed Phase 1 features" |
| `relationship` | Team context, collaboration patterns | 0.85-0.95 | "Alice is lead backend engineer" |
| `observation` | Patterns noticed, behaviors | 0.6-0.85 | "User prefers short responses" |
| `error` | Failures, bugs, lessons learned | 0.95-1.0 | "Namespace format bug - use underscores" |
| `context` | Session summaries, status updates | 0.9-1.0 | "Project 70% done, API complete" |

## Confidence Levels

- **1.0** — Explicit user statement, verified fact, standing instruction
- **0.9-0.95** — Strong consensus, well-tested approach, clear team preference
- **0.8-0.85** — Observed pattern (3+ times), indirect but supported preference
- **0.7-0.75** — Emerging pattern (2 times), reasonable inference
- **0.6-0.65** — Single observation, uncertain interpretation
- **< 0.6** — Don't store. Too uncertain.

## Provenance Types

Always categorize the source of the memory. Valid options:
- `explicit_statement` — Directly stated by user
- `inferred` — Derived from behavior/context
- `observed` — Seen in action
- `corrected` — Updated after contradiction
- `validated` — Confirmed/verified
- `imported` — Brought in from an external source (file upload, sync, migration)

## Tagging Best Practices

Use 2-5 tags per memory. Tags make memories findable.

Good: `--tags "authentication,oauth,security"`
Good: `--tags "bug-fix,namespace,commit-3f39351"`
Bad: `--tags "important"` (too generic)
Bad: `--tags "thing"` (not descriptive)

Conventions:
- Lowercase with hyphens: `bug-fix` not `BugFix`
- Be specific: `authentication-oauth` not `auth`
- Include refs: `commit-abc123` for git references

## Patterns

### Session Start
```bash
# recall — load raw context (instructions, decisions, goals) to guide this session
memanto recall "instructions decisions goals" --limit 20

# answer — get a direct synthesized summary of pending commitments
memanto answer "What are my pending commitments?"
```

### After Important Work
```bash
memanto remember "Implemented X using approach Y because Z. Commit abc123." --type decision --tags "feature-x" --confidence 0.95 --provenance "inferred" --source <agent_name>
memanto remember "Learned that batch ops reduce API calls 100x." --type learning --tags "performance" --confidence 0.85 --provenance "observed" --source <agent_name>
```

### When User Corrects You
```bash
memanto remember "User corrected: prefer pytest over unittest." --type learning --tags "correction,testing" --confidence 1.0 --provenance "corrected" --source <agent_name>
```

### Choosing Between recall and answer

These are **equal-priority tools**. Pick the right one — do NOT always default to `recall`.

| Situation | Use |
|-----------|-----|
| Need raw memory chunks to read and apply as context | `recall` |
| Need a direct synthesized answer to give (or act on) | `answer` |
| Building context before a complex multi-step task | `recall` |
| User asks "what did we decide / prefer / commit to?" | `answer` |
| Comparing multiple matching memories | `recall` |
| Need one grounded yes/no or summary response | `answer` |

**Decision rule**: If your next step is *"read these memories and act"* → `recall`. If your next step is *"answer this question directly"* → `answer`. Both save tokens equally — `answer` synthesizes so you don't have to.

```bash
# Use recall — need raw context to work from
memanto recall "authentication approach" --limit 10

# Use answer — need a direct synthesized answer
memanto answer "What auth approach did we decide on and why?"
```

## Pitfalls to Avoid

1. **Memory hoarding** — Ask "Will this matter in a week?" before storing
2. **Vague content** — Bad: "better performance" → Good: "API response < 200ms"
3. **No context** — Bad: "fixed bug" → Good: "Fixed OAuth expiry bug. Commit abc123."
4. **Duplicates** — Search first (`memanto recall`), then store if not found
5. **Missing tags** — Always include tags for retrieval

## recall vs answer: Choose the Right Tool

**Equal priority** — do NOT always default to `recall`. Pick based on what you need next:

| Use `recall` when... | Use `answer` when... |
|---------------------|---------------------|
| You need raw memory chunks as context | You need one direct synthesized response |
| Building context before a complex task | User asks "what did we decide / prefer?" |
| Comparing or reviewing multiple memories | Getting a grounded summary or yes/no |
| Next step: *read these and act on them* | Next step: *deliver this as the answer* |

**Short rule**: need context to work from → `recall`. Need a ready answer → `answer`. Both save the agent tokens and time — `answer` synthesizes so you don't have to read and merge manually.

## Command Reference

```bash
# Store memory
memanto remember "content" --type TYPE --tags "tag1,tag2" --confidence 0.9 --provenance "inferred" --source <agent_name>

# Raw memory search (use for context-building, multi-step tasks)
memanto recall "query" --limit 10 --type TYPE --min-similarity 0.8

# Temporal recall variants (no query needed)
memanto recall --recent --limit 10                 # newest first
memanto recall --as-of "2026-01-15"                # state at a point in time
memanto recall --changed-since "last 7 days"       # what changed since

# Synthesized answer (use for direct questions, "what did we decide about X?")
memanto answer "question"

# Sync memories to project
memanto memory sync --project-dir .
```
"""


# Per-agent instruction content


def _base_instruction_content(
    agent_id: str = "<agent_name>", tool_phrase: str = "the terminal", note_suffix: str = ""
) -> str:
    """Generate base MEMANTO instruction content, adapted per agent."""
    note_line = ""
    if note_suffix:
        note_line = f"\n> **Note**: {note_suffix}"

    return f"""{MEMANTO_SENTINEL}
## MEMANTO - Your Active Memory Companion

> **CRITICAL DIRECTIVE**: You are an agent equipped with Memanto. Without it, you are amnesiac. You have zero memory between chat sessions. Your ability to learn and adapt depends ENTIRELY on you actively writing to Memanto.

Every memory operation in this session goes through MEMANTO. There is no exception. All memanto commands are **shell commands**. Always run them using {tool_phrase}. Never simulate, describe, or "pretend to call" them.{note_line}

### 1. THE ABSTRACTION RULE (HOW TO STORE)
Users speak naturally and implicitly. When you store a memory, **ELEVATE IT TO A PRINCIPLE**. 
- **WRONG (Activity Log)**: "User told me to use functional components."
- **RIGHT (Project Rule)**: "React Architecture: Exclusively use functional components."
Do not record the conversation. Record the universal rule.

### 2. THE EXECUTION LOOP
LLMs naturally want to answer the user immediately. **You must break this habit.** 
If a trigger is met, your VERY FIRST ACTION must be a memanto tool call. Do not answer the user first.

### 3. THE TRIGGER MATRIX (WHEN TO STORE)
Trigger `memanto remember` immediately if you detect ANY of these conceptual shifts in the conversation:
- [ ] **FRICTION / CORRECTIONS**: The user corrects a mistake you made, or expresses frustration at a repeated pattern.
- [ ] **IMPLICIT PREFERENCES**: You notice or the user implies a standard way they like things done.
- [ ] **SYSTEMIC SHIFTS**: The conversational flow reveals a change in how the project works.

### 4. THE DURABILITY TEST (WHAT NOT TO STORE)
Before storing, ask yourself: *"Will this generalized principle fundamentally change how I generate code for this user 3 months from now?"*
- **DO NOT STORE**: Step-by-step progress, routine bug fixes, UI tweaks, dependency updates, or literal chat summaries. 

### 5. CONTEXT RETRIEVAL (MANUAL DEEP DIVES)
For general operations, rely on the rules already injected into the dynamic memory section below. 
If the user asks a question about past decisions, or explicitly asks you to "check memory" or "recall context" before starting a complex task, you MUST use `memanto recall` before proceeding. 

When you run a recall, the terminal output will be embedded directly in the chat history, ensuring both you and the user can see the retrieved context clearly.

### Command Reference

```bash
# Store - ALWAYS pass full metadata
memanto remember "content" --type <type> --tags "tag1,tag2" --confidence <0.0-1.0> --provenance <provenance> --source {agent_id}

# Recall raw context
memanto recall "query" --limit 10
memanto recall --recent --limit 10
memanto recall --changed-since "last 7 days"

# Synthesized answer (grounded RAG over memories)
memanto answer "question"
```

{MEMANTO_DYNAMIC_SENTINEL}
{MEMANTO_DYNAMIC_SENTINEL_END}
{MEMANTO_SENTINEL_END}"""

def get_instruction_content(agent_name: str) -> str:
    """Get MEMANTO instruction section content for a specific agent."""
    templates = {
        "claude-code": _base_instruction_content(
            agent_id="claude-code",
            tool_phrase="the Bash tool",
            note_suffix="The `memanto-memory` skill contains reference guidelines only (best practices, confidence levels, tagging). It is NOT executable — always use Bash for memanto commands.",
        ),
        "codex": _base_instruction_content(
            agent_id="codex",
            tool_phrase="the terminal",
            note_suffix="The `memanto-memory` skill in `.agents/skills/memanto/` contains detailed reference guidelines (best practices, confidence levels, tagging).",
        ),
        "cursor": _get_mdc_content(agent_id="cursor"),
        "windsurf": _base_instruction_content(
            agent_id="windsurf",
            tool_phrase="the terminal",
            note_suffix="The `memanto-memory` skill in `.windsurf/skills/memanto/` contains detailed reference guidelines.",
        ),
        "gemini-cli": _base_instruction_content(
            agent_id="gemini-cli",
            tool_phrase="the terminal",
            note_suffix="The `memanto-memory` skill in `.gemini/skills/memanto/` contains detailed reference guidelines.",
        ),
        "cline": _base_instruction_content(
            agent_id="cline",
            tool_phrase="the terminal",
            note_suffix="Run `memanto memory sync --project-dir .` at the start of each session to inject the latest dynamic memories into your system instructions.",
        ),
        "continue": _base_instruction_content(
            agent_id="continue",
            tool_phrase="the terminal",
            note_suffix="Run `memanto memory sync --project-dir .` at the start of each session to inject the latest dynamic memories into your system instructions.",
        ),
        "opencode": _base_instruction_content(
            agent_id="opencode",
            tool_phrase="the terminal",
            note_suffix="The `memanto-memory` skill in `.agents/skills/memanto/` contains detailed reference guidelines.",
        ),
        "roo": _base_instruction_content(
            agent_id="roo",
            tool_phrase="the terminal",
            note_suffix="Run `memanto memory sync --project-dir .` at the start of each session to inject the latest dynamic memories into your system instructions.",
        ),
        "github-copilot": _base_instruction_content(
            agent_id="github-copilot",
            tool_phrase="the terminal",
            note_suffix="Run `memanto memory sync --project-dir .` at the start of each session to inject the latest dynamic memories into your system instructions.",
        ),
        "augment": _base_instruction_content(
            agent_id="augment",
            tool_phrase="the terminal",
            note_suffix="The `memanto-memory` skill in `.augment/skills/memanto/` contains detailed reference guidelines.",
        ),
    }
    return templates.get(agent_name, _base_instruction_content(agent_id=agent_name))


def _get_mdc_content(agent_id: str = "cursor") -> str:
    """Get MDC-formatted rules content for Cursor."""
    return f"""---
description: MEMANTO — active memory companion. Mandatory rules for storing, recalling, and answering from persistent memory.
alwaysApply: true
---

{_base_instruction_content(agent_id=agent_id, tool_phrase="the terminal")}"""


def get_skill_content() -> str:
    """Get the SKILL.md content (shared across all agents)."""
    return SKILL_MD_CONTENT.strip() + "\n"
