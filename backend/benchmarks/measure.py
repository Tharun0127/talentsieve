"""Upload one batch of generated resumes and time it until every job is done.

Runs inside the api container against the live stack (driven by
benchmarks/run_benchmarks.sh, which sets the worker count and MockLLM
behaviour between runs):

    python -m benchmarks.measure --label "4 workers" --count 50

Each run uses a UNIQUE job description, so the LLM cache (keyed by resume +
job description) cannot answer from earlier runs and every resume really
goes through a worker.
"""
import argparse
import json
import os
import random
import time
import urllib.request
import uuid
from collections import Counter

from app.pdfgen import text_pdf
from scripts.generate_resumes import resume_lines

API = os.getenv("BENCH_API", "http://localhost:8000")
RESULTS = os.path.join(os.path.dirname(__file__), "results")


def call(path: str, data: bytes | None = None, headers: dict | None = None) -> dict:
    request = urllib.request.Request(API + path, data=data, headers=headers or {})
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def login() -> dict:
    body = json.dumps({"email": "recruiter@talentsieve.dev", "password": "recruit123"}).encode()
    token = call("/auth/login", body, {"content-type": "application/json"})["access_token"]
    return {"Authorization": f"Bearer {token}"}


def multipart(fields: dict, files: list[tuple[str, bytes]]) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
             for k, v in fields.items()]
    for name, data in files:
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="files"; filename="{name}"\r\n'
                     f"Content-Type: application/pdf\r\n\r\n".encode() + data + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def run(label: str, count: int) -> dict:
    headers = login()
    rng = random.Random(7)
    files = [(f"resume_{i:02d}.pdf", text_pdf(resume_lines(rng, i))) for i in range(1, count + 1)]
    job_description = f"Backend engineer: Python FastAPI PostgreSQL Redis Docker AWS. (run {uuid.uuid4().hex[:8]})"
    body, content_type = multipart({"title": f"bench: {label}", "job_description": job_description}, files)

    started = time.perf_counter()
    batch = call("/batches", body, {**headers, "content-type": content_type})
    upload_seconds = time.perf_counter() - started
    while True:
        detail = call(f"/batches/{batch['batch_id']}", headers=headers)
        finished = detail["counts"].get("done", 0) + detail["counts"].get("dead", 0)
        if finished >= count:
            break
        time.sleep(0.2)
    total_seconds = time.perf_counter() - started

    attempts = Counter(r["attempts"] for r in detail["resumes"])
    done = detail["counts"].get("done", 0)
    return {
        "label": label, "resumes": count, "upload_seconds": round(upload_seconds, 3),
        "total_seconds": round(total_seconds, 2), "throughput_per_min": round(count / total_seconds * 60, 1),
        "done": done, "dead": detail["counts"].get("dead", 0), "success_rate": round(done / count * 100, 1),
        "attempts_histogram": dict(sorted(attempts.items())),
        "llm_calls": sum(r["attempts"] for r in detail["resumes"]),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", required=True)
    parser.add_argument("--count", type=int, default=50)
    parser.add_argument("--out", default="benchmarks.jsonl")
    args = parser.parse_args()
    result = run(args.label, args.count)
    print(json.dumps(result))
    os.makedirs(RESULTS, exist_ok=True)
    with open(os.path.join(RESULTS, args.out), "a") as handle:
        handle.write(json.dumps(result) + "\n")


if __name__ == "__main__":
    main()
