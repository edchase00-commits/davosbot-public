"""Workout tool helpers."""

import json
import math
import sqlite3
from contextlib import closing
from typing import Callable

from .config import BOT_DB_PATH
from .db import connect_bot_db


def workout_log_tool(
    args: dict,
    sender: str,
    db_path: str = BOT_DB_PATH,
    connect_fn: Callable = connect_bot_db,
) -> str:
    """Store rep-based workout_entries; callers supply canonical pounds (lbs).

    Explicit source kg loads must be converted before this boundary and retained
    in notes, alongside effort/per-hand context. Timed holds are not rep sets.
    """
    if not isinstance(args, dict):
        return "Invalid workout — provide an object with exercise_name and sets."
    exercise = args.get("exercise_name", "")
    if not isinstance(exercise, str) or len(exercise) > 120:
        return "Invalid exercise name — use text up to 120 characters."
    exercise = exercise.strip()
    if not exercise:
        return "Missing exercise name."
    sets = args.get("sets", [])
    if isinstance(sets, list) and not sets:
        return "Missing sets — provide at least one {weight, reps} object."
    if not isinstance(sets, list) or len(sets) > 30:
        return "Invalid sets — provide 1 to 30 {weight, reps} objects."
    cleaned_sets = []
    for index, item in enumerate(sets, 1):
        if not isinstance(item, dict) or set(item) != {"weight", "reps"}:
            return f"Invalid set {index} — provide weight and reps only; use weight 0 for bodyweight."
        weight, reps = item["weight"], item["reps"]
        # Check type and bounds before conversion (including arbitrarily large
        # integers), and reject bool, which otherwise behaves like an integer.
        if (type(weight) not in (int, float) or not 0 <= weight <= 5000
                or not math.isfinite(weight)):
            return f"Invalid set {index} — weight must be a finite number from 0 to 5000 lbs."
        if (type(reps) not in (int, float) or not 1 <= reps <= 1000
                or not math.isfinite(reps) or reps != int(reps)):
            return f"Invalid set {index} — reps must be a whole number from 1 to 1000."
        cleaned_sets.append({"weight": float(weight), "reps": int(reps)})
    muscle = args.get("muscle_group", "")
    if not isinstance(muscle, str) or len(muscle) > 40:
        return "Invalid muscle group — use text up to 40 characters."
    notes = args.get("notes", "")
    if not isinstance(notes, str) or len(notes) > 500:
        return "Invalid notes — use text up to 500 characters."
    if not muscle:
        from .commands import _guess_muscle_group
        muscle = _guess_muscle_group(exercise)
    try:
        with connect_fn(db_path) as conn:
            conn.execute(
                "INSERT INTO workout_entries (sender, muscle_group, exercise_name, sets_json, notes) "
                "VALUES (?, ?, ?, ?, ?)",
                (sender, muscle, exercise, json.dumps(cleaned_sets), notes),
            )
    except Exception as e:
        return f"Workout log failed: {e}"
    max_w = max(s["weight"] for s in cleaned_sets)
    total_reps = sum(s["reps"] for s in cleaned_sets)
    if max_w:
        return f"Logged — {exercise}: {len(sets)} sets, {total_reps} total reps, up to {format_weight(max_w)}lbs"
    return f"Logged — {exercise}: {len(sets)} sets, {total_reps} total reps (bodyweight)"


def workout_number(value, default=0):
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def workout_int(value, default=0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def format_weight(value: float) -> str:
    return f"{value:g}"


def legacy_workout_sets(args: dict) -> list[dict]:
    sets_count = max(workout_int(args.get("sets"), 0), 0)
    reps = workout_int(args.get("reps"), 0)
    weight = workout_number(args.get("weight_lbs"), 0)
    if sets_count <= 0 and (reps or weight):
        sets_count = 1
    if sets_count <= 0:
        return []
    return [{"weight": weight, "reps": reps} for _ in range(sets_count)]


def summarize_workout_sets(sets: list[dict]) -> str:
    if not sets:
        return "logged"
    cleaned = []
    for item in sets:
        if not isinstance(item, dict):
            continue
        cleaned.append({
            "weight": workout_number(item.get("weight"), 0),
            "reps": workout_int(item.get("reps"), 0),
        })
    if not cleaned:
        return "logged"
    first = cleaned[0]
    same = all(s == first for s in cleaned)
    if same and len(cleaned) > 1:
        reps = first["reps"]
        weight = first["weight"]
        if weight:
            return f"{len(cleaned)}x{reps} @ {format_weight(weight)}lbs"
        if reps:
            return f"{len(cleaned)}x{reps} bodyweight"
        return f"{len(cleaned)} sets"
    parts = []
    for item in cleaned[:6]:
        reps = item["reps"]
        weight = item["weight"]
        if weight and reps:
            parts.append(f"{format_weight(weight)}lbs x{reps}")
        elif reps:
            parts.append(f"{reps} reps")
        elif weight:
            parts.append(f"{format_weight(weight)}lbs")
    return ", ".join(parts) if parts else f"{len(cleaned)} sets"


def format_canonical_workout(date: str, exercise: str, sets_raw: str, notes: str = "") -> str:
    try:
        sets = json.loads(sets_raw or "[]")
    except Exception:
        sets = []
    line = f"{date[:10]} - {exercise} {summarize_workout_sets(sets)}".strip()
    if notes:
        line += f" {notes}"
    return line.strip()


def legacy_workout_fallback_allowed(sender: str) -> bool:
    if not sender:
        return True
    try:
        from .permissions import is_owner
        return is_owner(sender)
    except Exception:
        return False


def log_workout(args: dict, sender: str = "", db_path: str = BOT_DB_PATH) -> str:
    exercise = str(args.get("exercise", "")).strip()
    if not exercise:
        return "Missing exercise name."
    sets = legacy_workout_sets(args)
    try:
        from .commands import _guess_muscle_group
        muscle_group = _guess_muscle_group(exercise)
    except Exception:
        muscle_group = "other"
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute(
            "INSERT INTO workout_entries (sender, muscle_group, exercise_name, sets_json, notes) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                sender,
                muscle_group,
                exercise,
                json.dumps(sets),
                args.get("notes", ""),
            ),
        )
        conn.commit()
    parts = [exercise]
    if args.get("sets") and args.get("reps"):
        parts.append(f"{args['sets']}x{args['reps']}")
    if args.get("weight_lbs"):
        parts.append(f"@ {args['weight_lbs']}lbs")
    return f"Logged: {' '.join(parts)}"


def query_legacy_workout(args: dict, db_path: str = BOT_DB_PATH) -> str:
    query_type = args["query_type"]
    with closing(sqlite3.connect(db_path)) as conn:
        if query_type == "recent":
            rows = conn.execute(
                "SELECT exercise, sets, reps, weight_lbs, notes, ts "
                "FROM workouts ORDER BY id DESC LIMIT 20"
            ).fetchall()
            if not rows:
                return "No workouts logged yet."
            return "\n".join(
                f"{r[5][:10]} — {r[0]} {r[1]}x{r[2]} @ {r[3]}lbs {r[4] or ''}".strip()
                for r in rows
            )

        elif query_type == "exercise":
            exercise = args.get("exercise", "")
            rows = conn.execute(
                "SELECT sets, reps, weight_lbs, notes, ts FROM workouts "
                "WHERE LOWER(exercise) = LOWER(?) ORDER BY id DESC LIMIT 20",
                (exercise,),
            ).fetchall()
            if not rows:
                return f"No logs found for '{exercise}'."
            return "\n".join(
                f"{r[4][:10]} — {r[0]}x{r[1]} @ {r[2]}lbs {r[3] or ''}".strip()
                for r in rows
            )

        elif query_type == "summary":
            rows = conn.execute(
                "SELECT exercise, COUNT(*) as sessions, MAX(weight_lbs) as max_weight "
                "FROM workouts GROUP BY LOWER(exercise) ORDER BY sessions DESC"
            ).fetchall()
            if not rows:
                return "No workouts logged yet."
            return "\n".join(f"{r[0]}: {r[1]} sessions, max {r[2]}lbs" for r in rows)

    return "Unknown query type."


def query_canonical_workout(conn: sqlite3.Connection, args: dict, sender: str) -> str | None:
    query_type = args["query_type"]
    where = "WHERE sender = ?"
    params: list = [sender]
    if not sender:
        where = ""
        params = []

    if query_type == "recent":
        rows = conn.execute(
            "SELECT date, exercise_name, sets_json, notes FROM workout_entries "
            f"{where} ORDER BY id DESC LIMIT 20",
            tuple(params),
        ).fetchall()
        if not rows:
            return None
        return "\n".join(format_canonical_workout(*row) for row in rows)

    if query_type == "exercise":
        exercise = str(args.get("exercise", "")).strip()
        if not exercise:
            return "Missing exercise name."
        clause = f"{where} AND LOWER(exercise_name) = LOWER(?)" if where else "WHERE LOWER(exercise_name) = LOWER(?)"
        rows = conn.execute(
            "SELECT date, exercise_name, sets_json, notes FROM workout_entries "
            f"{clause} ORDER BY id DESC LIMIT 20",
            tuple(params + [exercise]),
        ).fetchall()
        if not rows:
            return None
        return "\n".join(format_canonical_workout(*row) for row in rows)

    if query_type == "summary":
        rows = conn.execute(
            "SELECT exercise_name, sets_json FROM workout_entries "
            f"{where} ORDER BY id DESC",
            tuple(params),
        ).fetchall()
        if not rows:
            return None
        summary: dict[str, dict] = {}
        for exercise, sets_raw in rows:
            key = exercise.lower()
            item = summary.setdefault(key, {"exercise": exercise, "sessions": 0, "max_weight": 0.0})
            item["sessions"] += 1
            try:
                sets = json.loads(sets_raw or "[]")
            except Exception:
                sets = []
            for entry in sets:
                if isinstance(entry, dict):
                    item["max_weight"] = max(item["max_weight"], workout_number(entry.get("weight"), 0))
        ordered = sorted(summary.values(), key=lambda item: (-item["sessions"], item["exercise"].lower()))
        lines = []
        for item in ordered:
            max_weight = item["max_weight"]
            if max_weight:
                lines.append(f"{item['exercise']}: {item['sessions']} sessions, max {format_weight(max_weight)}lbs")
            else:
                lines.append(f"{item['exercise']}: {item['sessions']} sessions")
        return "\n".join(lines)

    return "Unknown query type."


def query_workout(args: dict, sender: str = "", db_path: str = BOT_DB_PATH) -> str:
    with closing(sqlite3.connect(db_path)) as conn:
        canonical = query_canonical_workout(conn, args, sender)
    if canonical is not None:
        return canonical
    if not legacy_workout_fallback_allowed(sender):
        return "No workouts logged yet."
    return query_legacy_workout(args, db_path=db_path)
