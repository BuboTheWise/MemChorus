"""
MemChorus lifecycle hooks for Hermes plugin integration.

This module provides the MemChorusHooks class that Hermes Gateway discovers via
setup.py entry_points ("hermes_agent.plugins" group) and calls at key moments
pre_llm_call, post_tool_call, on_session_start.

On import of memchorus package, global bootstrap fires if enabled.
These hooks wire into that bootstrap'd orchestrator instance to provide
automatic memory recall + behavioral prohibition guard scanning without requiring the
calling agent to do anything beyond `import memchorus`.

The pre_llm_call hook performs three phases:
  1. Guard scan (ProhibitionsManager): matches input against seed/distilled rules,
     injects [[GUARD]] blocks before any other context — hard gates over prompt content.
  2. Behavioral recall: auto-recall engine queries relevant memories by domain.
  3. Post-storage distillation: saved outcomes classified as CRITICAL mistakes are
     automatically converted into new prohibition rules via ProhibitionDistiller.

Environment control: set MEMCHORUS_AUTO_ENABLED=false to disable all hooks.
"""

import atexit
import hashlib
import importlib  # for dynamic entry_point discovery
import json
import logging
import re
import os
import threading
import time
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Lazy-global bootstrap helper
# ---------------------------------------------------------------------------

def _trigger_memchorus_bootstrap() -> None:
    """Force lazy-init bootstrap by accessing a symbol from the package's
    __getattr__ dispatch table.

    The package defines `_instance = None` as a module-level default so that
    ``from memchorus import _instance`` doesn't crash before bootstrap runs —
    but that also means simply reading ``memchorus._instance`` returns the stale
    default without ever calling ``__getattr__``. Accessing any lazy symbol
    (e.g. ``BehavioralTrigger``) *does* route through ``__getattr__``, which
    kicks off auto_bootstrap and overwrites sys.modules[memchorus]._instance
    with the real orchestrator before returning.

    Calling this once is cheap; subsequent accesses benefit from the internal
    _bootstrap_done guard inside __getattr__.
    """
    import sys
    mod = sys.modules.get("memchorus")
    if mod is not None and not getattr(mod, "_bootstrap_done", True):
        # Touch a lazy symbol to fire bootstrap (safe — already imported above)
        try:
            _ = mod.BehavioralTrigger  # noqa: F841
        except Exception:            # pragma: no cover - fallback is harmless
            pass


def _get_orchestrator() -> Optional[Any]:
    """Return the global MemoryOrchestrator singleton, ensuring bootstrap fires first."""
    try:
        _trigger_memchorus_bootstrap()
        return __import__('memchorus', fromlist=['_instance'])._instance
    except Exception as exc:  # pragma: no cover - defensive fallback
        logger.debug("_get_orchestrator failed (no auto_bootstrap yet): %s", exc)
        return None


# ---------------------------------------------------------------------------
# GAP026-C/D: Batched tool-capture with flush + structured ImportError logging
# ---------------------------------------------------------------------------

# Module-level batcher instance shared across all hook invocations.
# Initialized lazily on first use to avoid startup cost when hooks are disabled.
_CAPTURE_BATCHER: Any = None

def _atexit_flush() -> None:
    """Fallback flush safety net — ensures buffered captures are written even if
    on_session_end doesn't fire (e.g., unhandled exception or SIGTERM).

    Registered via atexit to guarantee remaining items in the batch buffer reach
    storage before the process exits. No-op when _CAPTURE_BATCHER is None.
    """
    if _CAPTURE_BATCHER is not None:
        try:
            _CAPTURE_BATCHER.close()
            logger.info("hooks: atexit flushed capture buffer")
        except Exception as exc:
            logger.warning("hooks: atexit flush failed: %s", exc)


def _get_capture_batcher(orchestrator: Any) -> Optional[Any]:
    """Lazily create and return the global ToolCaptureBatcher singleton.

    Returns None if imports fail — caller should fall back to direct save.
    Logs a structured ImportError on first failure (GAP026-D).
    """
    global _CAPTURE_BATCHER
    if _CAPTURE_BATCHER is not None:
        return _CAPTURE_BATCHER

    try:
        from memchorus.tool_capture_buffer import ToolCaptureBuffer  # noqa: F811
        from memchorus.auto_storage_engine import AutoStorageEngine

        engine = AutoStorageEngine(orchestrator=orchestrator)

        def _batch_flush(payloads: List[Dict[str, Any]]) -> None:
            """Flush a batch of payloads through AutoStorageEngine."""
            for payload in payloads:
                try:
                    text = payload.get("text", "")
                    res = engine.capture_outcome(text, outcome_type="automatic")
                    if res.get("saved"):
                        logger.info(
                            "hooks: batch-saved content (%s, importance %.2f)",
                            res.get("significance", ""),
                            res.get("importance_score", 0.0),
                        )
                        # Post-storage distillation: check if this saved outcome is a
                        # critical mistake worthy of becoming a prohibition guard (AC4)
                        _try_distill_prohibition(text, orchestrator)
                    else:
                        logger.debug("hooks: capture_outcome rejected: %s", res.get("reason", ""))
                except Exception as fe:
                    logger.warning("hooks: batch flush item failed — skipping. %s", fe)

        _CAPTURE_BATCHER = ToolCaptureBuffer(
            max_items=10,
            flush_interval=5.0,
            callback=_batch_flush,
        )
        return _CAPTURE_BATCHER

    except ImportError as ie:
        logger.error(
            "hooks: AutoStorageEngine/ToolCaptureBatcher import failed — %s\n"
            "All post-tool captures will fall back to direct orchestrator.save until restart.\n"
            "This usually means a broken package install; try:\n"
            "  pip install --no-deps 'memchorus[full]' from PyPI",
            ie,
        )
        return None


def _try_save_with_batch(orchestrator: Any, output_str: str) -> None:
    """Queue a tool-capture payload in the batch buffer (GAP026-C).

    If batching is unavailable, falls back to an immediate orchestrator.save()
    so no data is lost — just written individually instead of batched.
    """
    try:
        batcher = _get_capture_batcher(orchestrator)
        if batcher is not None:
            batcher.add({"text": output_str, "outcome_type": "automatic"})
            return

        # Batch unavailable — route through engine for proper classification
        try:
            from memchorus.auto_storage_engine import AutoStorageEngine
            engine = AutoStorageEngine(orchestrator=orchestrator)
            res = engine.capture_outcome(output_str, outcome_type="automatic")
            if res.get("saved"):
                logger.info(
                    "hooks: fallback-direct saved (%s, importance %.2f)",
                    res.get("significance", ""),
                    res.get("importance_score", 0.0),
                )
            else:
                logger.debug("hooks: fallback-direct rejected: %s", res.get("reason", ""))
        except ImportError as ie:
            logger.warning("hooks: AutoStorageEngine unavailable: %s", ie)
            _auto_save(orchestrator, output_str)
    except Exception as e:
        _auto_save(orchestrator, output_str, str(e))

def _auto_save(orchestrator, text, error_context=""):
    """Last-resort save when both batch and engine paths are unavailable."""
    try:
        from memchorus.auto_storage_engine import AutoStorageEngine
        engine = AutoStorageEngine(orchestrator=orchestrator)
        res = engine.capture_outcome(text, outcome_type="automatic")
        if res.get("saved"):
            logger.info(
                "hooks: _auto_save saved (%s)", res.get("significance", ""),
            )
            # Also try distillation on direct saves (AC4 completeness)
            _try_distill_prohibition(text, orchestrator)
    except Exception as e:
        logger.warning(
            "hooks: _auto_save failed — content lost. %s (context: %s)", e, error_context,
        )


def _try_distill_prohibition(text: str, orchestrator) -> None:
    """Post-storage distillation: try to convert a critical mistake into a prohibition guard.

    Called after capture_outcome saves a significant outcome. If the saved text looks like
    self-breaking behavior, distill it into an enforceable rule so the next agent actually
    sees and respects the guard.

    Gracefully degrades when ProhibitionDistiller or ProhibitionsManager are unavailable.
    Skips entirely when prohibitions.distillation_enabled is False.
    """
    # Bypass if distillation is explicitly disabled in config
    prohib_cfg = getattr(orchestrator, "config", {}) or {}
    distill_enabled = bool(prohib_cfg.get("prohibitions", {}).get("distillation_enabled", True))
    if not distill_enabled:
        return

    try:
        from memchorus.prohibition_distiller import ProhibitionDistiller
        from memchorus.prohibitions import ProhibitionsManager

        distiller = ProhibitionDistiller()
        rule_dict = distiller.distill(text)
        if rule_dict is None:
            return  # not worthy or cooldown active — nothing to do

        # Persist the distilled rule through the orchestrator's prohibitions manager
        try:
            pm = getattr(orchestrator, '_prohibitions_manager', None)
            if pm is None:
                pm = ProhibitionsManager()
                pm.load()
                orchestrator._prohibitions_manager = pm
            from memchorus.prohibitions import Prohibition
            rule = Prohibition.from_dict(rule_dict)
            pm.add_rule(rule)
            pm.save()
            logger.info(
                "hooks: distilled and saved new prohibition guard %s (severity=%d)",
                rule.id, rule.severity,
            )
        except Exception as pe:
            logger.warning(
                "hooks: distiller produced a rule but failed to persist via ProhibitionsManager: %s", pe
            )

    except ImportError:
        logger.debug("hooks: prohibition_distiller not available — skipping distillation")
    except Exception as e:
        logger.debug("hooks: distillation failed silently: %s", e)


# ---------------------------------------------------------------------------
# Hook class — discovered by Hermes via entry_points["hermes_agent.plugins"]
# ---------------------------------------------------------------------------

class MemChorusHooks:
    """Lifecycle hooks that fire at key decision points in the agent loop.

    Methods are called by Hermes Gateway at runtime:
      - on_pre_llm_call(context)        before every LLM API call
      - on_post_tool_call(tool_data)    after every tool execution
      - on_session_start(session_id)    once per new Hermes session start
      - on_session_end(session_id)      once per Hermes session end / teardown

    Each method queries the global orchestrator and injects relevant context or
    saves newly significant outcomes automatically. BehavioralTrigger.detect() is
    used to shape recall/saving based on detected decision points.
    """

    def __init__(self) -> None:
        # Instantiate BehavioralTrigger for decision-point detection in hooks.
        # We import lazily here so the class remains usable even when
        # behavioral_trigger isn't available (graceful degradation).
        try:
            from memchorus.behavioral_trigger import BehavioralTrigger, DecisionPoint  # noqa: F401
            self._btrigger = BehavioralTrigger()
        except Exception as exc:
            logger.debug("BehavioralTrigger unavailable: %s — hooks will operate in fallback mode", exc)
            self._btrigger = None

    def on_pre_llm_call(self, **kwargs: Any) -> Optional[Dict[str, Any]]:
        """Fire before an LLM call to auto-recall relevant memories + guard-scan input.

        Returns a dict with injected context (if available) or None if disabled/empty.
        Memory recall is soft context injection; behavioral guards are hard gates that
        inject [[GUARD]] blocks blocking self-breaking actions (env corruption, data loss,
        toolchain breakage). Guards run first before any recall context is added.
        """
        logger.info("MemChorus on_pre_llm_call ENTRY — kwargs keys: %s", list(kwargs.keys())[:5])
        orchestrator = _get_orchestrator()
        if orchestrator is None:
            return None

        try:
            # 1. Call auto-recall engine via orchestrator's search pipeline
            # Use _build_search_terms() for progressive fallback — even when
            # user_message and conversation_history are empty, we can still
            # recall from task context (task_id, model, platform, session_id).
            input_text = _build_search_terms(kwargs)
            if not input_text:
                return None

            # 0. Scan for behavioral guards BEFORE any other injection (hard gates)
            # Bypass if prohibitions.enabled is explicitly set to False
            prohib_cfg = orchestrator.config.get("prohibitions", {})
            guards_enabled = bool(prohib_cfg.get("enabled", True))
            if guards_enabled:
                guard_blocks = self._try_guard_scan(input_text, orchestrator)
            else:
                guard_blocks = []

            detected_points = []
            enriched_terms = input_text
            if self._btrigger is not None:
                input_str = str(input_text)[:4096]  # cap for performance
                detected_points = self._btrigger.detect(input_str)

            # Enrich search query with matched decision-point keywords so that
            # when a merge, commit, or process-trigger word is detected, the
            # recall actually searches for matching process/protocol memories.
            if detected_points:
                matched_kw = " ".join(
                    str(dp.matched_keyword) for dp in detected_points
                    if dp.matched_keyword
                )
                if matched_kw:
                    enriched_terms = f"{matched_kw} {input_text}"

            # Determine search limit based on decision point priority:
            # PLANNING_START / CONTEXTUAL_SYNTHESIS -> broader recall (limit=5)
            # TOOL_CALL_INTENT / ERROR_STATE -> focused recall (limit=3)
            # Default: limit=3
            search_limit = 3
            if detected_points:
                from memchorus.behavioral_trigger import DecisionPoint as _DP
                for dp in detected_points:
                    if dp.type in (_DP.PLANNING_START, _DP.CONTEXTUAL_SYNTHESIS_COMPLETION):
                        search_limit = 5
                        break

            # Use search() (not retrieve()) for pre-decision recall — retrieve(key)
            # only does exact-key lookup and doesn't accept a limit param.
            context_items = orchestrator.search(enriched_terms, limit=search_limit)

            injected_blocks: List[str] = []

            # Guards go first so they appear before soft recall / feedback blocks
            injected_blocks.extend(guard_blocks)

            if context_items:
                injected_blocks.append(
                    "[MemChorus Memory Recall]\n"
                    f"{_format_context_block(context_items)}\n"
                    "[/MemChorus Memory Recall]"
                )

            # 2. Evaluate feedback loop corrections (delegated to private method)
            feedback_blocks = self._try_feedback_loop(input_text, kwargs)
            injected_blocks.extend(feedback_blocks)

            if not injected_blocks:
                return None

            result: Dict[str, Any] = {
                "source": "memchorus_pre_llm_call",
                "context": "\n\n".join(injected_blocks),
            }
            return result

        except Exception as exc:  # pragma: no cover - graceful degradation
            logger.warning("on_pre_llm_call failed — returning None (hooks remain active). %s", exc)
            return None

    def _try_feedback_loop(self, input_text: str, kwargs: Dict[str, Any]) -> List[str]:
        """Run feedback loop to inject targeted course-corrections at decision points.

        Matches the current trigger context against stored corrections whose category
        overlaps with the detected decision type. Fires as [[FEEDBACK CORRECTION]] blocks
        distinct from [MemChorus Memory Recall]. Each injection decrements the correction's
        exhaust TTL counter — exhausted entries are archived and removed from the queue.

        Gracefully degrades when feedback_loop module is unavailable.

        Args:
            input_text: Combined search terms from kwargs (message, task, etc.)
            kwargs: Original kwargs passed to on_pre_llm_call (contains trigger_category, etc.)

        Returns:
            List of [[FEEDBACK CORRECTION]] markdown block strings (may be empty).
        """
        try:
            orchestrator = _get_orchestrator()
            if orchestrator is None:
                return []

            fb_config = orchestrator.config.get("feedback_loop", {})
            # Default enable when feedback_loop key exists but 'enabled' missing
            enabled = bool(fb_config.get("enabled", True))
            feedback_mgr_config = {"feedback_loop": {**fb_config, "enabled": enabled}}

            from memchorus.feedback_loop import FeedbackLoopManager
            mgr = FeedbackLoopManager(config=feedback_mgr_config)
            return mgr.process_feedback(input_text, kwargs)
        except ImportError:
            logger.debug("hooks: feedback_loop module unavailable — returning empty blocks")
            return []
        except Exception as e:
            logger.debug("hooks: feedback loop failed silently: %s", e)
            return []

    def _try_guard_scan(self, input_text: str, orchestrator) -> List[str]:
        """Scan input for behavioral prohibition matches and inject [[GUARD]] blocks.

        Reuses the cached ProhibitionsManager from the orchestrator (populated by distillation
        or created lazily here). Gracefully degrades when the prohibitions module is unavailable.
        Returns a list of markdown guard block strings (may be empty).
        """
        try:
            from memchorus.prohibitions import ProhibitionsManager

            # Reuse cached prohibitions manager or create fresh one
            pm = getattr(orchestrator, "_prohibitions_manager", None)
            if pm is None:
                pm = ProhibitionsManager()
                count = pm.load()
                orchestrator._prohibitions_manager = pm
                logger.info("hooks: initialized ProhibitionsManager with %d rules", count)

            result = pm.scan_text(str(input_text)[:4096])  # cap for performance
            blocks = result.inject_blocks()
            if blocks:
                guard_header = "[[BEHAVIORAL GUARDS]]\n" + "\n".join(blocks)
                logger.info(
                    "hooks: prohibition scan triggered %d rule(s) (verdict=%s, %.1fms)",
                    len(result.matched_rules), result.verdict.value, result.timing_ms,
                )
                return [guard_header]
            return []
        except ImportError:
            logger.debug("hooks: ProhibitionsManager not available — skipping guard scan")
            return []
        except Exception as e:
            logger.debug("hooks: guard scan failed silently: %s", e)
            return []

    def on_post_tool_call(self, **kwargs: Any) -> Optional[Dict[str, Any]]:
        logger.info("MemChorus on_post_tool_call ENTRY — kwargs keys: %s", list(kwargs.keys())[:5])
        """Fire after tool execution to auto-capture significant outcomes.

        If the tool output contains important results, save them automatically
        for future recall without the agent needing to remember to store it later.

        Returns dict with storage confirmation or None if nothing captured.
        """
        orchestrator = _get_orchestrator()
        if orchestrator is None:
            return None

        try:
            # Hermes model_tools._emit_post_tool_call_hook() passes result=
            tool_output = kwargs.get("result")
            if not tool_output:
                return None

            # Convert structured outputs to readable text.
            # dict/list results should become JSON (not Python repr via str())
            # so downstream significance detection, entropy checks, and recall
            # see clean data instead of garbled "{'k': 'v'}" strings.
            if isinstance(tool_output, (dict, list)):
                output_str = json.dumps(tool_output)
            else:
                output_str = str(tool_output)

            # Guard: skip query echo artifacts — recall query templates that
            # leaked through the tool pipeline and would pollute memory storage.
            from memchorus.auto_storage_engine import _is_query_echo
            if _is_query_echo(output_str):
                logger.debug("hooks: skipping query echo artifact in tool output")
                return None

            # Guard: skip placeholder artifacts — synthetic filler text like
            # "session context [TASK-ID] current task" that upstream injects
            # when no real tool_output is available (MC-004).
            from memchorus.auto_storage_engine import _is_placeholder_artifact
            if _is_placeholder_artifact(output_str):
                logger.debug("hooks: skipping placeholder artifact in tool output")
                return None

            # BehavioralTrigger gate: only auto-save when decision points detected.
            # This prevents noise-flooding (Bug 4 fix) and makes the behavioral
            # significance detector actually functional.
            # Fallback: if output is substantial (>=200 chars with signal entropy),
            # save it regardless — structured tool results have no natural-language
            # decision-point cues but can still be meaningful.
            detected = []
            if self._btrigger is not None:
                detected = self._btrigger.detect(output_str)

            from memchorus.auto_storage_engine import _has_minimum_signal
            has_signal = _has_minimum_signal(output_str)

            if not detected and not has_signal:
                logger.debug(
                    "hooks: no behavioral decision points AND no signal entropy in tool output — skipping auto-save"
                )
                return None

            if detected:
                logger.info("hooks: BehavioralTrigger detected %d decision point(s)", len(detected))
            else:
                logger.debug("hooks: fallback — substantial signal content (%d chars, entropy OK) saved", len(output_str))

            # Route through AutoStorageEngine's capture_outcome pipeline for
            # proper significance detection, scoring, provenance markers, and
            # dedup — results are buffered via ToolCaptureBatcher to avoid
            # hammering storage on every tool call (GAP026-C / GAP026-D).
            _try_save_with_batch(orchestrator, output_str)

            return None

        except Exception as exc:  # pragma: no cover - graceful degradation
            logger.warning("on_post_tool_call failed — returning None. %s", exc)
            return None

    def on_session_start(self, **kwargs: Any) -> Optional[Dict[str, Any]]:
        """Fire once when a new Hermes session begins (or task is picked up) to auto-orient the agent.

        Reads HERMES_KANBAN_TASK or WORKSPACE from env, queries relevant memories,
        and returns them so the session starts with project context already available.
        """
        orchestrator = _get_orchestrator()
        if orchestrator is None:
            return None

        try:
            # Try to load orientation engine (may not exist yet — optional import)
            orient_module = importlib.import_module("memchorus.orientation")
        except ImportError:
            # Orientation subsystem not installed yet — no big deal, just skip it)
            logger.debug("Orientation module not available — skipping auto-orientation.")
            return None

        try:
            kanban_task = os.environ.get("HERMES_KANBAN_TASK")

            # Delegate the full orientation sequence to the module — it handles
            # cache checks, project detection, query construction, and silent
            # degradation all at once.
            all_items = orient_module.orientation_search(
                env_task=kanban_task,
                orchestrator=orchestrator,
                limit=5,
                cache_ttl_seconds=getattr(orient_module, "DEFAULT_CACHE_TTL_SECONDS", 60.0),
            )

            if not all_items:
                return None  # empty → silent skip (AC-O3)

            result: Dict[str, Any] = {
                "source": "memchorus_auto_orientation",
                "project_context": _format_context_block(all_items),
            }
            return result

        except Exception as exc:  # pragma: no cover - graceful degradation
            logger.warning("on_session_start orientation failed. %s", exc)
            return None

    def on_session_end(self, **kwargs: Any) -> Optional[Dict[str, Any]]:
        """Fire when a Hermes session ends to flush pending captures and clean up resources.

        Ensures the ToolCaptureBatcher drains any remaining items to storage before
        the process exits. Also deregisters the atexit handler so we don't double-flush
        if both on_session_end and atexit fire.

        Auto-tuning: scans conversation history for user correction signals via
        MistakeDetector and feeds noise/useful flags back to HitRateTracker (§10.2).

        Returns dict with flush confirmation or None if nothing to flush.
        """
        global _CAPTURE_BATCHER
        try:
            batcher = _CAPTURE_BATCHER
            if batcher is not None:
# pending is an int property — don't wrap in len() again.
                # Fall back to _queue only if .pending doesn't exist (old code).
                try:
                    count_before = batcher.pending  # already an int
                except AttributeError:
                    count_before = len(getattr(batcher, '_queue', []))
                batcher.close()
                logger.info(
                    "hooks: on_session_end flush complete (pending=%d)",
                    count_before,
                )
            else:
                logger.debug("hooks: on_session_end — no capture batcher to flush")
            _CAPTURE_BATCHER = None
            # Remove atexit handler since we're flushing explicitly here
            try:
                atexit.unregister(_atexit_flush)
            except Exception:
                pass  # best-effort unregister; harmless if it fails

        except Exception as exc:  # pragma: no cover - graceful degradation
            logger.warning("on_session_end failed — atexit still active. %s", exc, exc_info=True)
            return None

        # Auto-tuning: session-end mistake detection (§10.2 turn-retrospective)
        try:
            orchestrator = _get_orchestrator()
            if orchestrator is not None:
                from memchorus.mistake_detector import MistakeDetector as _MD
                from memchorus.hit_rate_tracker import HitRateTracker as _HRT
                from memchorus.prohibitions import ProhibitionsManager as _PM

                detector = _MD.get_instance()
                tracker = _HRT.get_instance()

                # Gather all user text from this session's conversation history
                history = kwargs.get("conversation_history") or []
                user_texts: List[str] = []
                for m in history:
                    if isinstance(m, dict):
                        role = m.get("role", "")
                        if role in ("user", "human"):
                            text = m.get("content", "") or m.get("text", "")
                            if text:
                                user_texts.append(str(text))
                    elif isinstance(m, str):
                        user_texts.append(m)

                total_noise = 0
                total_useful = 0
                for ut in user_texts:
                    noise, useful = detector.classify_and_flag(ut)
                    total_noise += noise
                    total_useful += useful

                if total_noise or total_useful:
                    logger.info(
                        "hooks: on_session_end auto-tuning — noise=%d useful=%d from %d user messages",
                        total_noise, total_useful, len(user_texts),
                    )
        except Exception:  # pragma: no cover - graceful degradation
            pass  # tracking failures never interrupt session teardown

        return {
            "source": "memchorus_session_end",
            "teardown": "complete",
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# GAP P0-4 FIX (2026-07-19): Enforce character budget per entry + total block
_MAX_CONTENT_CHARS = 300   # max chars per single memory entry
_DEFAULT_MAX_BLOCK_CHARS = 2000  # GH-96: configurable default recall block ceiling
_MAX_BLOCK_CHARS = _DEFAULT_MAX_BLOCK_CHARS

def _extract_text_from_message(message: Any) -> str:
    """Extract text content from a Message dict/object for search purposes."""
    if isinstance(message, str):
        return message
    if isinstance(message, dict):
        return message.get("content", "") or message.get("text", "") or ""
    # Fallback for object with .content or .text attributes
    try:
        content = getattr(message, "content", None) or getattr(message, "text", None)
        if isinstance(content, str):
            return content
    except Exception:
        pass
    return ""


# ---------------------------------------------------------------------------
# Search term quality helpers — stop-word filtering, stemming, TF scoring
# Added for [TASK-ID]: mitigate score dilution from noise in raw messages
# ---------------------------------------------------------------------------

_STOP_WORDS = frozenset([
    # Core English stopwords
    "i", "me", "my", "myself", "we", "our", "ours", "ourselves", "you",
    "your", "yours", "yourself", "yourselves", "he", "him", "his",
    "himself", "she", "her", "hers", "herself", "it", "its", "itself",
    "they", "them", "their", "theirs", "themselves", "what", "which",
    "who", "whom", "this", "that", "these", "those", "am", "is", "are",
    "was", "were", "be", "been", "being", "have", "has", "had", "having",
    "do", "does", "did", "doing", "a", "an", "the", "and", "but", "if",
    "or", "because", "as", "until", "while", "of", "at", "by", "for",
    "with", "about", "against", "between", "through", "during", "before",
    "after", "above", "below", "to", "from", "up", "down", "in", "out",
    "on", "off", "over", "under", "again", "further", "then", "once",
    "here", "there", "when", "where", "why", "how", "all", "both",
    "each", "every", "either", "neither", "nor", "not", "only", "own",
    "same", "so", "than", "too", "very", "s", "t", "can", "will",
    "just", "don", "should", "now", "d", "ll", "m", "o", "re", "ve",
    "y", "ain", "aren", "couldn", "didn", "doesn", "hadn", "hasn",
    "haven", "isn", "ma", "mightn", "mustn", "needn", "shan", "shouldn",
    "wasn", "weren", "won", "wouldn",
    # Common conversational filler and low-signal words
    "said", "get", "got", "go", "going", "like", "make", "made", "may",
    "might", "much", "many", "well", "want", "went", "will", "would",
    "could", "shall", "also", "into", "one", "two", "first", "really",
    "already", "something", "nothing", "everything", "anything",
    "however", "therefore", "meanwhile", "furthermore", "consequently",
    "additionally", "generally", "probably", "certainly", "actually",
    "definitely", "basically", "literally", "exactly", "quite",
    "rather", "perhaps", "obviously", "clearly", "simply",
    # Developer/conversational noise
    "hey", "hi", "hello", "thanks", "please", "ok", "okay", "sure",
    "right", "yes", "no", "yeah", "yep", "hmm", "ugh", "wow",
    "cool", "great", "nice", "awesome", "sorry", "oops", "err",
    "errr", "ok", "lol", "btw", "fyi", "imo", "imho", "tbh", "idk",
    "smh", "lmao", "rofl", "brb", "gtg", "ttfn", "np",
    # Pseudo-code markers and formatting
    "note", "edit", "update", "ps", "pp",
])

_MAX_TERMS = 40  # cap on how many terms to return


def _simple_stem(word: str) -> str:
    """Very conservative suffix stemming that avoids breaking root words.

    Only strips productive English suffixes when the remaining base is
    at least 5 characters long, preventing destruction of short roots.
    """
    w = word.lower()
    if len(w) < 6:
        return w

    # -tion / -sion -> empty (implementa**tion** -> implementa ~implement)
    for suff in ("tion", "sion"):
        if w.endswith(suff):
            base = w[:-len(suff)]
            return base if len(base) >= 5 else w

    # -ing -> remove (running -> run, fixing -> fix, building -> build)
    if w.endswith("ing"):
        base = w[:-3]
        if len(base) >= 5:
            # handle "fixing" -> "fix" double-consonant drop
            if len(base[-1]) == len(base[-2]):
                return base
            return base
    elif w.endswith("ed"):
        base = w[:-2]
        if len(base) >= 5:
            # same double-consonant convention
            if len(base[-1]) == len(base[-2]):
                return base
            return base
    elif w.endswith("ing"):
        base = w[:-3]
        if len(base) >= 5:
            return base

    return w


def _filter_and_score_terms(text: str) -> List[str]:
    """Remove stopwords, apply stemming, rank by term frequency.

    Prevents low-signal tokens from diluting match scores in
    _content_matches(). Returns a list of high-signal terms sorted by
    frequency (descending), capped at _MAX_TERMS.
    """
    # Tokenize on whitespace + common delimiters preserving alphanumerics
    raw = re.findall(r"[a-zA-Z0-9]+(?:[._-][a-zA-Z0-9]+)*", text.lower())

    # Stop-word filter
    filtered = [w for w in raw if len(w) >= 2 and w not in _STOP_WORDS]

    if not filtered:
        return []

    # Stem aggressively but conservatively — preserve root words < 6 chars
    stemmed = [_simple_stem(w) for w in filtered]

    # Count term frequency for ranking
    tf: Dict[str, int] = {}
    for word in stemmed:
        tf[word] = tf.get(word, 0) + 1

    # Sort by descending frequency then alphabetical tie-break
    ranked = sorted(tf.keys(), key=lambda w: (-tf[w], w))
    return ranked[:_MAX_TERMS]


def _build_search_terms(kwargs: Dict[str, Any]) -> str:
    """Build search query from kwargs with progressive fallbacks.

    Even when user_message and conversation_history are empty/missing,
    we can construct a meaningful recall query from task context, model,
    platform, etc. This prevents pre-LLM recall from silently returning
    None simply because the primary text sources are falsy.

    Priority chain:
        1. user_message (primary input)
        2. conversation_history (last messages as fallback)
        3. task_id / model / platform context for project-aligned recall
        4. Empty string — caller should return None to skip
    """
    # Primary: user message
    user_msg = kwargs.get("user_message")
    if isinstance(user_msg, str) and user_msg.strip():
        terms = _filter_and_score_terms(user_msg)
        if terms:
            return " ".join(terms)
        return user_msg
    if isinstance(user_msg, dict):
        text = _extract_text_from_message(user_msg)
        if text:
            terms = _filter_and_score_terms(text)
            if terms:
                return " ".join(terms)
            return text
    # Catch-all for object-style messages with .content / .text attributes
    if not isinstance(user_msg, (str, dict, type(None))):
        text = _extract_text_from_message(user_msg)
        if text:
            terms = _filter_and_score_terms(text)
            if terms:
                return " ".join(terms)
            return text

    # Fallback 1: conversation history (use last message content)
    history = kwargs.get("conversation_history") or []
    if isinstance(history, list) and history:
        messages = [_extract_text_from_message(m) for m in history]
        available = " ".join([m for m in messages if m])[-4096:]  # cap length
        if available:
            terms = _filter_and_score_terms(available)
            if terms:
                return " ".join(terms)
            return available

    # Fallback 2: Build context-based query from metadata
    parts: List[str] = []
    task_id = kwargs.get("task_id")
    if task_id:
        parts.append(str(task_id))
    model = kwargs.get("model")
    if model:
        parts.append(str(model))
    platform = kwargs.get("platform")
    if platform and str(platform).strip():
        parts.append(str(platform))
    session_id = kwargs.get("session_id")
    if session_id:
        parts.append(str(session_id)[:16])

    return " ".join(parts)


def _resolve_char_limit() -> int:
    """Return effective recall block character ceiling (GH-96).

    Resolution order (first match wins):
      1. MEMCHORUS_RECALL_MAX_CHARS env var
      2. Per-profile config.yaml: memchorus.recall.max_block_chars
         OR memchorus.hook_char_limit (legacy key)
      3. Global default _DEFAULT_MAX_BLOCK_CHARS
    """
    from memchorus import _sanitize_profile

    # Layer 1: environment variable
    env_val = os.environ.get("MEMCHORUS_RECALL_MAX_CHARS")
    if env_val is not None:
        try:
            val = int(env_val)
            return max(200, min(val, 10000))
        except ValueError:
            pass

    # Layer 2: per-profile config.yaml
    try:
        profile = _sanitize_profile(os.environ.get("HERMES_PROFILE", "default"))
        cfg_path = str(_Path.home() / ".hermes" / "profiles" / profile / "config.yaml")
        p = _Path(cfg_path)
        if p.exists():
            data = _yml.safe_load(p.read_text()) or {}
            memchorus_cfg = data.get("memchorus", {})

            # New key: recall.max_block_chars (nested under recall.)
            if isinstance(memchorus_cfg, dict):
                recall_cfg = memchorus_cfg.get("recall", None)
                if isinstance(recall_cfg, dict):
                    limit = recall_cfg.get("max_block_chars", None)
                    if isinstance(limit, int):
                        return max(200, min(limit, 10000))

            # Legacy key: memchorus.hook_char_limit
            if isinstance(memchorus_cfg, dict):
                limit = memchorus_cfg.get("hook_char_limit", None)
                if isinstance(limit, int):
                    return max(200, min(limit, 10000))
    except Exception:
        pass

    # Layer 3: global default
    return _DEFAULT_MAX_BLOCK_CHARS


# ---------------------------------------------------------------------------
# Cross-turn injection suppression window (GH-141)
#
# Recall re-injected the same entries (unchanged content) on consecutive turns,
# wasting prompt budget. GH-141 adds a bounded LRU + TTL window cache of
# recently-injected (key, content_hash) pairs, keyed per Hermes profile so
# windows never cross-contaminate between agents. When a key is within the
# window AND its content hash is unchanged since the last render, the block
# emits ONE compact marker line instead of the full body. Changed content or
# TTL expiry restores the full body.
# ---------------------------------------------------------------------------

_DEFAULT_SUPPRESSION_WINDOW = 5      # entries
_DEFAULT_SUPPRESSION_TTL_S = 180.0   # seconds — short enough to re-render on a new session
_MAX_WINDOW_SIZE = 200               # hard cap regardless of config (memory safety)

_SUPPRESSION_MARKER = "↳ (shown earlier)"


def _hash_content(content: str) -> str:
    """Stable digest of rendered content — used to detect unchanged re-injection."""
    return hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()[:16]


class _SuppressionWindow:
    """Bounded LRU + TTL cache of recently-injected (key, content_hash) pairs.

    Thread-safe. ``mark()`` records a render (evicting expired / over-cap
    entries first), ``suppressed()`` reports whether a re-render of the same
    key + same hash should collapse to a marker line.
    """

    __slots__ = ("_window_size", "_ttl_seconds", "_entries", "_lock")

    def __init__(self, window_size: int = _DEFAULT_SUPPRESSION_WINDOW,
                 ttl_seconds: float = _DEFAULT_SUPPRESSION_TTL_S):
        self._window_size = max(1, min(int(window_size), _MAX_WINDOW_SIZE))
        self._ttl_seconds = max(0.0, float(ttl_seconds))
        self._entries: "OrderedDict[Any, Tuple[str, float]]" = OrderedDict()
        self._lock = threading.Lock()

    def _prune_expired(self) -> None:
        now = time.time()
        stale = [k for k, (_h, ts) in self._entries.items()
                 if (now - ts) > self._ttl_seconds]
        for k in stale:
            del self._entries[k]

    def suppressed(self, key: Any, content_hash: str) -> bool:
        """True if this key+hash was already rendered within the window."""
        with self._lock:
            rec = self._entries.get(key)
            if rec is None:
                return False
            stored_hash, ts = rec
            if (time.time() - ts) > self._ttl_seconds:
                del self._entries[key]
                return False
            return stored_hash == content_hash

    def mark(self, key: Any, content_hash: str) -> None:
        """Record a full-body render (or a marker render) for this key."""
        with self._lock:
            self._prune_expired()
            self._entries.pop(key, None)
            self._entries[key] = (content_hash, time.time())
            while len(self._entries) > self._window_size:
                self._entries.popitem(last=False)  # evict least-recently-used


# One window per Hermes profile so agents don't share suppression state.
_suppression_windows: Dict[str, _SuppressionWindow] = {}
_suppression_windows_lock = threading.Lock()


def _resolve_profile_name() -> str:
    """Hermes profile identifier used to scope the suppression window."""
    try:
        from memchorus import _sanitize_profile
        return _sanitize_profile(os.environ.get("HERMES_PROFILE", "default"))
    except Exception:
        return os.environ.get("HERMES_PROFILE", "default") or "default"


def _resolve_suppression_settings() -> "tuple[int, float]":
    """(window_size, ttl_seconds) from per-profile config.yaml.

    Keys: memchorus.suppression.window_size / .ttl_seconds (or legacy memchorus.recall).
    Env overrides: MEMCHORUS_SUPPRESSION_WINDOW, MEMCHORUS_SUPPRESSION_TTL.
    Returns defaults when unset.
    """
    window_size, ttl = _DEFAULT_SUPPRESSION_WINDOW, _DEFAULT_SUPPRESSION_TTL_S
    profile = _resolve_profile_name()
    try:
        cfg_path = _Path.home() / ".hermes" / "profiles" / profile / "config.yaml"
        if not cfg_path.exists():
            cfg_path = _Path.home() / ".hermes" / "config.yaml"
        if cfg_path.exists():
            data = _yml.safe_load(cfg_path.read_text()) or {}
            memchorus_cfg = data.get("memchorus", {}) if isinstance(data, dict) else {}
            sup_cfg = memchorus_cfg.get("suppression", {}) if isinstance(memchorus_cfg, dict) else {}
            if isinstance(sup_cfg, dict):
                ws = sup_cfg.get("window_size")
                if isinstance(ws, int) and ws > 0:
                    window_size = ws
                ts = sup_cfg.get("ttl_seconds")
                if isinstance(ts, (int, float)) and ts >= 0:
                    ttl = float(ts)
            recall_cfg = memchorus_cfg.get("recall", {}) if isinstance(memchorus_cfg, dict) else {}
            if isinstance(recall_cfg, dict):
                ws = recall_cfg.get("suppression_window")
                if isinstance(ws, int) and ws > 0 and window_size == _DEFAULT_SUPPRESSION_WINDOW:
                    window_size = ws
    except Exception:
        pass  # config errors never break recall — fall back to defaults

    if os.environ.get("MEMCHORUS_SUPPRESSION_WINDOW"):
        try:
            window_size = int(os.environ["MEMCHORUS_SUPPRESSION_WINDOW"])
        except ValueError:
            pass
    if os.environ.get("MEMCHORUS_SUPPRESSION_TTL"):
        try:
            ttl = float(os.environ["MEMCHORUS_SUPPRESSION_TTL"])
        except ValueError:
            pass
    return window_size, ttl


def _get_suppression_window() -> _SuppressionWindow:
    """Fetch (or create) the per-profile suppression window instance."""
    profile = _resolve_profile_name()
    with _suppression_windows_lock:
        win = _suppression_windows.get(profile)
        if win is None:
            ws, ttl = _resolve_suppression_settings()
            win = _SuppressionWindow(ws, ttl)
            _suppression_windows[profile] = win
        return win


def _clear_suppression_windows() -> None:
    """Reset all windows (used by tests; safe at any time)."""
    with _suppression_windows_lock:
        _suppression_windows.clear()


def _unwrap_content_field(value: Any) -> str:
    """Unwrap raw content coming out of memory sources into a clean string.

    Memory sources occasionally return structured payloads instead of plain
    strings — e.g. ``{"key": ..., "content": {"text": "..."}}`` or
    ``{"text": "..."}``.  Naive ``str(value)`` turns those into ``{'key': ...}``
    dict reprs that are useless inside an injected context block (#143).

    Resolution order:
      1. ``value`` is already a ``str`` -> returned as-is
      2. ``value`` is a ``dict`` containing a string ``text`` field -> that text
      3. ``value`` is a ``dict`` whose ``content`` field resolves to a string
         (recursing through nested wrappers, with depth guard)
      4. ``value`` is a ``dict`` / list -> compact JSON
      5. anything else -> ``str(value)``
    """
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        direct = value.get("text")
        if isinstance(direct, str) and direct:
            return direct
        if "content" in value and isinstance(value["content"], (dict, list)):
            inner = _unwrap_content_field(value["content"])
            if inner:
                return inner
        try:
            return json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            return str(value)
    if isinstance(value, (list, tuple)):
        try:
            return json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            return str(value)
    return str(value)

def _format_context_block(items: List[Dict[str, Any]]) -> str:
    """Turn orchestrator results into a Markdown-ready context block for agent consumption.

    Enforces character budget so huge auto-tool dumps don't destroy the prompt window.
    GH-96: resolves max_block_chars dynamically via _resolve_char_limit() (env var,
    per-profile config, or default).  When the budget is exceeded, lowest-scored
    entries are dropped first to preserve the most relevant memories.
    Truncation respects line boundaries — partial lines are dropped rather than cut,
    ensuring markdown formatting stays intact.
    """
    if not items:
        return ""

    max_chars = _resolve_char_limit()

    # Build formatted lines with per-entry budget enforcement + key dedup.
    # Each entry is (line, score, key_or_None, hash_or_None) — identity is only
    # present for full-body entries so the post-drop pass can mark precisely
    # those that actually rendered (markers never re-mark; dropped entries don't).
    entries: List[Tuple[str, float, Optional[Any], Optional[str]]] = []
    seen_keys: set = set()
    window = _get_suppression_window()

    for item in items:
        key = item.get("key") or str(item)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        score = float(item.get("score", 0.0))
        content_raw = item.get("content")
        if content_raw in (None, ""):
            content_raw = item.get("text") or ""
        # Unwrap structured payloads (e.g. {"text": ...}, nested {"content": ...})
        # into a human-readable string rather than a raw dict repr (#143).
        content_raw = _unwrap_content_field(content_raw)
        raw_content = content_raw.rstrip()

        # --- Cross-turn suppression (GH-141): if this exact key+content was
        # already rendered recently (per profile), collapse to a marker line
        # instead of repeating the full body in the next turn's block.
        content_hash = _hash_content(raw_content)
        if window.suppressed(key, content_hash):
            entries.append((f"- {key}: {_SUPPRESSION_MARKER}", score, None, None))
            continue

        # --- Per-entry budget enforcement (line-boundary aware) --------------
        if len(raw_content) > _MAX_CONTENT_CHARS:
            content_lines = raw_content.split("\n")
            if len(content_lines) == 1:
                # Single-line content — safe to cut at any point
                raw_content = raw_content[:_MAX_CONTENT_CHARS] + "..."
            else:
                # Multi-line — only keep complete lines up to budget
                kept: List[str] = []
                running = 0
                for line in content_lines:
                    if running + len(line) + 1 > _MAX_CONTENT_CHARS:
                        break
                    kept.append(line)
                    running += len(line) + 1
                if kept:
                    raw_content = "\n".join(kept) + "..."
                else:
                    # First line alone exceeds budget — partial cut as fallback
                    raw_content = content_lines[0][:_MAX_CONTENT_CHARS] + "..."

        line = f"- **{key}** — {raw_content}"
        entries.append((line, score, key, content_hash))

    # Sort by score ascending so we can pop lowest-scored first if over budget
    entries.sort(key=lambda e: e[1])
    lines = [e[0] for e in entries]

    joined = "\n".join(lines)
    dropped_count = 0

    # --- Hard total block ceiling (drops complete entries, not partial lines)-
    # Constants that appear in every output regardless of truncation
    _header_len = len("[MemChorus injected context]\n")
    _footer_len = len("\n[/MemChorus injected block]")

    while entries and len(joined) + _header_len + _footer_len > max_chars:
        # Remove the lowest-scored entry (at the front after ascending sort)
        entries.pop(0)
        dropped_count += 1
        lines = [e[0] for e in entries]
        joined = "\n".join(lines)

    # Re-sort remaining entries descending by score so highest-ranked appears first
    if dropped_count > 0:
        entries.sort(key=lambda e: -e[1])
        lines = [e[0] for e in entries]
        joined = "\n".join(lines)
        joined += f"\n... (truncated, budget exceeded — {dropped_count} entries dropped)"

    # Mark only the full-body entries that actually rendered into this block
    # (skips markers and any that were dropped by the budget pass) so the
    # cross-turn suppression window is render-accurate.
    for _line, _score, _key, _hash in entries:
        if _key is not None and _hash is not None:
            window.mark(_key, _hash)

    return f"[MemChorus injected context]\n{joined}\n[/MemChorus injected block]"


# Plugin configuration loader — reads plugin.yaml save_triggers before bootstrap
import yaml as _yml  # Optional: skip if PyYAML not installed
from pathlib import Path as _Path

__PLUGIN_YAML_PATH = str(_Path.home() / ".hermes" / "plugins" / "hermes-memchorus" / "plugin.yaml")


def _load_plugin_config() -> dict:
    """Read plugin.yaml from the default Hermes plugin path. Returns {} on failure."""
    try:
        p = _Path(__PLUGIN_YAML_PATH)
        if not p.exists():
            return {}
        raw = p.read_text()
        cfg = _yml.safe_load(raw) or {}
        if not isinstance(cfg, dict):
            return {}
        return cfg
    except Exception:
        # PyYAML missing or file unreadable — caller decides if that's fatal
        return {}


# ---------------------------------------------------------------------------
# Hermes plugin entry point -- called by Hermes gateway at startup
# ---------------------------------------------------------------------------

_instance_holder: List[Any] = [None]  # mutable container for the registered instance


def register(ctx: Any) -> None:
    """Hermes plugin registration callback.

    Called by the Hermes gateway when the plugin is discovered via entry points
    or directory scanning. Registers lifecycle hook callbacks with PluginContext.
    """
    # Merge user-provided save_triggers BEFORE any BehavioralTrigger instance exists
    plugin_cfg = _load_plugin_config()
    user_triggers = plugin_cfg.get("save_triggers", [])  # type: ignore[union-attr]
    if user_triggers and hasattr(ctx, 'plugin_config'):
        try:
            from memchorus.behavioral_trigger import configure_save_triggers  # type: ignore[attr-defined]
            configure_save_triggers(user_triggers)
        except Exception as exc:
            logger.warning("Failed to apply save_triggers: %s", exc)

    # Trigger lazy bootstrap of orchestrator singleton BEFORE registering hooks.
    # This ensures _instance exists when hooks fire — without it every hook
    # silently returns None (see [TASK-ID]). Orchestrator hit this lazily via MCP tool
    # access, but executor never touches memchorus directly so bootstrap was
    # deferred forever.
    __import__('memchorus', fromlist=['_trigger_lazy_bootstrap'])._trigger_lazy_bootstrap()

    hooks = MemChorusHooks()
    _instance_holder[0] = hooks  # keep a reference so GC doesn't collect

    # Register all four lifecycle hooks
    ctx.register_hook("pre_llm_call", hooks.on_pre_llm_call)
    ctx.register_hook("post_tool_call", hooks.on_post_tool_call)
    ctx.register_hook("on_session_start", hooks.on_session_start)
    ctx.register_hook("on_session_end", hooks.on_session_end)

    # Register atexit fallback safety net — flushes buffer if on_session_end doesn't fire
    try:
        atexit.register(_atexit_flush)
    except Exception:
        logger.warning("hooks: failed to register atexit fallback")

    logger.info(
        "MemChorus v%s registered hooks: pre_llm_call, post_tool_call, on_session_start, on_session_end",
        __import__('memchorus').__version__,
    )


# ---------------------------------------------------------------------------
# Module-level hook functions — Hermes plugin loader expects these at package.
# Delegates to the singleton MemChorusHooks instance created during register().
# Falls back gracefully when register() hasn't run yet (e.g., direct import).
# ---------------------------------------------------------------------------

def pre_llm_call(**kwargs: Any) -> Optional[Dict[str, Any]]:
    """Module-level alias for on_pre_llm_call."""
    return _get_module_hooks().on_pre_llm_call(**kwargs)


def post_tool_call(**kwargs: Any) -> Optional[Dict[str, Any]]:
    """Module-level alias for on_post_tool_call."""
    return _get_module_hooks().on_post_tool_call(**kwargs)


def on_pre_llm_call(**kwargs: Any) -> Optional[Dict[str, Any]]:
    """Module-level alias — delegates to MemChorusHooks.on_pre_llm_call."""
    return pre_llm_call(**kwargs)


def on_post_tool_call(**kwargs: Any) -> Optional[Dict[str, Any]]:
    """Module-level alias — delegates to MemChorusHooks.on_post_tool_call."""
    return post_tool_call(**kwargs)


def on_session_start(**kwargs: Any) -> Optional[Dict[str, Any]]:
    """Module-level alias — delegates to MemChorusHooks.on_session_start."""
    return _get_module_hooks().on_session_start(**kwargs)


def on_session_end(**kwargs: Any) -> Optional[Dict[str, Any]]:
    """Module-level alias — delegates to MemChorusHooks.on_session_end."""
    return _get_module_hooks().on_session_end(**kwargs)


def _get_module_hooks() -> "MemChorusHooks":
    """Return the registered hooks instance (if register() ran), or create a lazy fallback.

    The fallback is cached so repeated calls still return the same singleton.
    """
    inst = _instance_holder[0]
    if inst is None:
        # Lazy fallback for direct import without going through Hermes plugin system.
        _instance_holder[0] = inst = MemChorusHooks()
    return inst