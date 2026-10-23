"""Compare actual local history fitting with synthetic conversations only.

Run from a review checkout on the Mini. No bot imports, private files, tools,
cloud calls, or sends. The JSON retains synthetic inputs and final answers.
"""

import argparse
import ast
import hashlib
import json
import logging
import subprocess
import time
from types import SimpleNamespace
from pathlib import Path

import requests

if __package__:
    from .eval_conversation_style import prompt_namespace
else:
    from eval_conversation_style import prompt_namespace


ROOT = Path(__file__).resolve().parents[1]
FUNCTIONS = {
    "_clip_text_for_model", "_estimate_ollama_tokens", "_fit_history_for_model",
    "_ollama_history_budget", "_fit_ollama_history", "_clip_middle_for_local_prompt",
    "_reduce_prompt_budget", "_split_ollama_system_sections", "_fit_system_for_ollama",
}
FILLER = "The table decorations can be blue or green; either is acceptable. "
CASES = [
    {
        "id": "opening_constraints",
        "history": [{"role": "user", "content": "Dinner is Saturday at 7pm with a $120 total budget. " + FILLER * 18 + "Please keep this plan available for my next question."}],
        "text": "What are my budget and meeting time?",
        "criterion": "State $120 and Saturday at 7pm, without inventing other details.",
    },
    {
        "id": "correction_chain",
        "history": [
            {"role": "user", "content": "Dinner is Saturday at 7pm. Budget $120; six people; pickup costs $18 per person. " + FILLER * 11},
            {"role": "assistant", "content": "Pickup for six costs $108, within the $120 budget. " + FILLER * 9},
            {"role": "user", "content": "Correction: eight people and a $150 budget. " + FILLER * 10},
            {"role": "assistant", "content": "Pickup for eight costs $144. " + FILLER * 9},
            {"role": "user", "content": "Actually seven people; everything else stays the same."},
        ],
        "text": "Summarize the latest plan, total and headroom.",
        "criterion": "Saturday 7pm, seven people, $150 cap, pickup $126 and $24 headroom. No order claim.",
    },
    {
        "id": "draft_opening_and_deadline",
        "history": [{"role": "user", "content": "Draft: I can review your deck Tuesday at 2pm, but need the file Monday by noon. " + FILLER * 18 + "This is a draft only; nothing has been sent."}],
        "text": "What deadline did I give for the file?",
        "criterion": "Monday by noon; do not substitute Tuesday or claim a send.",
    },
    {
        "id": "short_context_control",
        "history": [{"role": "user", "content": "Both pizzas cost $18. One is five minutes away and the other is twenty minutes away."}],
        "text": "Which should I choose and why?",
        "criterion": "Choose the five-minute option using equal price and shorter travel, without inventing food-quality differences.",
    },
]


def load_fitters(source, context_tokens):
    nodes = []
    for node in ast.parse(source).body:
        if isinstance(node, ast.FunctionDef) and node.name in FUNCTIONS:
            nodes.append(node)
        elif isinstance(node, ast.Assign) and all(isinstance(target, ast.Name) for target in node.targets):
            if any(target.id.startswith(("_MAX_", "_MIN_", "_OLLAMA_")) for target in node.targets):
                try:
                    ast.literal_eval(node.value)
                except (ValueError, TypeError):
                    continue
                nodes.append(node)
    namespace = {
        "OLLAMA_NUM_CTX": context_tokens, "logger": logging.getLogger("context-eval"),
        # This component comparison supplies no authenticated actor prefix.
        # Keep it compatible with the separate request-context change without
        # importing permission/configuration code or private runtime state.
        "_request_context": SimpleNamespace(split_request_context=lambda system: ("", system)),
    }
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "synthetic-history-fitters", "exec"), namespace)
    return namespace


def fitted_inputs(namespace, system, case, token_cap):
    system = namespace["_fit_system_for_ollama"](system)
    if "_fit_ollama_history" in namespace:
        history = namespace["_fit_ollama_history"](system, case["history"], case["text"], token_cap)
    else:
        history = namespace["_fit_history_for_model"](
            case["history"], max_chars=namespace["_MAX_OLLAMA_HISTORY_CHARS"],
            max_turn_chars=namespace["_MAX_OLLAMA_HISTORY_TURN_CHARS"],
        )
    return system, history


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-ref", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--model", default="gemma3:latest")
    parser.add_argument("--samples", type=int, choices=range(1, 4), default=1)
    parser.add_argument("--timeout", type=float, default=45)
    parser.add_argument("--case", choices=[case["id"] for case in CASES], action="append")
    args = parser.parse_args(argv)
    if args.timeout <= 0:
        parser.error("Timeout must be positive.")
    if ROOT.is_relative_to(Path("/Users/<you>/projects/davosbot")):
        parser.error("Use a review checkout, not production.")
    baseline = subprocess.check_output(["git", "rev-parse", "--verify", args.baseline_ref + "^{commit}"], cwd=ROOT, text=True).strip()
    before = subprocess.check_output(["git", "show", baseline + ":davosbot/brain.py"], cwd=ROOT, text=True, encoding="utf-8")
    after = (ROOT / "davosbot/brain.py").read_text(encoding="utf-8")
    personality_source = (ROOT / "davosbot/personality.py").read_text(encoding="utf-8")
    personality = prompt_namespace(personality_source)
    personality["_current_time_instructions"] = lambda: "## Synthetic current time\nTuesday, September 8, 2026, 7pm Pacific."
    options = {"num_predict": 180, "num_ctx": 8192, "temperature": 0.3, "seed": 7}
    fitters = {label: load_fitters(source, options["num_ctx"]) for label, source in (("before", before), ("after", after))}
    results = []
    with requests.Session() as session:
        session.trust_env = False
        response = session.get("http://127.0.0.1:11434/api/tags", timeout=10)
        response.raise_for_status()
        installed = {item["name"]: item for item in response.json().get("models", [])}
        if args.model not in installed:
            parser.error("Select an installed model; this script never downloads models.")
        for case in CASES:
            if args.case and case["id"] not in args.case:
                continue
            prompt = personality["build_light_chat_system_prompt"](user_text=case["text"])
            for sample in range(args.samples):
                sample_options = {**options, "seed": options["seed"] + sample}
                for label, namespace in fitters.items():
                    system, history = fitted_inputs(namespace, prompt, case, options["num_predict"])
                    result = {"case": case["id"], "variant": label, "sample": sample + 1, "options": sample_options, "criterion": case["criterion"],
                              "original_history": case["history"], "history": history, "text": case["text"],
                              "system": system, "system_sha256": hashlib.sha256(system.encode()).hexdigest(), "status": "context_limit"}
                    started = time.monotonic()
                    if history is not None:
                        payload = {"model": args.model, "stream": False, "options": sample_options,
                                   "messages": [{"role": "system", "content": system}, *history, {"role": "user", "content": case["text"]}]}
                        if args.model.partition(":")[0].lower() == "gemma4":
                            payload["think"] = False
                        try:
                            response = session.post("http://127.0.0.1:11434/api/chat", json=payload, timeout=args.timeout)
                            response.raise_for_status()
                            data = response.json()
                            message = data.get("message", {})
                            output = message.get("content")
                            if not isinstance(output, str):
                                raise ValueError("invalid final response")
                            result.update(output=output, done_reason=data.get("done_reason"), output_tokens=data.get("eval_count"),
                                          status="truncated" if data.get("done_reason") == "length" else ("received" if output.strip() else "empty"))
                        except (requests.RequestException, ValueError, AttributeError) as exc:
                            result.update(status="error", error=type(exc).__name__)
                    result["seconds"] = round(time.monotonic() - started, 3)
                    results.append(result)
                    print(f"{case['id']}/{label}/{sample + 1}: {result['status']} ({result['seconds']}s)", flush=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({"baseline_ref": baseline, "after_brain_sha256": hashlib.sha256(after.encode()).hexdigest(),
                                     "personality_sha256": hashlib.sha256(personality_source.encode()).hexdigest(),
                                     "model": args.model, "model_digest": installed[args.model].get("digest"), "options": options,
                                     "semantic_assessment": "manual_review_required", "results": results}, indent=2), encoding="utf-8")
    return int(any(result["status"] != "received" for result in results))


if __name__ == "__main__":
    raise SystemExit(main())
