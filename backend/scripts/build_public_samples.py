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
    conversations = json.loads(
        (backend / "samples/conversations.json").read_text(encoding="utf-8")
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
        scenario = scenarios[edit["id"]]
        conversation = conversations[edit["id"]]
        original_path = (
            backend / "samples/generated/website" / edit["id"] / "sample.json"
        )
        if original_path.exists():
            messages = json.loads(original_path.read_text(encoding="utf-8"))[
                "conversation"
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
