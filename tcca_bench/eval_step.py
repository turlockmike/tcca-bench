#!/usr/bin/env python3
"""Grade a single answer via Ollama (native API, think:false).

Usage:
    python eval_step.py --question "What degree?" --answer "Business Admin" \
        --hypothesis "The user graduated with Business Administration" \
        --question-type single-session-user --model gemma4:26b

Prints "true" or "false" to stdout.
"""

import argparse
import sys

import httpx

OLLAMA_URL = "http://localhost:11434"


def get_eval_prompt(question_type: str, question: str, answer: str, hypothesis: str) -> str:
    """Build yes/no eval prompt. Matches LongMemEval evaluate_qa.py templates."""
    base_type = question_type.replace("_abs", "")

    if base_type in ("single-session-user", "single-session-assistant", "multi-session"):
        return (
            "I will give you a question, a correct answer, and a response from a model. "
            "Please answer yes if the response contains the correct answer. Otherwise, answer no. "
            "If the response is equivalent to the correct answer or contains all the intermediate steps "
            "to get the correct answer, you should also answer yes. "
            "If the response only contains a subset of the information required by the answer, answer no.\n\n"
            f"Question: {question}\n"
            f"Correct Answer: {answer}\n"
            f"Model Response: {hypothesis}\n"
            "Is the model response correct? Answer yes or no only."
        )
    elif base_type == "temporal-reasoning":
        return (
            "I will give you a question, a correct answer, and a response from a model. "
            "Please answer yes if the response contains the correct answer. Otherwise, answer no. "
            "Do not penalize off-by-one errors for the number of days.\n\n"
            f"Question: {question}\n"
            f"Correct Answer: {answer}\n"
            f"Model Response: {hypothesis}\n"
            "Is the model response correct? Answer yes or no only."
        )
    elif base_type == "knowledge-update":
        return (
            "I will give you a question, a correct answer, and a response from a model. "
            "Please answer yes if the response contains the correct answer. Otherwise, answer no. "
            "If the response contains some previous information along with an updated answer, "
            "the response should be considered as correct as long as the updated answer is the required answer.\n\n"
            f"Question: {question}\n"
            f"Correct Answer: {answer}\n"
            f"Model Response: {hypothesis}\n"
            "Is the model response correct? Answer yes or no only."
        )
    elif base_type == "single-session-preference":
        return (
            "I will give you a question, a rubric for desired personalized response, "
            "and a response from a model. "
            "Please answer yes if the response satisfies the desired response. Otherwise, answer no. "
            "The response is correct as long as it recalls and utilizes the user's personal information correctly.\n\n"
            f"Question: {question}\n"
            f"Rubric: {answer}\n"
            f"Model Response: {hypothesis}\n"
            "Does the model response satisfy the rubric? Answer yes or no only."
        )
    else:
        return (
            f"Question: {question}\n"
            f"Correct Answer: {answer}\n"
            f"Model Response: {hypothesis}\n"
            "Is the model response correct? Answer yes or no only."
        )


ABSTAIN_PHRASES = (
    "i don't know",
    "i don't know.",
    "i do not know",
    "i do not know.",
    "no information",
    "not in the context",
    "not mentioned",
    "cannot determine",
)


def _is_abstention(text: str) -> bool:
    t = (text or "").strip().lower()
    if not t:
        return True
    return any(p in t for p in ABSTAIN_PHRASES)


def grade(model: str, question: str, answer: str, hypothesis: str, question_type: str, ollama_url: str = None) -> bool:
    """Call Ollama to grade an answer. Returns True if correct."""
    url = ollama_url or OLLAMA_URL

    # Abstention: correct answer IS to abstain. Don't call the judge.
    if question_type == "abstention":
        return _is_abstention(hypothesis)

    # For non-abstention types, an empty or abstaining hypothesis is wrong.
    if not hypothesis or _is_abstention(hypothesis):
        return False

    prompt = get_eval_prompt(question_type, question, answer, hypothesis)

    response = httpx.post(
        f"{url}/api/chat",
        json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "think": False,
            "options": {"temperature": 0, "num_predict": 50},
        },
        timeout=120.0,
    )
    response.raise_for_status()
    text = response.json().get("message", {}).get("content", "").strip()

    if not text:
        return False
    first_word = text.split()[0].lower().rstrip(".,!:;")
    return first_word == "yes"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--question", required=True)
    parser.add_argument("--answer", required=True)
    parser.add_argument("--hypothesis", required=True)
    parser.add_argument("--question-type", required=True)
    parser.add_argument("--model", default="gemma4:26b")
    parser.add_argument("--ollama-url", default=OLLAMA_URL)
    args = parser.parse_args()

    result = grade(args.model, args.question, args.answer, args.hypothesis, args.question_type, args.ollama_url)
    print("true" if result else "false")


if __name__ == "__main__":
    main()
