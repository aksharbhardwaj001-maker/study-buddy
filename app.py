import os
import uuid

from flask import Flask, render_template, request, redirect, url_for, session

import study_buddy as sb

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-secret-change-me-for-local-only")

# Server-side store for data too big / sensitive for a cookie (transcript text, questions).
# Keyed by a random id we stash in the (cookie) session. Fine for a single-user / small-scale app.
STORE = {}


def get_bucket():
    sid = session.get("sid")
    if not sid or sid not in STORE:
        sid = str(uuid.uuid4())
        session["sid"] = sid
        STORE[sid] = {}
    return STORE[sid]


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/fetch", methods=["POST"])
def fetch():
    url = request.form.get("url", "").strip()
    bucket = get_bucket()

    try:
        video_id = sb.extract_video_id(url)
        transcript, total_seconds = sb.fetch_transcript(video_id)
    except (ValueError, RuntimeError) as e:
        return render_template("index.html", error=str(e), prefill=url)

    memory = sb.load_memory()
    video_memory = memory.get(video_id, {"watched_seconds": 0, "total_seconds": total_seconds, "scores": []})
    old_checkpoint = video_memory["watched_seconds"]

    bucket["video_id"] = video_id
    bucket["transcript"] = transcript
    bucket["total_seconds"] = total_seconds
    bucket["old_checkpoint"] = old_checkpoint

    percent = round(old_checkpoint / total_seconds * 100) if total_seconds else 0

    return render_template(
        "watched.html",
        old_checkpoint_fmt=sb.format_seconds(old_checkpoint),
        total_seconds_fmt=sb.format_seconds(total_seconds),
        percent=percent,
    )


@app.route("/generate", methods=["POST"])
def generate():
    bucket = get_bucket()
    if "transcript" not in bucket:
        return redirect(url_for("index"))

    watched_text = request.form.get("watched", "").strip()
    live_elapsed = request.form.get("live_elapsed", "").strip()
    old_checkpoint = bucket["old_checkpoint"]
    total_seconds = bucket["total_seconds"]

    try:
        if live_elapsed:
            # Live-timer mode: mirrors the original script's "press Enter, watch, press Enter"
            # flow - elapsed seconds are added on top of the last checkpoint.
            elapsed_seconds = float(live_elapsed)
            if elapsed_seconds <= 0:
                raise ValueError("Timer reported no elapsed time.")
            new_checkpoint = min(old_checkpoint + elapsed_seconds, total_seconds)
        elif watched_text:
            new_checkpoint = sb.compute_new_checkpoint(old_checkpoint, total_seconds, watched_text)
        else:
            new_checkpoint = old_checkpoint
    except ValueError:
        percent = round(old_checkpoint / total_seconds * 100) if total_seconds else 0
        return render_template(
            "watched.html",
            old_checkpoint_fmt=sb.format_seconds(old_checkpoint),
            total_seconds_fmt=sb.format_seconds(total_seconds),
            percent=percent,
            error="Couldn't read that. Use a timestamp like 12:30, or a plain number of minutes.",
        )

    if new_checkpoint <= old_checkpoint:
        percent = round(old_checkpoint / total_seconds * 100) if total_seconds else 0
        return render_template(
            "watched.html",
            old_checkpoint_fmt=sb.format_seconds(old_checkpoint),
            total_seconds_fmt=sb.format_seconds(total_seconds),
            percent=percent,
            error="That's not further than where you already left off. Enter a later timestamp or a positive number of minutes.",
        )

    segment_text = sb.get_segment_between(bucket["transcript"], old_checkpoint, new_checkpoint)
    if not segment_text.strip():
        percent = round(old_checkpoint / total_seconds * 100) if total_seconds else 0
        return render_template(
            "watched.html",
            old_checkpoint_fmt=sb.format_seconds(old_checkpoint),
            total_seconds_fmt=sb.format_seconds(total_seconds),
            percent=percent,
            error="No captions found in that range. Try a bit more time.",
        )

    try:
        result = sb.generate_recap_and_quiz(segment_text)
    except RuntimeError as e:
        percent = round(old_checkpoint / total_seconds * 100) if total_seconds else 0
        return render_template(
            "watched.html",
            old_checkpoint_fmt=sb.format_seconds(old_checkpoint),
            total_seconds_fmt=sb.format_seconds(total_seconds),
            percent=percent,
            error=str(e),
        )

    bucket["new_checkpoint"] = new_checkpoint
    bucket["questions"] = result["questions"]
    bucket["recap"] = result["recap"]

    new_percent = round(new_checkpoint / total_seconds * 100) if total_seconds else 0

    return render_template(
        "quiz.html",
        recap=result["recap"],
        questions=result["questions"],
        new_percent=new_percent,
        gained_fmt=sb.format_seconds(new_checkpoint - old_checkpoint),
    )


@app.route("/grade", methods=["POST"])
def grade():
    bucket = get_bucket()
    if "questions" not in bucket:
        return redirect(url_for("index"))

    questions = bucket["questions"]
    user_answers = [request.form.get(f"answer_{i}", "") for i in range(len(questions))]

    try:
        graded, questions, user_answers = sb.grade_answers(questions, user_answers)
    except RuntimeError as e:
        return render_template(
            "quiz.html",
            recap=bucket["recap"],
            questions=questions,
            new_percent=round(bucket["new_checkpoint"] / bucket["total_seconds"] * 100),
            gained_fmt=sb.format_seconds(bucket["new_checkpoint"] - bucket["old_checkpoint"]),
            error=str(e),
        )

    correct_count = sum(1 for g in graded if g["correct"])
    score_percent = round((correct_count / len(graded)) * 100)

    video_id = bucket["video_id"]
    total_seconds = bucket["total_seconds"]
    new_checkpoint = bucket["new_checkpoint"]

    memory = sb.load_memory()
    video_memory = memory.get(video_id, {"watched_seconds": 0, "total_seconds": total_seconds, "scores": []})
    video_memory["watched_seconds"] = new_checkpoint
    video_memory["total_seconds"] = total_seconds
    video_memory["scores"].append(score_percent)
    memory[video_id] = video_memory
    sb.save_memory(memory)

    completion_percent = round((new_checkpoint / total_seconds) * 100)
    avg_score = round(sum(video_memory["scores"]) / len(video_memory["scores"]))

    rows = list(zip(questions, user_answers, graded))

    # Clear this video's working data so a fresh /fetch starts clean.
    for key in ("transcript", "questions", "recap", "new_checkpoint"):
        bucket.pop(key, None)

    return render_template(
        "results.html",
        rows=rows,
        correct_count=correct_count,
        total_count=len(graded),
        score_percent=score_percent,
        completion_percent=completion_percent,
        avg_score=avg_score,
        watched_fmt=sb.format_seconds(new_checkpoint),
        total_fmt=sb.format_seconds(total_seconds),
    )


if __name__ == "__main__":
    app.run(debug=True)