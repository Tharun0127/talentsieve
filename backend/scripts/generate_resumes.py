"""Generate sample resume PDFs so anyone can run the demo.

    python -m scripts.generate_resumes                 # 50 resumes -> ./sample_resumes
    python -m scripts.generate_resumes --count 50 --corrupt 2 --out /tmp/resumes

Candidates get a random mix of skills, so scoring against a backend job
description produces a real spread of scores. --corrupt N adds broken files
to show the dead-letter queue (they fail permanently, never retried).
Deterministic (fixed seed): the same command always writes the same files.
"""
import argparse
import os
import random

from app.pdfgen import corrupt_pdf, text_pdf

FIRST = ["Asha", "Ben", "Chen", "Diya", "Elena", "Farid", "Grace", "Hiro", "Ines", "Jon", "Kavya", "Liam",
         "Maya", "Noah", "Olu", "Priya", "Quinn", "Rahul", "Sara", "Tomas", "Uma", "Victor", "Wen", "Yusuf", "Zoe"]
LAST = ["Rao", "Ito", "Park", "Silva", "Novak", "Haddad", "Okafor", "Müller", "Garcia", "Kim", "Singh", "Brown"]
BACKEND = ["Python", "FastAPI", "Django", "PostgreSQL", "Redis", "Docker", "Kubernetes", "AWS", "Celery",
           "Kafka", "SQLAlchemy", "REST APIs", "GraphQL", "CI/CD", "Terraform", "asyncio"]
OTHER = ["React", "TypeScript", "Figma", "Photoshop", "Excel", "Salesforce", "Java", "Spring", "Go", "Rust",
         "Swift", "Tableau", "SEO", "Copywriting"]
TITLES = ["Backend Engineer", "Software Engineer", "Full-Stack Developer", "Data Engineer", "Frontend Developer",
          "Product Designer", "DevOps Engineer", "Marketing Analyst"]
COMPANIES = ["Acme Corp", "Globex", "Initech", "Umbrella Labs", "Stark Industries", "Wayne Tech", "Hooli"]


def resume_lines(rng: random.Random, index: int) -> list[str]:
    name = f"{rng.choice(FIRST)} {rng.choice(LAST)}"
    backend_share = rng.random()  # how "backend" this candidate is
    skills = rng.sample(BACKEND, k=max(1, round(backend_share * 8))) + rng.sample(OTHER, k=rng.randint(1, 4))
    rng.shuffle(skills)
    years = rng.randint(1, 12)
    return [
        name,
        f"{rng.choice(TITLES)} | {name.split()[0].lower()}{index}@example.com",
        "",
        f"Summary: {years} years building software products.",
        f"Skills: {', '.join(skills)}",
        "",
        "Experience",
        f"- {rng.choice(COMPANIES)} ({years // 2 + 1} yrs): built services with {', '.join(skills[:3])}.",
        f"- {rng.choice(COMPANIES)} ({max(1, years // 2)} yrs): shipped features using {', '.join(skills[3:6]) or skills[0]}.",
        "",
        "Education: B.Sc. Computer Science",
    ]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=50)
    parser.add_argument("--corrupt", type=int, default=0)
    parser.add_argument("--out", default="sample_resumes")
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()

    rng = random.Random(args.seed)
    os.makedirs(args.out, exist_ok=True)
    for i in range(1, args.count + 1):
        with open(os.path.join(args.out, f"resume_{i:02d}.pdf"), "wb") as handle:
            handle.write(text_pdf(resume_lines(rng, i)))
    for i in range(1, args.corrupt + 1):
        with open(os.path.join(args.out, f"corrupt_{i:02d}.pdf"), "wb") as handle:
            handle.write(corrupt_pdf())
    print(f"wrote {args.count} resumes and {args.corrupt} corrupt files to {args.out}/")


if __name__ == "__main__":
    main()
