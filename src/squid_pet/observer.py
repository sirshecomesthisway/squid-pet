"""
observer.py -- speech-bubble reaction layer for Squid.

Architecture: the Observer is a passive comment layer that:
  1. Watches state transitions reported by the StateMachine
  2. Watches direct user interactions reported by PetApi
  3. Returns short reaction strings (<= 32 chars) for the frontend bubble

It NEVER modifies pet state, NEVER intercepts the coding agent, and NEVER
produces multi-line output. The voice lives entirely in the BUBBLE_LINES
dict below -- editing that dict is the canonical way to evolve Squid's
personality.

Reference: openspec/specs/observer-mode/spec.md
"""
from __future__ import annotations

import re
import random
import logging
from typing import Callable, Optional, Union

log = logging.getLogger(__name__)

# Hard cap on bubble length. Anything longer would wrap to 2+ lines at the
# default sprite width (~200px @ 14px font). Enforced defensively in
# _pick(): out-of-spec lines return None and log a warning.
MAX_BUBBLE_CHARS = 32

# How often an idle chatter beat becomes an idea prompt instead of
# ordinary filler. 1-in-5: idle chatter fires every 26-34s and she goes
# drowsy after a few minutes, so this lands roughly once or twice per
# awake-idle stretch -- present without hounding you.
IDEA_PROMPT_CHANCE = 0.2

# ----------------------------------------------------------------------
# BUBBLE_LINES -- the voice contract. Pink owns this dict.
# ----------------------------------------------------------------------
# Sonic signatures per state (diversified after voice review on 2026-06-13):
#   thinking     = m/h closed-mouth pondering
#   working      = percussive activity
#   grooving     = p/s sneaky discovery (a subagent appeared!)
#   celebrating  = vowel-loud joy
#   concerned    = clipped distress
#   back_to_idle = breath of relief
#   waking       = guttural fog-clearing
#   approval_needed = short attention-grab (Squid waving her flag)
#
# Interaction signatures:
#   poke / like / sprint / sprint_end / drowsy
#
# Registered but unwired (kept for future, no trigger emits them in v1):
#   like     -- heart animation already says "loved"
#   sleeping -- bubble would interrupt the calm; let sprite + Zz do the work
# ----------------------------------------------------------------------
LineSpec = Union[str, list[str]]
BUBBLE_LINES: dict[str, LineSpec] = {
    # state transitions
    "thinking":     ["hmm", "mmm...", "hrm", "thinky"],
    "working":      ["tap tap", "*types*", "mm-hm", "work work"],
    "grooving":     ["psst!", "who?", "*peeks*", "eee?"],
    "celebrating":  ["yay!!", "woo!", "!!", "*wiggles*"],
    "concerned":    ["eep", "hmmnn", "urk", "!?"],
    "back_to_idle": ["pheww", "hhh", "*flops*", "*sigh*"],
    "waking":       ["mmf...", "nhg", "wh-", "*stretches*"],

    # The agent is waving a flag -- awaiting Pink's input (Pink 2026-07-02)
    "approval_needed": ["your turn!", "psst!!", "yoo??", "input?",
                        "heyy!!", "you? you!", "peek", "hi hi!"],

    # interactions
    "poke":         ["boop?", "hi", "?", "hm?"],
    "shake":        ["wheee", "whoa!", "whee~", "hey hey"],
    "sprint":       ["wheee!", "zoom", "*blurs*", "go!"],
    "sprint_end":   ["*pant pant*", "phew", "x_x"],
    "drowsy":       ["*yawn*", "sleepy...", "mmh"],

    # Pink-2026-08-31: "like" is now wired -- fired by
    # PetApi.acknowledge_approval() when a dblclick/heart lands while
    # she's waving the approval_needed flag ("I saw you, calming
    # down"). "sleeping" stays registered but unwired in v1.
    "like":         ["~", "gotcha!", "okay okay!", "noted~", "seen ya"],
    "sleeping":     ["zzz...", "*snore*"],

    # Idle chatter (2026-08-18) -- fired occasionally by RoutineController
    # during "rest" beats of the idle cycle, NOT on a state transition.
    # Voice: same dry/fond/fragmentary rules as everything else, but
    # there's genuinely nothing happening, so these are small-talk /
    # boredom beats rather than reactions to anything.
    # Pink-2026-08-27k: expanded with funnier lines ("too quiet" report) --
    # same voice, leaning harder into dry/absurdist octopus-desk-pet humor.
    "idle_chatter": ["hmm", "*stares*", "waiting~", "bored?", "tick... tock",
                     "*tentacle wiggle*", "anything?", "still here",
                     "la la la", "*doodles*", "hm hm hm", "nothing yet",
                     "*people-watching*", "quiet today",
                     "*counts pixels*", "ink's dry", "8 arms, 0 tasks",
                     "send help. or snacks", "procrastinating hard",
                     "*polishes suckers*", "*this is fine*",
                     "not stuck. resting", "*naps standing up*",
                     "could use a snack",
                     # Pink-2026-08-31: more of the same idle/bored voice.
                     "*idly floats*", "eight arms, zero plans",
                     "just vibing here", "is this a screensaver?",
                     "*yawns, mostly*", "watching the cursor blink",
                     "*pretends to work*", "so... anything?",
                     "ink levels: fine", "*floats sideways*",
                     "clock is not moving", "*stares at nothing*",
                     "still floating", "low tide today"],

    # Pink-2026-09-01: idle lines that invite the next project rather than
    # just filling silence. Deliberately a SEPARATE pool, not more
    # idle_chatter entries: chatter fires every 26-34s, and a squid asking
    # "what should we build?" at that rate stops reading as company and
    # starts reading as nagging. Blended in at IDEA_PROMPT_CHANCE by
    # on_idle_chatter so it stays an occasional nudge.
    #
    # Same register as the rest of her voice -- lowercase, short, dry,
    # never exclaiming. Half of them offer an idea ("i got an idea") and
    # half ask for one; she should feel like a collaborator with her own
    # half-formed thoughts, not a prompt box.
    "idle_idea_prompt": ["i got an idea", "ok, i got an idea",
                         "*has an idea* ...maybe",
                         "got anything to build?",
                         "what are we making?",
                         "wanna build something?",
                         "any half-baked ideas?",
                         "pitch me something",
                         "*taps a tentacle* ideas?",
                         "i've been thinking...",
                         "got a project in mind?",
                         "something worth making?",
                         "ideas? arms are free",
                         "*brainstorming, allegedly*",
                         "new thing today?"],

    # "Still working" reannounce fallback (2026-08-27k) -- fired by
    # _maybe_reannounce_working when there's no concrete shell command to
    # report (Edit/Write-only tool calls, or plain generation with no
    # tool call in flight). Same percussive "working" sonic signature,
    # just more variety than the 4-line on-entry set. Mixed at pick time
    # with working_wrapup below -- see on_still_working.
    "working_generic": ["writing code", "typing away", "*click clack*",
                        "on it", "building things", "*focused*",
                        "assembling parts", "mid-thought", "*tap tap tap*",
                        "cooking something"],

    # Occasional "sounds like she's wrapping up" flavor, mixed into the
    # SAME reannounce pool as working_generic above rather than gated on
    # any real "is this the last step" detection -- there's no such
    # signal available (no transcript content is ever read, by design).
    # Purely a minority flavor for variety, not a claim of fact.
    "working_wrapup": ["wrapping up", "tying loose ends",
                       "writing the summary", "almost there",
                       "polishing it", "final touches",
                       "closing things out", "buttoning up"],

    # Pink-2026-08-31: octopus-personality flavor for the same
    # working_generic/working_wrapup fallback pool (see on_still_working)
    # -- added after a report that the "working" reannounce beat could
    # get stepped on by idle_chatter's "8 arms, 0 tasks" and friends,
    # which read as bored/idle while she's actually mid-task. These are
    # deliberately the busy inverse of that idle voice (same dry/fond
    # octopus-desk-pet humor, but leaning into "swamped", not "bored").
    "working_squid": ["*eight arms, one task*", "*all arms busy*",
                      "ink's flowing", "no arms free rn",
                      "*tentacle traffic jam*", "grinding away",
                      "*ink-stained already*", "hands full. all 8",
                      "*deep in the ink*", "arms full, no complaints",
                      # Pink-2026-08-31: more of the same busy voice.
                      "*all suckers on deck*", "eight arms, zero rest",
                      "*ink flying*", "*multi-arm multitask*",
                      "swamped (happily)", "*typing with six arms*",
                      "arms everywhere, useful", "*full tentacle sprint*",
                      "busy is an understatement", "*ink trail behind me*",
                      "no time to float", "*eight-armed efficiency*"],
}

# ----------------------------------------------------------------------
# State transitions -> trigger keys
# ----------------------------------------------------------------------
# Only certain transitions fire a bubble. Steady state (same -> same) is a
# silent no-op. Some transitions are intentionally NOT wired (e.g. anything
# -> sleeping) because the bubble would interrupt the mood the sprite is
# trying to convey.
#
# Format: (new_state, optional_from_set) -> trigger_key
#   If from_set is None, ANY old_state -> new_state fires the key.
#   If from_set is a set/frozenset, only those old_states fire.
# ----------------------------------------------------------------------
STATE_TRIGGERS: list[tuple[str, Optional[frozenset[str]], str]] = [
    # The agent is waving a flag -- awaiting Pink's input (Pink 2026-07-02).
    # Fires on any transition INTO approval_needed. Rule-based lines
    # from BUBBLE_LINES["approval_needed"].
    ("approval_needed", None, "approval_needed"),

    # New thinking turn (covers idle -> thinking, but NOT working -> thinking
    # since working IS already a kind of thinking)
    ("thinking",    frozenset({"idle", "sleeping", "drowsy", "celebrating", "concerned"}), "thinking"),

    # Started using tools / shell
    ("working",     None,                                                                   "working"),

    # Subagent appeared (highest charm-per-LOC)
    ("grooving",    None,                                                                   "grooving"),

    # Task finished
    ("celebrating", None,                                                                   "celebrating"),

    # Error appeared
    ("concerned",   None,                                                                   "concerned"),

    # Error cleared -> relief (NOT idle -> idle, which is silent)
    ("idle",        frozenset({"concerned"}),                                               "back_to_idle"),
]


# ----------------------------------------------------------------------
# Concern-reason formatting
# ----------------------------------------------------------------------
# Pink-2026-08-22: the natural trigger for "concerned" (the legacy agent's
# detector reading its errors.log) was removed along with it -- no
# equivalent exists for Claude Code / Codex. "concerned" is presently only
# reachable via the ~/.squid-pet/force_state debug override, which never
# populates concern_reason, so this formatting always falls through to the
# generic concerned line below. Kept in case a future detector wires up
# concern_reason again.

_REASON_PREFIX_TRIM = (
    "anthropic.", "openai.", "google.", "pydantic_ai.", "httpx.",
    "TaskGroup", "ExceptionGroup",
)

def _format_concern_reason(reason: str) -> Optional[str]:
    """Turn a raw error-log reason into a bubble-friendly string.

    Returns None if the reason is empty or unsalvageable. Truncates to
    MAX_BUBBLE_CHARS - 1 (leaving room for trailing ellipsis if cut).
    """
    if not reason:
        return None
    r = reason.strip()
    # Strip noisy module prefixes
    for prefix in _REASON_PREFIX_TRIM:
        if r.startswith(prefix):
            r = r[len(prefix):].lstrip(":. ")
            break
    # Common cleanups
    if r.lower().startswith("error: "):
        r = r[7:]
    if r.lower().startswith("exception: "):
        r = r[11:]
    # Lowercase for pet vibes (errors shouldn't SHOUT at you)
    r = r.lower()
    # Truncate
    if len(r) > MAX_BUBBLE_CHARS:
        r = r[:MAX_BUBBLE_CHARS - 3].rstrip() + "..."
    return r or None


# ----------------------------------------------------------------------
# Celebrate-reason formatting -- "why is she celebrating?"
# ----------------------------------------------------------------------
# Pink-2026-08-27: celebrating used to ALWAYS show a generic exclamation
# ("yay!!"/"woo!"/...) with zero indication of what actually happened --
# a real user complaint ("squid is celebrating but i don't know why").
# watcher.py's celebrating branch sets a specific state_reason naming
# which detector's signal fired; this maps that to a clear, deterministic
# bubble instead of a random mood-only pick. Unlike _format_concern_reason,
# this is NOT probabilistic -- "why" deserves a consistent answer, not
# personality-driven variety.
#
# Pink-2026-08-30: "claude celebrating" briefly removed from this table
# (Stop fires every turn, not just "the task is done", so it was moved
# entirely to GROOVING -- Pink report: celebrating mid-task on routine
# turns). A same-day settle-window promotion attempt (GROOVING ->
# CELEBRATING after N seconds of silence) turned out to have the same
# bug just delayed -- ordinary reply latency outlasts any reasonable
# window. Re-added for good once watcher.py grew a real way to tell them
# apart: claude_task_complete, an explicit marker Claude itself writes
# (scripts/squid_task_complete.py) only when the whole task is done, no
# timer involved -- see StateMachine._compute_inner branches 2/3.
#
# codex's celebrate signal is UNCHANGED (CodexDetector.is_celebrating()
# is still a hardcoded False, "no reliable signal yet" -- Codex has no
# known equivalent hook, so this branch stays unreachable/aspirational
# for now) -- keep it noncommittal so a future real signal doesn't
# inherit an overclaiming default. GitDetector's celebrate is tied to an
# actual HEAD mtime change -- a real commit happened -- so it's already
# safe to state as fact.
_CELEBRATE_REASON_BUBBLES = {
    # Pink-2026-08-30: re-added -- claude celebrating is real again now
    # that watcher.py distinguishes it from routine turn completion via
    # an explicit marker (see claude_task_complete in
    # StateMachine._compute_inner), not a timer.
    "claude celebrating": "finished with claude!",
    "codex celebrating": "ooh, codex!",
    "git celebrating": "nice, fresh commit!",
}


def _format_celebrate_reason(state_reason: str) -> Optional[str]:
    """Deterministic "why" bubble for a specific celebrate source.
    Returns None for the generic/unspecific "celebrating" reason (e.g.
    force_state debug override) -- falls back to the mood-pick line."""
    return _CELEBRATE_REASON_BUBBLES.get((state_reason or "").strip().lower())


# ----------------------------------------------------------------------
# Reason explanations -- "why did she just do that?"
# ----------------------------------------------------------------------
# Pink-2026-09-01: "she went from idle to a very brief working state, I
# guess because I opened /model, but I'm not sure -- better to explain
# why." The watcher already computes a state_reason for every state (it is
# what `squid why` prints); it just never reached the bubble in a form
# anyone would want to read.
#
# There WAS a path for this ("Fix C", 2026-06-28): a 50% chance to use
# state_reason verbatim if it started with one of
# ("shell ", "subagent", "streaming", "error:", "writing", "post-busy").
# Two things were wrong with it. Those prefixes no longer match what the
# watcher emits -- "file write detected (claude_code)", "claude streaming"
# and "claude turn in flight" all miss -- so the path was mostly dead. And
# where it did hit, it published raw internals: "shell child active
# (claude_c..." truncated at MAX_BUBBLE_CHARS. A reason is a log line; an
# explanation is a sentence for the person watching.
#
# So: a real map, always applied (no coin flip -- if she moved, you get to
# know why), phrased in her voice and short enough to survive the cap.
_REASON_EXPLAIN = {
    "shell child active (claude_code)": "claude ran a command",
    "shell child active (codex)":       "codex ran a command",
    "shell child active":               "something ran a command",
    "file write detected (claude_code)": "claude edited a file",
    "file write detected (codex)":       "codex edited a file",
    "claude streaming":       "claude's thinking",
    "codex streaming":        "codex's thinking",
    "claude turn in flight":  "claude's mid-turn",
    "claude recapping":       "claude's recapping",
    "claude grooving":        "claude finished a turn",
    "creative burst":         "something wrapped up",
    "claude celebrating":     "claude finished the task",
    "codex celebrating":      "codex finished",
    "git celebrating":        "fresh commit landed",
    "no signals":             "nothing running",
    "non-agent detector busy": "something's busy",
    # Deliberately NOT mapped: bare "celebrating" (the force_state debug
    # override's unspecific reason). "something good happened" explains
    # nothing, and an invented non-explanation is worse than letting her
    # personality answer -- see
    # test_celebrating_falls_back_to_generic_mood_pick_when_unspecific,
    # which caught exactly that when this entry existed.
}

# Reasons the watcher builds with a variable tail, matched by prefix.
_REASON_EXPLAIN_PREFIX = (
    ("working hold",        "still on it"),
    ("idle ",               "nothing going on"),
    ("force_state override", "pinned by hand"),
    ("awaiting_input",      "claude needs you"),
)


def _explain_reason(state_reason: str,
                    approval_label: Optional[str] = None) -> Optional[str]:
    """Turn a watcher state_reason into a bubble that says why she moved.

    Returns None for anything unmapped, so the caller falls back to a
    personality line rather than leaking a new internal string into the
    UI the day someone adds one.
    """
    if not state_reason:
        return None
    r = state_reason.strip()
    # Pink-2026-09-01: with several sessions waiting, "claude needs you"
    # does not say WHOSE turn it is. The caller resolves the project names
    # (watcher.describe_waiting_sessions) and passes the phrase in; a bare
    # count still beats a uuid.
    if approval_label and r.lower().startswith("awaiting_input"):
        return approval_label[:MAX_BUBBLE_CHARS]
    exact = _REASON_EXPLAIN.get(r)
    if exact is not None:
        return exact
    low = r.lower()
    for prefix, text in _REASON_EXPLAIN_PREFIX:
        if low.startswith(prefix.lower()):
            return text
    return None


# ----------------------------------------------------------------------
# Shell-child detection -- "runs pytest in tests/" / "runs git push"
# ----------------------------------------------------------------------
# When the watcher reports state="working" on the strength of a live shell
# child, window.py passes that child's cmdline here (via
# _current_shell_cmdline -> watcher.shell_child_activity) so we can name
# what's actually running instead of the generic "claude ran a command".
#
# The cmdline arrives in one of two shapes:
#
#   * a bare tool invocation, e.g. ["pytest", "tests/", "-v"] -- caught when
#     the real tool process is alive at scan time (the grandchild case).
#   * Claude Code's Bash-tool WRAPPER, e.g.
#       ["/bin/zsh", "-c",
#        "source <snapshot> ... || true && setopt ... || true && "
#        "eval 'pytest tests/ -v' < /dev/null && pwd -P >| /tmp/..."]
#     The wrapper is a direct child of `claude` and lives for the whole
#     command, so watcher.shell_child_activity now returns IT when the
#     grandchild isn't caught (which is most of the time -- Bash calls are
#     short). The real command is inside `eval '<CMD>' < /dev/null`.
#
# _unwrap_eval_payload normalizes both shapes to a command string;
# _parse_command turns that into (verb[, path-like target]); _shell_cmd_bubble
# renders "runs {what}[ in {where}]" under the 32-char cap. The agent name is
# deliberately dropped (it is almost always "claude" and the sprite implies
# it) -- that is what buys the budget for a target. Examples:
#   pytest tests/test_observer.py -v  ->  "runs pytest in test_observer.py"
#   git push origin main              ->  "runs git push"
#   cd src && ruff check foo/bar.py   ->  "runs ruff in bar.py"
#   brew install ripgrep              ->  "runs brew install"  (pkg is not a path)
# ----------------------------------------------------------------------

# Two-word commands where the subcommand matters for the bubble
_TWO_WORD_TOOLS = {"git", "brew", "uv", "pip", "npm", "yarn", "pnpm", "docker",
                   "kubectl", "gcloud", "aws", "az", "gh", "go", "cargo"}

# Leading tokens that set up the environment rather than being the command
# worth reporting -- skipped so `cd src && ruff ...` reports `ruff`, not `cd`.
_NOISE_LEADERS = frozenset({"cd", "pushd", "export", "source", ".", "set",
                            "setopt", "unset", "unalias", "builtin", "eval"})

# Flags whose VALUE is free text (a message), never a location -- so
# `git commit -m "fix bar/baz"` never puts the message in the target slot.
_MSG_FLAGS = frozenset({"-m", "--message", "-c", "-C"})

_WRAPPER_SHELLS = ("sh", "bash", "zsh", "fish")
# Claude Code wraps the real command in `eval '<CMD>' < /dev/null`.
_EVAL_RE = re.compile(r"eval '(.*)' < /dev/null", re.DOTALL)
_EVAL_RE_LOOSE = re.compile(r"eval '(.*)'", re.DOTALL)
# Shell separators we split a payload on to find the first real command.
_SEG_SEP = re.compile(r"&&|\|\||\||;|\n")
# A token that names a location: has a path separator or a file extension.
_EXT_RE = re.compile(r"\.\w{1,5}$")
# A shell redirect token (`>`, `>>`, `<`, `2>`, `>/dev/null`, `2>&1`, ...).
# Its target is NOT a command location -- `>/dev/null` must never become a
# "where" just because it contains a slash.
_REDIR_RE = re.compile(r"^\d*[<>]")
# A redirect that is ONLY the operator, so its target is the NEXT token
# (`git log > out.txt`), as opposed to a glued/self-contained form
# (`>/dev/null`, `2>&1`).
_REDIR_OP_ONLY_RE = re.compile(r"^\d*(>>|>|<)$")


def _unwrap_eval_payload(cmdline: list[str]) -> Optional[str]:
    """Normalize a shell cmdline to the command string to report.

    For a `*sh -c` wrapper, return the `eval '<CMD>'` payload if present
    (Claude Code's Bash-tool shape), else the raw `-c` script (a plain
    `sh -c "cmd"` call). Decodes `\\012` -> newline and `'\\''` -> `'`.
    For a non-wrapper cmdline, the args ARE the command; join them.
    Returns None for an empty cmdline or a wrapper with an empty script.
    """
    if not cmdline:
        return None
    head = cmdline[0].rsplit("/", 1)[-1]
    if head in _WRAPPER_SHELLS and len(cmdline) >= 3 and cmdline[1] == "-c":
        script = cmdline[2]
        if not script:
            return None
        m = _EVAL_RE.search(script) or _EVAL_RE_LOOSE.search(script)
        payload = m.group(1) if m else script
        return payload.replace("\\012", "\n").replace("'\\''", "'")
    return " ".join(cmdline)


def _looks_like_path(tok: str) -> bool:
    """A cheap, tool-agnostic proxy for 'this token names a location':
    it has a path separator or ends in a file extension. Keeps commit
    messages, grep patterns and package names out of the target slot."""
    return "/" in tok or bool(_EXT_RE.search(tok))


def _parse_command(payload: str) -> tuple[Optional[str], Optional[str]]:
    """Turn a command string into (what, where).

    `what` is the verb (basename), plus the subcommand for a two-word tool.
    `where` is the first path-like argument, or None. Leading noise-leader
    segments (`cd`, `export`, ...) are skipped so the first REAL command is
    reported. Flags and message-flag values are never taken as `where`.
    """
    for seg in _SEG_SEP.split(payload):
        toks = seg.strip().split()
        if not toks:
            continue
        # command = first non-flag token in this segment
        ci = next((i for i, t in enumerate(toks) if not t.startswith("-")), None)
        if ci is None:
            continue  # all flags -- not a command segment
        if toks[ci].startswith("#"):
            continue  # a comment line, not a command
        cmd = toks[ci].rsplit("/", 1)[-1]
        if cmd in _NOISE_LEADERS:
            continue  # skip `cd src`, `export X=y`, ... keep looking
        rest = toks[ci + 1:]
        what = cmd
        if cmd in _TWO_WORD_TOOLS:
            sub = next((a for a in rest if not a.startswith("-")), None)
            if sub:
                what = f"{cmd} {sub}"
                rest = rest[rest.index(sub) + 1:]
        where = None
        skip_next = False
        for a in rest:
            if skip_next:
                skip_next = False
                continue
            if a in _MSG_FLAGS:
                skip_next = True  # its value is a message, not a location
                continue
            if _REDIR_RE.match(a):
                # a redirect (and its target, if separated) -- not a location
                if _REDIR_OP_ONLY_RE.match(a):
                    skip_next = True
                continue
            if a.startswith("-"):
                continue
            if _looks_like_path(a):
                where = a
                break
        return what, where
    return None, None


def _shell_cmd_bubble(cmdline: list[str]) -> Optional[str]:
    """Format a shell child's cmdline into a 'runs {what}[ in {where}]'
    bubble (agent name dropped), within the 32-char cap.

    Returns None -- so the caller falls back to the generic reason line --
    for an empty cmdline, a wrapper with no recoverable command, or a
    payload whose only command is a noise leader. This keeps a future
    change to Claude Code's wrapper preamble from ever leaking `source`,
    `eval`, or a snapshot path into a bubble.
    """
    payload = _unwrap_eval_payload(cmdline)
    if not payload:
        return None
    what, where = _parse_command(payload)
    if not what:
        return None
    base = f"runs {what}"
    if len(base) > MAX_BUBBLE_CHARS:
        return None  # pathological; let the generic line handle it
    if not where:
        return base
    # Fit the target under the cap: full path -> basename -> parent dir -> drop.
    candidates = [where]
    basename = where.rstrip("/").rsplit("/", 1)[-1]
    if basename and basename != where:
        candidates.append(basename)
    parent = where.rstrip("/").rsplit("/", 1)
    if len(parent) == 2 and parent[0]:
        candidates.append(parent[0] + "/")
    for w in candidates:
        s = f"{base} in {w}"
        if len(s) <= MAX_BUBBLE_CHARS:
            return s
    return base


# ----------------------------------------------------------------------
# Observer class
# ----------------------------------------------------------------------
class Observer:
    """Generates speech-bubble reactions for state changes + interactions.

    Stateless except for the mute flag (queried via callback so config
    changes are picked up live without restart).
    """

    def __init__(self, get_muted: Callable[[], bool]):
        """get_muted: callback returning current mute state (queried per call).

        Pink-2026-09-04: this used to also take an LLM client, a publish
        callback and an enable-getter, for a background enrichment pass that
        could overwrite a rule-based bubble with a model-written one. That
        whole layer is gone along with its backend; every
        bubble is rule-based now, decided synchronously right here.
        """
        self._get_muted = get_muted

    # ------------------------------------------------------------------
    # Internal: random pick + length guard
    # ------------------------------------------------------------------
    def _pick(self, key: str) -> Optional[str]:
        """Pick a line for the given key. Returns None if key unknown,
        mute is on, or every candidate exceeds MAX_BUBBLE_CHARS."""
        if self._get_muted():
            return None
        spec = BUBBLE_LINES.get(key)
        if spec is None:
            return None
        choices = [spec] if isinstance(spec, str) else list(spec)
        # Filter out oversized entries defensively
        valid = [c for c in choices if len(c) <= MAX_BUBBLE_CHARS]
        if not valid:
            if choices:
                log.warning(
                    "observer: every line for key=%r exceeds %d chars; "
                    "no bubble will fire. Edit BUBBLE_LINES.",
                    key, MAX_BUBBLE_CHARS,
                )
            return None
        if len(valid) < len(choices):
            log.warning(
                "observer: %d/%d lines for key=%r exceed %d chars",
                len(choices) - len(valid), len(choices), key, MAX_BUBBLE_CHARS,
            )
        return random.choice(valid)

    # ------------------------------------------------------------------
    # State-change trigger
    # ------------------------------------------------------------------
    def on_state_change(
        self,
        old: str,
        new: str,
        *,
        concern_reason: str = "",
        shell_cmdline: Optional[list[str]] = None,
        # Fix C (2026-06-28): state machine's "why" string. When non-empty
        # AND starts with an interesting prefix, 50% chance Squid uses it
        # verbatim as the bubble (the "mix mood + reason" path Pink chose).
        state_reason: str = "",
        # Pink-2026-09-01: pre-resolved "who is waiting" phrase; see
        # _explain_reason. Resolved by the caller because it needs disk and
        # process lookups, which this module deliberately never does.
        approval_label: Optional[str] = None,
    ) -> Optional[str]:
        """Called when the StateMachine reports a transition.

        - Returns None if old == new (silent no-op)
        - Returns None if mute is on
        - For 'concerned', prefers the concern_reason verbatim if non-empty
        - For 'working' shell-state, prefers the shell command name if known
        - Otherwise picks a generic line from BUBBLE_LINES
        """
        if old == new:
            return None
        if self._get_muted():
            return None

        # Pink-2026-08-30: PreCompact-triggered "recapping" needs to
        # announce itself regardless of what state Squid was in right
        # before -- including working -> thinking, which the "thinking"
        # trigger below deliberately suppresses everywhere else ("working
        # IS already a kind of thinking", no bubble needed). A compaction
        # is a genuinely different activity from routine reasoning, and
        # Pink specifically asked for it called out by name, so this
        # bypasses STATE_TRIGGERS's old-state filtering entirely. Not
        # random (see _format_celebrate_reason's rationale) -- Pink wants
        # a consistent, always-shown answer, not a personality coin flip.
        # Naturally rate-limited by the old==new check above: repeated
        # ticks while still recapping never re-fire this (old and new are
        # both "thinking" on every tick after the first).
        if new == "thinking" and state_reason == "claude recapping":
            return "📝 recapping..."

        # Find matching trigger
        trigger_key = None
        for new_state, from_set, key in STATE_TRIGGERS:
            if new == new_state and (from_set is None or old in from_set):
                trigger_key = key
                break
        if trigger_key is None:
            return None

        # Enriched bubbles -- concrete info beats generic emote
        if trigger_key == "concerned":
            specific = _format_concern_reason(concern_reason)
            if specific is not None:
                return specific
        elif trigger_key == "celebrating":
            specific = _format_celebrate_reason(state_reason)
            if specific is not None:
                return specific
        elif trigger_key == "working" and shell_cmdline:
            specific = _shell_cmd_bubble(shell_cmdline)
            if specific is not None and len(specific) <= MAX_BUBBLE_CHARS:
                return specific

        # Explain the move (Pink-2026-09-01). Replaces the old 50%
        # raw-state_reason path -- see _explain_reason for why that one was
        # both mostly dead and, when alive, published truncated internals.
        # Not a coin flip: if she changed state, you get to know why. Falls
        # through to a personality line only when the reason is unmapped,
        # so a newly-added internal string can never leak into a bubble.
        explained = _explain_reason(state_reason, approval_label)
        if explained is not None:
            return explained

        return self._pick(trigger_key)

    # ------------------------------------------------------------------
    # "Still working" refresh -- NOT a state transition
    # ------------------------------------------------------------------
    def on_still_working(self, shell_cmdline: Optional[list[str]]) -> Optional[str]:
        """Called periodically (by PetApi, throttled) while state STAYS
        'working' across ticks, to surface what she's currently watching
        even though on_state_change only fires once on entry.

        Prefers a concrete "running X" from a live shell child. Falls
        back to a generic working/wrap-up line (working_generic +
        working_wrapup, mixed) when there's no shell command to report --
        e.g. Edit/Write-only tool calls, or plain generation with no tool
        call in flight. Pink-2026-08-27k: this used to return None in the
        fallback case (repeating a canned line "would read as a glitch,
        not aliveness") -- but the caller (_maybe_reannounce_working)
        already dedupes against the last shown text and only fires on a
        throttle, so a varied generic pool reads as ambient presence
        instead, addressing a "too quiet" report.
        """
        if self._get_muted():
            return None
        if shell_cmdline:
            specific = _shell_cmd_bubble(shell_cmdline)
            if specific is not None:
                return specific
        pool = (BUBBLE_LINES["working_generic"] + BUBBLE_LINES["working_wrapup"]
                + BUBBLE_LINES["working_squid"])
        return random.choice(pool)

    # ------------------------------------------------------------------
    # Interaction trigger
    # ------------------------------------------------------------------
    def on_interaction(self, kind: str) -> Optional[str]:
        """Called when the user interacts with Squid (poke, sprint, etc.)."""
        return self._pick(kind)

    def on_idle_chatter(self) -> Optional[str]:
        """The ~26-34s ambient idle beat (RoutineController's chatter_cb).

        Mostly ordinary idle filler, but IDEA_PROMPT_CHANCE of the time she
        asks what you want to build -- or claims to have thought of
        something herself. Separate pool rather than more idle_chatter
        entries so the rate is a knob: the difference between a pet with
        her own ideas and one that pesters you is entirely frequency.
        """
        if random.random() < IDEA_PROMPT_CHANCE:
            return self._pick("idle_idea_prompt")
        return self._pick("idle_chatter")

    # ------------------------------------------------------------------
    # Mood trigger (frontend mood notifications: drowsy, sleeping, stretch)
    # ------------------------------------------------------------------
    def on_mood_change(self, old: str, new: str) -> Optional[str]:
        """Called when the JS mood layer changes (drowsy/sleeping/stretch).

        Only fires for the entry edge -- e.g. (-> drowsy) once, not on every
        tick of drowsiness. Sleeping is silenced (let the sprite speak).
        Stretch maps to 'waking' since that's the wake-transition.
        """
        if old == new:
            return None
        if new == "drowsy":
            return self._pick("drowsy")
        if new == "stretch":
            return self._pick("waking")
        # sleeping -> no bubble (interrupts the calm)
        return None
