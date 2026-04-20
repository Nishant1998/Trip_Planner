"""Build reviewed, provider-independent frontend examples. No model or API calls."""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def build_samples():
    backend = ROOT / "backend"
    scenarios = {
        item["id"]: item
        for item in json.loads(
            (backend / "samples/scenarios.json").read_text(encoding="utf-8")
        )
    }
    edits = json.loads(
        (backend / "samples/public_edits.json").read_text(encoding="utf-8")
    )
    samples = []
    photos = json.loads(
        (backend / "samples/photo_manifest.json").read_text(encoding="utf-8")
    )
    for edit in edits:
        cover_photo = photos["covers"][edit["destination"]]
        edit["cover"] = cover_photo["url"]
        edit["coverPhoto"] = cover_photo
        for day in edit["days"]:
            for stop in day["stops"]:
                photo = photos["places"].get(stop.get("mapQuery"))
                if photo:
                    stop["photo"] = photo
        source = json.loads(
            (
                backend / "samples/generated/website" / edit["id"] / "sample.json"
            ).read_text(encoding="utf-8")
        )
        scenario = scenarios[edit["id"]]
        messages = source["conversation"]
        opening = next(i for i, m in enumerate(messages) if m["role"] == "user")
        summary = next(
            m
            for m in reversed(messages)
            if m["role"] == "assistant" and "Does this look right?" in m["content"]
        )
        conversation = [
            *messages[opening : opening + 2],
            {"role": "user", "content": scenario["preferences"]},
            {"role": "user", "content": scenario["correction"]},
            summary,
            {
                "role": "user",
                "content": "Yes, the latest summary is correct. Start planning.",
            },
            {
                "role": "assistant",
                "content": "Perfect! Your trip details are confirmed and saved.",
            },
        ]
        originals = {(message["role"], message["content"]) for message in messages}
        if any(
            (message["role"], message["content"]) not in originals
            for message in conversation
        ):
            raise ValueError(
                f"A selected message is not in the original chat: {edit['id']}"
            )
        # Only author-reviewed text and selected anonymous messages enter the public bundle.
        samples.append(
            {
                **edit,
                "title": scenario["title"],
                "features": scenario["features"],
                "requirements": scenario["expected"],
                "status": "draft",
                "provenance": "Generated with Riya, then reviewed and simplified for this demo.",
                "conversation": conversation,
            }
        )
    target = ROOT / "frontend/src/data/sampleTrips.json"
    target.write_text(
        json.dumps(samples, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        f"Built {len(samples)} public samples; no accounts, provider responses or private run IDs included."
    )


if __name__ == "__main__":
    build_samples()
