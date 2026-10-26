"""Main execution loop for JEv browser automation agent.

Coordinates site discovery, perception, recipe guidance, Jev decisions,
text generation, and action execution with self-healing recovery.
"""

import os
import re
import sys
import time
import urllib.parse
from typing import Any, Dict, List, Optional, Set, Tuple
from core.jev.control import browser_input_interrupted
from core.browser_use.secret_entry import SecretEntryError, secret_failure_reason
from core.browser_use.recipient_selection import RecipientSelectionError
from core.jev.calendar_interaction import interact_calendar, parse_date, start_calendar

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def load_env(path: str = ".env"):
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip())


load_env()

import browser_factory
import decide
try:
    from core.jev import llm_decider
except ImportError:
    import llm_decider
try:
    from core.jev import trajectory
except ImportError:
    import trajectory
from core.jev import logger
import text
from recipes.registry import find_recipe

GOOGLE_SEARCH = "https://www.google.com/search?q={}"
DUCK_SEARCH = "https://duckduckgo.com/?q={}"
SITE_OK_AT = 0.6

BOT_MARKERS = (
    "captcha",
    "are you a robot",
    "are you a person",
    "unusual traffic",
    "access denied",
    "request blocked",
    "forbidden",
)

GUESS_STOPWORDS = frozenset(
    """about best book check click comment comments find first goto http https
    latest official open page pages please price release releases search show
    ticket tickets video videos website""".split()
)


def search_urls(query: str) -> Tuple[Tuple[str, str], ...]:
    quoted = urllib.parse.quote_plus(query)
    return (("Google", GOOGLE_SEARCH.format(quoted)), ("DuckDuckGo", DUCK_SEARCH.format(quoted)))


def guess_is_obvious(goal: str, url: str) -> bool:
    parts = urllib.parse.urlparse(url)
    hay = re.sub(r"[^a-z0-9]+", " ", (parts.netloc + parts.path).lower())
    words = {w for w in re.findall(r"[a-z0-9]+", goal.lower()) if w not in GUESS_STOPWORDS}
    for w in words:
        if len(w) >= 5 and w in hay:
            return True
    return False


def bot_wall(b) -> bool:
    if "sorry/index" in b.url or "/sorry" in b.url:
        return True
    try:
        page_text = (b.page_text() or "").lower()
    except Exception:
        return False
    for marker in BOT_MARKERS:
        if marker == "captcha":
            if any(block in page_text for block in ("solve the captcha to continue", "unusual traffic", "verify you are a human", "cf-turnstile")):
                return True
            continue
        if marker in page_text:
            return True
    return False


def page_signature(page: Dict[str, Any]) -> Tuple:
    elements = page.get("elements") or []
    return (
        page.get("url") or "",
        page.get("scroll_y", 0),
        page.get("at_bottom", False),
        len(elements),
        len(page.get("text") or ""),
        tuple(
            (e.get("index"), e.get("value"), e.get("checked"), e.get("selected"), e.get("expanded"))
            for e in elements
        ),
    )


def site_score(b, goal: str) -> Tuple[float, str]:
    log = logger.get_logger()
    try:
        page_text = b.page_text()
    except Exception as e:
        log.warn(f"site_score: unreadable page ({e})", url=b.url)
        return 0.0, f"unreadable page ({e})"

    low = page_text.lower()
    marker = next((m for m in BOT_MARKERS if m in low), None)
    if marker or len(page_text.strip()) < 50:
        try:
            usable = bool(b.snapshot()["elements"])
        except Exception:
            usable = False
        if not usable:
            note = f"bot wall ({marker!r})" if marker else "no content rendered"
            log.warn(f"site_score rejected: {note}", url=b.url)
            return 0.0, note

    try:
        score = decide.ask_yes_no(
            page_text, b.url, f"Is this page the right site to accomplish this goal: {goal}?"
        )
    except Exception as e:
        log.error(f"site_score judge failed: {e}", url=b.url)
        return 0.0, f"judge failed ({e})"

    if score >= SITE_OK_AT:
        log.site_check(b.url, score, "accepted")
        return score, "accepted"

    note = f"score {score:.2f} below {SITE_OK_AT}"
    log.site_check(b.url, score, note)
    return score, note


def snap_or_recover(b, empties: int) -> Tuple[Optional[Dict[str, Any]], int]:
    log = logger.get_logger()
    try:
        page = b.snapshot()
    except Exception as e:
        if browser_input_interrupted(e):
            raise
        log.warn(f"snapshot exception: {e}", url=b.url)
        page = None

    if page and (page["elements"] or len(page.get("text") or "") >= 80):
        log.page_state(
            page.get("url", b.url),
            page.get("title", ""),
            elements_count=len(page.get("elements", [])),
            sample_elements=[
                {"index": e["index"], "role": e["role"], "label": e["label"][:50]}
                for e in page.get("elements", [])[:8]
            ],
            scroll_percent=page.get("scroll_percent"),
            at_bottom=page.get("at_bottom", False),
            all_elements=[
                {"index": e["index"], "role": e["role"], "label": e["label"], "value": e.get("value"), "checked": e.get("checked"), "selected": e.get("selected")}
                for e in page.get("elements", [])
            ],
        )
        return page, 0

    empties += 1
    log.warn(f"Page empty or non-rendering (count={empties})", url=b.url)
    if empties == 3:
        log.info("page still empty, reloading once", url=b.url)
        try:
            b.reload()
        except Exception as e:
            if browser_input_interrupted(e):
                raise
            log.error(f"reload failed: {e}", url=b.url)
    return None, empties


def find_site(b, goal: str) -> bool:
    """Finds the starting website via direct URL, recipe default, LLM guess, or search engine."""
    log = logger.get_logger()
    log.info(f"Phase 1: Finding entry site for goal: {goal!r}")

    # 1. Direct URL in goal text
    m = re.search(r"https?://[^\s\"']+", goal)
    if m:
        direct = m.group(0).rstrip(",.;:!?)\"'>")
        log.info(f"direct URL found in goal: {direct}")
        try:
            b.goto(direct)
            log.record_action("navigate", name="", tag="page", selector="page", link=direct)
            return True
        except Exception as e:
            if browser_input_interrupted(e):
                raise
            log.warn(f"direct URL navigation failed: {e}", url=direct)

    # 2. Recipe default URL or LLM site guess
    recipe = find_recipe(goal)
    if recipe and recipe.default_start_url:
        log.info(f"recipe start URL found: {recipe.default_start_url}")
        target_domain = urllib.parse.urlparse(recipe.default_start_url).netloc.lower()
        curr_domain = urllib.parse.urlparse(getattr(b, "url", "")).netloc.lower()
        if target_domain and target_domain in curr_domain:
            log.info(f"browser already at recipe domain: {curr_domain}")
            return True
        try:
            b.goto(recipe.default_start_url)
            log.record_action("navigate", name="", tag="page", selector="page", link=recipe.default_start_url)
            return True
        except Exception as e:
            if browser_input_interrupted(e):
                raise
            log.warn(f"recipe URL navigation failed: {e}", url=recipe.default_start_url)
            curr_domain = urllib.parse.urlparse(getattr(b, "url", "")).netloc.lower()
            if target_domain and target_domain in curr_domain:
                log.info(f"browser reached recipe domain despite timeout: {curr_domain}")
                return True
        return True

    guess = ""
    try:
        guess = text.guess_site(goal)
    except Exception as e:
        log.warn(f"site guess skipped: {e}")

    if guess:
        log.info(f"trying site: {guess}")
        try:
            b.goto(guess)
            log.record_action("navigate", name="", tag="page", selector="page", link=guess)
            if guess_is_obvious(goal, guess) and not bot_wall(b):
                log.info(f"site accepted by URL match: {guess}")
                return True
            score, note = site_score(b, goal)
            if score >= SITE_OK_AT:
                log.info(f"site accepted: {guess} (score: {score:.2f})")
                return True
            log.info(f"site rejected ({note}), falling back to search")
        except Exception as e:
            if browser_input_interrupted(e):
                raise
            log.warn(f"site navigation failed: {e}", url=guess)

    # 3. Search query fallback
    query = ""
    try:
        query = text.search_query(goal)
    except Exception as e:
        log.warn(f"search query generation failed: {e}")
    query = query or goal

    for engine, url in search_urls(query):
        log.info(f"Phase 2: {engine} search URL: {url}")
        try:
            b.goto(url)
            log.record_action("navigate", name=f"{engine} search", tag="page", selector="page", link=url)
        except Exception as e:
            if browser_input_interrupted(e):
                raise
            log.warn(f"{engine} navigation failed: {e}", url=url)
            continue
        if bot_wall(b):
            log.warn(f"{engine} served a bot wall, trying next engine", url=b.url)
            continue
        try:
            usable = bool(b.snapshot()["elements"])
        except Exception as e:
            if browser_input_interrupted(e):
                raise
            log.warn(f"{engine} snapshot failed: {e}", url=b.url)
            usable = False
        if usable:
            log.info(f"{engine} results loaded, continuing on: {b.url}")
            return True

    log.error("no search engine returned usable results")
    return False


def act_with_recovery(
    b, op: str, index: Optional[str], label: str, value: Optional[str]
) -> Tuple[Optional[str], Optional[Exception], Optional[str]]:
    """Runs an action; on a stale node reference re-snapshots and re-resolves target."""
    log = logger.get_logger()
    try:
        return b.act(index, op, value), None, index
    except Exception as first:
        if browser_input_interrupted(first) or isinstance(first, RecipientSelectionError):
            return None, first, index
        if op not in ("CLICK", "TYPE_TEXT") or not label:
            return None, first, index
        try:
            b.snapshot()
        except Exception:
            return None, first, index
        fresh = b.find_by_label(label, exclude=index)
        if not fresh:
            return None, first, index
        log.info(f"retry after re-snapshot: [{index}] {label!r} is now [{fresh}]")
        try:
            return b.act(fresh, op, value), None, fresh
        except Exception as second:
            return None, second, fresh


def run_task(b, goal: str, budget: int) -> str:
    """Main task execution loop."""
    log = logger.get_logger()
    log.info(f"Phase 3: Running task loop: {goal!r} (budget={budget} steps)")

    recipe = find_recipe(goal, b.url)
    recipe_context: Dict[str, Any] = {"goal": goal}
    milestones = []
    if recipe:
        log.info(f"Matched workflow recipe: {recipe.name!r}")
        milestones = recipe.build_milestones(goal, recipe_context)
    else:
        log.info("No domain recipe matched; running in pure autonomous mode.")

    task_key = trajectory.normalize_task_key(goal, getattr(b, "url", None))
    cached_trajectory = trajectory.load_trajectory(task_key)
    if cached_trajectory:
        log.info(f"Loaded learned playbook trajectory for task {task_key} ({len(cached_trajectory.get('steps', []))} steps)")
    else:
        log.info(f"No prior playbook trajectory for task {task_key}; recording fresh execution.")

    history: List[Dict[str, Any]] = []
    if hasattr(b, "task_history"):
        b.task_history = history
    empties, fail_key, fail_count = 0, None, 0
    last_move, last_sig, stuck = None, None, 0
    last_simple, simple_stuck = None, 0
    filled_skip: Dict[Tuple[str, str], int] = {}
    banned: Set[Tuple[str, str]] = set()
    skipped: Set[str] = set()
    stall_steps = 0
    last_done_count = -1
    url_history: List[str] = []
    playbook_cursor = 0

    step = 0
    while step < budget:
        step += 1
        if hasattr(b, "task_steps"):
            b.task_steps = step
        try:
            log.info(f"--- Task Step {step}/{budget} ---")
            page, empties = snap_or_recover(b, empties)
            if not page:
                if empties >= 5:
                    msg = "stopped: page shows no content (bot wall?)"
                    log.error(msg, url=b.url, empties=empties)
                    return msg
                continue

            if hasattr(b, "screenshot"):
                try:
                    page_ss = b.screenshot()
                    if page_ss:
                        page["screenshot"] = page_ss
                except Exception as error:
                    if browser_input_interrupted(error):
                        raise
                    pass

            capture_dir = os.environ.get("JEV_CAPTURE_SCREENSHOTS_DIR")
            if capture_dir and step == 1:
                try:
                    os.makedirs(capture_dir, exist_ok=True)
                    if hasattr(b, "screenshot"):
                        b.screenshot(path=os.path.join(capture_dir, "step_000_INITIAL_PAGE.png"))
                except Exception as error:
                    if browser_input_interrupted(error):
                        raise
                    pass

            # Bot wall check
            if bot_wall(b) and (len(page.get("elements") or []) <= 2 or "sorry" in (b.url or "").lower()):
                msg = "stopped: bot wall detected mid-task (captcha or access blocked)"
                log.error(msg, url=b.url)
                return msg

            # Ping-pong loop detector
            curr_url = page.get("url") or b.url
            if not url_history or url_history[-1] != curr_url:
                url_history.append(curr_url)
                if (
                    len(url_history) >= 4
                    and url_history[-1] == url_history[-3]
                    and url_history[-2] == url_history[-4]
                    and url_history[-1] != url_history[-2]
                ):
                    msg = f"stopped: ping-pong loop detected between {url_history[-1]} and {url_history[-2]}"
                    log.error(msg)
                    return msg

            # Check recipe termination conditions
            if recipe_context.get("calendar_selection"):
                selection = recipe_context["calendar_selection"]
                result = interact_calendar(b, page, selection, history)
                if result != "verified":
                    return result
                del recipe_context["calendar_selection"]
                continue

            if recipe and recipe.should_terminate(goal, history, recipe_context):
                log.info("Recipe stop condition satisfied. Finishing task.")
                return "done"

            # Priority 1: Critical Browser Barriers (Unfilled CAPTCHA, Backdrop Overlays, Banner Modals)
            recovery_action = None
            if recipe:
                recovery_action = recipe.handle_recovery(b, page, history, recipe_context)

            # Priority 2: Check Learned Playbook for direct match
            playbook_match_result = None
            suggested = None
            if not recovery_action and cached_trajectory:
                playbook_match_result = trajectory.match_playbook_action(
                    cached_trajectory, playbook_cursor, page.get("elements", []), lookahead=2
                )
                if playbook_match_result:
                    suggested, matched_pb_idx = playbook_match_result

            op, index, move = None, None, {}
            milestone_hint = None
            if recovery_action:
                move = recovery_action
                op = recovery_action["operation"]
                index = recovery_action.get("index")
                log.info(f"Barrier intervention action: {op} [{index}] ({recovery_action.get('reason')})")
            elif suggested:
                move = suggested
                op = suggested["operation"]
                index = suggested.get("index")
                log.info(f"Playbook direct action: {op} [{index}] ({suggested.get('reason')})")
            else:
                # Priority 3: Milestone & Decider Autonomous Execution
                active_milestone = None
                done_count = 0
                for m in milestones:
                    if getattr(m, "id", "") in skipped:
                        continue
                    if m.is_completed:
                        try:
                            finished = bool(m.is_completed(page, history, recipe_context))
                        except Exception as error:
                            if browser_input_interrupted(error):
                                raise
                            finished = False
                        if finished:
                            done_count += 1
                            continue
                        active_milestone = m
                        break
                    active_milestone = m
                    break
                if active_milestone is None and milestones:
                    summary = recipe_context.get("completed_reports", [])
                    failed = sorted({str(m).split("_")[-1] for m in skipped})
                    log.info(f"Task finished with result: done done={summary} failed={failed}")
                    trajectory.save_or_optimize_trajectory(task_key, goal, getattr(log, "recorded_actions", []), status="done")
                    return "done"
                if done_count != last_done_count:
                    last_done_count = done_count
                    stall_steps = 0
                else:
                    stall_steps += 1
                if stall_steps >= 12:
                    mid = getattr(active_milestone, "id", "step")
                    skipped.add(mid)
                    failed_list = recipe_context.setdefault("failed_milestones", [])
                    if mid not in failed_list:
                        failed_list.append(mid)
                    log.warn(f"milestone stalled, skipping: {mid}")
                    stall_steps = 0
                    continue

                if active_milestone:
                    hint_prop = active_milestone.instruction_hint
                    milestone_hint = hint_prop(page, history, recipe_context) if callable(hint_prop) else hint_prop

                if recipe and hasattr(recipe, "_remaining_reports"):
                    rem_list = recipe._remaining_reports(recipe_context)
                    if rem_list:
                        prog_info = f"Progress: {len(recipe_context.get('completed_reports', []))} reports done. Remaining reports to configure: {rem_list}."
                        milestone_hint = f"{milestone_hint or ''} | {prog_info}".strip(" |")

                if cached_trajectory:
                    pb_hint_res = trajectory.find_playbook_step(
                        cached_trajectory, playbook_cursor, page.get("elements", []), lookahead=2
                    )
                    if pb_hint_res:
                        pb_hint, _ = pb_hint_res
                        pb_target = pb_hint.get("target") or {}
                        matched_idx = pb_hint.get("matched_index")
                        p_name = pb_target.get("name") or pb_target.get("tag") or ""
                        pb_desc = f"Learned playbook next step: {pb_hint.get('type')} on '{p_name}'"
                        if matched_idx:
                            pb_desc += f" (element [{matched_idx}])"
                        milestone_hint = f"{milestone_hint or ''} | {pb_desc}".strip(" |")

                try:
                    decide_page = page
                    if banned:
                        bset = {i for (u, i) in banned if u == curr_url}
                        if bset:
                            decide_page = dict(page, elements=[e for e in (page.get("elements") or []) if str(e.get("index")) not in bset])
                    move = decide.choose(decide_page, goal, history, extra_hint=milestone_hint, screenshot_data_url=page.get("screenshot"))
                    op, index = move["operation"], move.get("index")
                    thought = move.get("thought")
                    if thought:
                        log.info(f"decision thought: {thought}")
                except Exception as e:
                    msg = f"stopped: decide failed ({e})"
                    log.error(msg, url=b.url)
                    return msg

            target_el = b.get_element(index) if index else None
            label = " ".join(((target_el or {}).get("label") or "").split())

            if op == "CLICK" and target_el is not None and target_el.get("role") in ("option", "menuitem"):
                try:
                    fresh = b.snapshot(retries=1)
                    want_lbl = " ".join((label or "").lower().split())
                    fresh_el = next((e for e in fresh.get("elements", []) if str(e.get("index")) == str(index)), None)
                    got_lbl = " ".join(((fresh_el or {}).get("label") or "").lower().split())
                    if fresh_el is None or got_lbl != want_lbl:
                        alt = next((e for e in fresh.get("elements", []) if e.get("role") in ("option", "menuitem") and " ".join((e.get("label") or "").lower().split()) == want_lbl), None)
                        if alt is not None:
                            index = str(alt.get("index"))
                            target_el = b.get_element(index)
                            label = " ".join(((target_el or {}).get("label") or "").split())
                            log.info(f"re-resolved stale option [{index}] by live label")
                        else:
                            log.warn(f"option [{index}] gone from live page, skipping")
                            history.append({"action": f"skipped stale [{index}]", "kind": "skip", "text": ""})
                            continue
                except Exception as error:
                    if browser_input_interrupted(error):
                        raise
                    pass

            log.decision(op, index, element=target_el)
            if op == "DONE":
                remaining = recipe._remaining_reports(recipe_context) if recipe and hasattr(recipe, "_remaining_reports") else None
                if remaining:
                    log.warn(f"Decider signaled DONE prematurely; {len(remaining)} reports still pending: {remaining}. Continuing execution.")
                    history.append({
                        "action": f"Premature DONE rejected: reports {remaining} are not completed yet. Fill the remaining fields on the current form first, or select the next report.",
                        "kind": "skip",
                        "text": "",
                    })
                    continue
                else:
                    log.info("Goal reported DONE by the decider, run finished")
                    capture_dir = os.environ.get("JEV_CAPTURE_SCREENSHOTS_DIR")
                    if capture_dir:
                        try:
                            os.makedirs(capture_dir, exist_ok=True)
                            if hasattr(b, "screenshot"):
                                b.screenshot(path=os.path.join(capture_dir, f"step_{step:03d}_DONE_FINAL_STATE.png"))
                        except Exception as error:
                            if browser_input_interrupted(error):
                                raise
                            pass
                    trajectory.save_or_optimize_trajectory(task_key, goal, getattr(log, "recorded_actions", []), status="done")
                    return "done"

            if op == "NEEDS_INPUT":
                reason = str(move.get("reason") or "human input required in the browser")
                log.warn(f"Task paused: {reason}")
                return f"needs_input: {reason}"

            if op == "BACK":
                log.act_start(op, None)
                done = b.act(None, "BACK")
                history.append({"action": "went back to previous page", "kind": "BACK", "text": ""})
                log.act_success(done, page_url=b.url)
                last_move, last_sig, stuck = (op, None), None, 0
                continue

            sig = page_signature(page)
            target_node = (target_el or {}).get("node")
            move_key = (op, target_node if target_node is not None else index, sig[0])
            simple_key = (op, index)
            if op in ("CLICK", "TYPE_TEXT") and simple_key == last_simple:
                simple_stuck += 1
                if simple_stuck >= 3:
                    msg = f"stopped: {op} [{index}] repeated with no progress"
                    log.error(msg, element=target_el, url=b.url)
                    return msg
            else:
                simple_stuck = 0
            last_simple = simple_key
            if op in ("CLICK", "TYPE_TEXT", "SCROLL", "SCROLL_TOP", "SCROLL_UP", "WAIT", "BACK") and (move_key == last_move or (op, index) == last_move):
                if sig == last_sig:
                    stuck += 1
                    max_stuck = 300 if (op == "WAIT" and recovery_action) else (4 if op == "WAIT" else (4 if op == "SCROLL" and not page.get("at_bottom") else 3))
                    log.warn(f"repeat guard: {op} [{index}] again with no page change ({stuck}/{max_stuck})")
                    if stuck >= max_stuck:
                        msg = f"stopped: {op} [{index}] repeated with no page change"
                        log.error(msg, element=target_el, url=b.url)
                        return msg
                else:
                    stuck = 0
            else:
                stuck = 0

            last_move, last_sig = move_key, sig

            value = None
            if op == "TYPE_TEXT" and ((target_el or {}).get("isPassword") or (target_el or {}).get("input_type") == "password" or any(word in label.lower() for word in ("password", "pwd"))):
                filler = getattr(b, "fill_saved_secret", None)
                if not callable(filler):
                    return "needs_input: This browser requires manual password entry."
                try:
                    outcome = filler(str(index))
                except Exception as error:
                    if browser_input_interrupted(error):
                        raise
                    stage = error.stage if isinstance(error, SecretEntryError) else "entry"
                    reason = secret_failure_reason(error)
                    log.warn(f"Saved-secret entry stopped: stage={stage} reason={reason}")
                    return f"needs_input: Saved-password entry stopped at {stage}: {reason}"
                history.append({"action": f"Saved-secret entry for [{index}] completed", "kind": "SECRET_INPUT", "text": ""})
                log.info(f"Saved-secret entry: [{index}] {outcome}")
                continue
            if op in ("TYPE_TEXT", "SET_DATE"):
                value = recovery_action.get("value") if recovery_action else None
                if recipe and any(key in label.lower() for key in ("password", "pwd")):
                    report_field = any(key in label.lower() for key in ("extraction", "report", "confirm", "re-enter")) or recipe.name == "cams_mailback"
                    value = recipe.field_hints.get("report_password" if report_field else "password")
                if not value and move.get("value"):
                    value = move.get("value")
                if not value:
                    try:
                        role = (target_el or {}).get("role")
                        field_hints = recipe.field_hints if recipe else None
                        value = text.field_text(
                            goal,
                            label,
                            b.title,
                            role=role,
                            section_context=milestone_hint,
                            field_hints=field_hints,
                        )
                        log.info(f"Text generated for [{index}] ({label!r}): {value!r}")
                    except Exception as e:
                        log.warn(f"text generation failed for [{index}], skipping: {e}")
                        history.append({"action": f"skipped [{index}]", "kind": "skip", "text": ""})
                        continue
                if value:
                    clean_val = str(value).strip().rstrip(".,;!?")
                    value = clean_val
                    if op == "SET_DATE" or ((target_el or {}).get("readonly") and parse_date(value) is not None):
                        try:
                            recipe_context["calendar_selection"] = start_calendar(page, target_el or {}, value)
                        except ValueError as error:
                            return f"needs_input: {error}"
                        result = interact_calendar(b, page, recipe_context["calendar_selection"], history)
                        if result != "verified":
                            return result
                        del recipe_context["calendar_selection"]
                        last_simple, simple_stuck = None, 0
                        continue
                    cur_held = str((target_el or {}).get("value") or "").strip()
                    is_pwd = (
                        (target_el or {}).get("type") == "password"
                        or "••" in cur_held
                        or "password" in label.lower()
                    )
                    if (cur_held and cur_held.lower() == clean_val.lower()) or (is_pwd and cur_held):
                        log.info(f"[{index}] already holds requested value, skipping")
                        history.append({"action": f"[{index}] {label!r} already filled", "kind": "skip", "text": ""})
                        skip_key = (curr_url, str(index))
                        filled_skip[skip_key] = filled_skip.get(skip_key, 0) + 1
                        if filled_skip[skip_key] >= 2:
                            banned.add(skip_key)
                        if playbook_match_result and matched_pb_idx is not None:
                            playbook_cursor = matched_pb_idx + 1
                            log.info(f"Playbook cursor advanced to step #{playbook_cursor} (field already filled)")
                        last_simple = None
                        simple_stuck = 0
                        continue

            log.act_start(op, index, element=target_el, text=value)
            done, err, index = act_with_recovery(b, op, index, label, value)

            if err:
                if browser_input_interrupted(err):
                    raise err
                if isinstance(err, RecipientSelectionError):
                    log.warn(str(err))
                    return f"needs_input: {err}"
                fail_count = fail_count + 1 if (op, index) == fail_key else 1
                fail_key = (op, index)
                target_el = b.get_element(index)
                log.act_fail(err, op, index, element=target_el, fail_count=fail_count)
                if fail_count >= 3:
                    msg = f"stopped: [{index}] stays unusable (covered, gone, or disabled)"
                    log.error(msg, element=target_el, url=b.url)
                    return msg
                history.append({"action": f"failed to {op.lower()} [{index}] {label!r}", "kind": "fail", "text": ""})
                time.sleep(0.25)
                continue

            fail_key, fail_count = None, 0
            record_success = getattr(recipe, "record_action_success", None)
            if recovery_action and callable(record_success):
                record_success(move, recipe_context)
            history.append({"action": f"{done} {label!r}", "kind": op, "text": value or ""})
            log.act_success(done, page_url=b.url)

            capture_dir = os.environ.get("JEV_CAPTURE_SCREENSHOTS_DIR")
            if capture_dir:
                try:
                    os.makedirs(capture_dir, exist_ok=True)
                    clean_lbl = re.sub(r"[^a-zA-Z0-9_]+", "_", label or op)[:30].strip("_")
                    img_name = f"step_{step:03d}_{op}_{clean_lbl}.png"
                    if hasattr(b, "screenshot"):
                        b.screenshot(path=os.path.join(capture_dir, img_name))
                except Exception as error:
                    if browser_input_interrupted(error):
                        raise
                    pass

            if playbook_match_result:
                playbook_cursor = matched_pb_idx + 1
                log.info(f"Playbook cursor advanced to step #{playbook_cursor}")

            if op == "CLICK":
                href_link = (target_el or {}).get("href")
                tag_name = (target_el or {}).get("role") or "button"
                log.record_action("click", name=label, tag=tag_name, selector=f"[{index}]" if index else "", link=href_link)
            elif op == "TYPE_TEXT":
                tag_name = (target_el or {}).get("role") or "input"
                clean_val = None if "captcha" in (label or "").lower() else value
                log.record_action("type", name=label or (value or ""), tag=tag_name, selector=f"[{index}]" if index else "", value=clean_val)
            elif op == "BACK":
                log.record_action("navigate", name="back", tag="page", selector="page", link=getattr(b, "url", None))

            if (move.get("needs_confirmation") or op in ("CLICK", "TYPE_TEXT")) and op not in ("WAIT", "BACK", "DONE", "SCROLL", "SCROLL_TOP", "SCROLL_UP"):
                try:
                    if b.url != page.get("url"):
                        log.act_success(f"navigated to {b.url}", page_url=b.url)
                        log.record_action("navigate", name="", tag="page", selector="page", link=b.url)
                    else:
                        next_page = b.snapshot(retries=1)
                        verification = llm_decider.verify_action_result(goal, move, page, next_page)
                        if not verification.get("confirmed"):
                            log.warn(f"action confirmation notice: {verification.get('reason')}")
                except Exception as error:
                    if browser_input_interrupted(error):
                        raise
                    pass

        except Exception as error:
            if not browser_input_interrupted(error):
                raise
            waiter = getattr(b, "wait_until_resumed", None)
            if not callable(waiter) or not waiter():
                message = "needs_input: Browser Use was paused or interrupted. The resume window expired or this browser cannot retain the task."
                log.warn(message)
                return message
            history.append({"action": "Browser Use resumed after an interrupted operation whose outcome is uncertain. Re-read the current page; do not repeat already satisfied actions.", "kind": "interrupted", "text": ""})
            last_move, last_sig, stuck = None, None, 0
            last_simple, simple_stuck = None, 0
            step -= 1

    msg = "stopped: step budget reached"
    log.warn(msg, budget=budget)
    return msg


def main():
    load_env()
    if len(sys.argv) < 2:
        print('usage: python loop.py "your goal"')
        raise SystemExit(2)
    goal = sys.argv[1]
    log = logger.get_logger(goal)
    env_budget = int(os.environ.get("MAX_TASK_STEPS", "35"))
    matched_recipe = find_recipe(goal)
    task_budget = matched_recipe.get_budget(goal, env_budget) if matched_recipe else env_budget
    b = browser_factory.get_browser()
    started = time.perf_counter()
    try:
        mode = browser_factory.describe()
        log.info(f"Browser mode: {mode} ({type(b).__name__})")
        log.info(f"Decider engine: {os.environ.get('JEV_DECIDER_ENGINE', 'systemone').strip().lower()}")
        if not find_site(b, goal):
            log.error("could not reach a usable site")
            return
        result = run_task(b, goal, task_budget)
        elapsed = round(time.perf_counter() - started, 1)
        log.info(f"Task finished with result: {result} in {elapsed}s")
    except Exception as e:
        log.error(f"Unhandled fatal error in run: {e}", exc=str(e))
    finally:
        b.close()
        log.close()


if __name__ == "__main__":
    main()
