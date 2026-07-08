import json
import os
import re
import time
from openai import OpenAI, RateLimitError

from youtube_transcript_api import YouTubeTranscriptApi
try:
    from youtube_transcript_api import TranscriptsDisabled, NoTranscriptFound
except ImportError:
    TranscriptsDisabled = NoTranscriptFound = Exception


# ----------------------------------------------------------------------
# SETTINGS
# ----------------------------------------------------------------------
API_KEY = os.environ.get("OPENROUTER_API_KEY")
MODEL_NAME = "openrouter/free"
NUM_QUIZ_QUESTIONS = 5
MAX_RETRIES = 2
MEMORY_FILE = os.path.join(os.path.dirname(__file__), "video_memory.json")

_client = None


def get_client():
    """Lazily create the OpenAI client so a missing key fails with a clear error,
    only when it's actually needed, not at import time."""
    global _client
    if not API_KEY:
        raise RuntimeError(
            "No API key found. Set the OPENROUTER_API_KEY environment variable before starting the server."
        )
    if _client is None:
        _client = OpenAI(api_key=API_KEY, base_url="https://openrouter.ai/api/v1")
    return _client


# ----------------------------------------------------------------------
# Memory (per-video watch progress + quiz score history)
# ----------------------------------------------------------------------
def load_memory():
    if os.path.exists(MEMORY_FILE):
        with open(MEMORY_FILE, "r") as f:
            return json.load(f)
    return {}


def save_memory(memory):
    with open(MEMORY_FILE, "w") as f:
        json.dump(memory, f, indent=2)


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
def extract_video_id(url):
    match = re.search(r"(?:v=|youtu\.be/)([A-Za-z0-9_-]{11})", url)
    if not match:
        raise ValueError("Couldn't find a valid YouTube video ID in that link. Check the URL and try again.")
    return match.group(1)


def format_seconds(s):
    """Turns 480 into '8:00', 3661 into '1:01:01' - for friendly display."""
    s = int(s)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{sec:02d}"
    return f"{m}:{sec:02d}"


def extract_json(text):
    text = text.strip()
    text = re.sub(r"^```(json)?", "", text)
    text = re.sub(r"```$", "", text)
    text = text.strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("No JSON object found in the model's reply.")
    return json.loads(text[start:end + 1])


def call_qwen_for_json(prompt, retries=MAX_RETRIES, rate_limit_retries=5):
    """
    Calls the AI and parses JSON from the reply.
    Handles two different problems automatically:
    1. Rate limits (429) - free-tier models occasionally get briefly overloaded.
       We wait and retry with increasing delays instead of crashing.
    2. Malformed JSON - if the model's reply isn't valid JSON, we retry the request.
    """
    client = get_client()
    last_error = None

    for rl_attempt in range(rate_limit_retries):
        try:
            for attempt in range(retries + 1):
                response = client.chat.completions.create(
                    model=MODEL_NAME,
                    messages=[{"role": "user", "content": prompt}],
                )
                raw_reply = response.choices[0].message.content
                try:
                    return extract_json(raw_reply)
                except (json.JSONDecodeError, ValueError) as e:
                    last_error = e
                    continue
            raise RuntimeError(f"The model didn't return valid JSON after {retries + 1} tries. Last error: {last_error}")

        except RateLimitError as e:
            if rl_attempt < rate_limit_retries - 1:
                wait_time = 10 * (rl_attempt + 1)  # 10s, 20s, 30s, 40s...
                time.sleep(wait_time)
                continue
            raise RuntimeError(
                "Still rate-limited after several automatic retries. "
                "The free tier may be under heavy load right now - wait a minute and try again, "
                "or add a small amount of credit to your OpenRouter account to skip this limit."
            ) from e


# ----------------------------------------------------------------------
# Transcript fetching
# ----------------------------------------------------------------------
def fetch_transcript(video_id):
    """Returns (transcript_list, total_seconds) for the whole video."""
    try:
        transcript = YouTubeTranscriptApi.get_transcript(video_id)
    except AttributeError:
        api = YouTubeTranscriptApi()
        fetched = api.fetch(video_id)
        transcript = [{"text": s.text, "start": s.start, "duration": s.duration} for s in fetched]
    except TranscriptsDisabled:
        raise RuntimeError("This video has captions disabled, so I can't read it. Try a different video.")
    except NoTranscriptFound:
        raise RuntimeError("No transcript/captions found for this video. Try a different video.")

    if not transcript:
        raise RuntimeError("This video's transcript is empty. Try a different video.")

    last_entry = transcript[-1]
    total_seconds = int(last_entry["start"] + last_entry["duration"])
    return transcript, total_seconds


def get_segment_between(transcript, start_seconds, end_seconds):
    """Grabs only the NEW captions between two timestamps (today's portion)."""
    return " ".join(
        entry["text"] for entry in transcript
        if start_seconds <= entry["start"] < end_seconds
    )


# ----------------------------------------------------------------------
# Recap + quiz generation
# ----------------------------------------------------------------------
def generate_recap_and_quiz(segment_text):
    prompt = f"""
    Here is a transcript of part of an educational video (today's new portion only):
    ---
    {segment_text}
    ---

    Based ONLY on this content:
    1. Write a short recap (4-6 sentences) summarizing the key points.
    2. Write exactly {NUM_QUIZ_QUESTIONS} quiz questions to test understanding,
       each with one clear, unambiguous correct answer.

    Reply ONLY with valid JSON, no extra text, no markdown formatting, in this exact shape:
    {{
      "recap": "your recap here",
      "questions": [
        {{"question": "...", "answer": "..."}}
      ]
    }}
    The "questions" list must contain exactly {NUM_QUIZ_QUESTIONS} items.
    """
    result = call_qwen_for_json(prompt)
    if "recap" not in result or "questions" not in result:
        raise RuntimeError("The model's response was missing expected fields. Try running again.")
    return result


def grade_answers(questions, user_answers):
    pair_count = min(len(questions), len(user_answers))
    questions = questions[:pair_count]
    user_answers = user_answers[:pair_count]

    grading_prompt = "Grade these quiz answers. For each, say whether it's correct and give a one-line reason.\n\n"
    for i, q in enumerate(questions):
        grading_prompt += f"Q{i+1}: {q['question']}\n"
        grading_prompt += f"Correct answer: {q['answer']}\n"
        grading_prompt += f"Student's answer: {user_answers[i] if user_answers[i].strip() else '(no answer given)'}\n\n"

    grading_prompt += f"""
    Reply ONLY with valid JSON, no extra text, in this exact shape:
    {{"results": [{{"correct": true, "feedback": "..."}}]}}
    The "results" list must contain exactly {pair_count} items, in the same order as the questions above.
    """

    result = call_qwen_for_json(grading_prompt)
    results = result.get("results", [])
    if len(results) != pair_count:
        raise RuntimeError("Grading response didn't match the number of questions. Try again.")
    return results, questions, user_answers


# ----------------------------------------------------------------------
# Watched-time parsing (web version: no input(), just parses a string)
# ----------------------------------------------------------------------
def parse_watched_duration(text):
    """
    - a timestamp like '1:20' or '1:02:03' -> treated as the exact point reached in the video (absolute)
    - a plain number like '3' -> treated as extra minutes watched since last time (relative)
    Returns (seconds, is_absolute).
    """
    text = text.strip()
    if ":" in text:
        parts = [int(p) for p in text.split(":")]
        if len(parts) == 2:
            minutes, seconds = parts
            total_seconds = minutes * 60 + seconds
        else:
            hours, minutes, seconds = parts
            total_seconds = hours * 3600 + minutes * 60 + seconds
        return total_seconds, True
    else:
        minutes = float(text)
        return minutes * 60, False


def compute_new_checkpoint(old_checkpoint, total_seconds, watched_text):
    seconds, is_absolute = parse_watched_duration(watched_text)
    new_checkpoint = seconds if is_absolute else old_checkpoint + seconds
    return min(new_checkpoint, total_seconds)
