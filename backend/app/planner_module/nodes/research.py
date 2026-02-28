"""Research node functions; all graph edges are declared in graph.py."""

from __future__ import annotations

from app.planner_module.models.research import ResearchReport
from app.planner_module.tools.research import ResearchTools

from ..state import PipelineState


def research_nodes(tools: ResearchTools):
    """Create research node functions bound to this run's tools."""

    async def prepare(state: PipelineState):
        tools.write_stage("00_research_input", state["trip"].model_dump(mode="json"))
        return {}

    async def interpret(state: PipelineState):
        tools.write_stage(
            "01_destination_interpretation",
            {
                "type": state["trip"].destination.type,
                "items": state["trip"].destination.items,
            },
        )
        return {}

    async def resolve(state: PipelineState):
        stops = await tools.resolve_destinations(state["trip"])
        tools.write_stage(
            "02_resolved_stops", {"stops": [item.model_dump() for item in stops]}
        )
        return {"stops": stops}

    async def context(state: PipelineState):
        weather, finance = await tools.research_context(state["trip"], state["stops"])
        tools.write_stage(
            "03_context_research",
            {
                "weather": [item.model_dump() for item in weather],
                "financial_context": finance,
            },
        )
        return {"weather": weather, "finance": finance}

    async def discovery(state: PipelineState):
        targets = set(state.get("repair_targets", [])) or None
        discovered = await tools.discover_pois(
            state["trip"],
            state["stops"],
            stop_ids=targets,
            repair=bool(targets),
        )
        candidates = dict(state.get("candidates", {}))
        for stop_id, new_items in discovered.items():
            candidates[stop_id] = tools.canonical_candidates(
                new_items,
                candidates.get(stop_id, []),
                trip=state["trip"],
                reserve_repair=not state.get("repair_pass"),
            )
        tools.write_stage("04_poi_candidates", candidates)
        return {"candidates": candidates}

    async def shortlist(state: PipelineState):
        targets = set(state.get("repair_targets", []))
        candidates = {
            stop_id: [
                record
                for record in records
                if (stop_id, record.get("google_place_id") or record["id"])
                not in tools.enrichment_attempts
            ]
            for stop_id, records in state["candidates"].items()
            if not targets or stop_id in targets
        }
        selected = tools.shortlist_pois(state["trip"], candidates)
        tools.write_stage(
            "05_poi_shortlist",
            {key: [item["name"] for item in value] for key, value in selected.items()},
        )
        return {"shortlist": selected}

    async def enrich(state: PipelineState):
        backups = tools.candidate_pois(state["trip"], state["candidates"])
        enriched = await tools.enrich_pois(state["trip"], state["shortlist"])
        pois = tools.merge_pois(backups, state.get("pois", {}), enriched)
        tools.write_stage(
            "06_poi_enrichment",
            {key: [item.model_dump() for item in value] for key, value in pois.items()},
        )
        return {"pois": pois}

    async def cluster(state: PipelineState):
        clusters = tools.cluster_pois(state["pois"])
        tools.write_stage(
            "07_clusters", {"clusters": [item.model_dump() for item in clusters]}
        )
        return {"clusters": clusters}

    async def routes(state: PipelineState):
        repair_targets = set(state.get("repair_transport_targets", []))
        result = await tools.research_routes(
            state["trip"],
            state["stops"],
            state["clusters"],
            repair=bool(state.get("repair_transport")),
            repair_requirement_ids=repair_targets,
            previous_requirements=state.get("transport_requirements"),
            previous_routes=state.get("routes"),
        )
        tools.write_stage(
            "08_routes",
            {
                "inter_stop": [item.model_dump() for item in result[0]],
                "intra_stop": [item.model_dump() for item in result[1]],
                "requirements": [item.model_dump() for item in result[2]],
            },
        )
        return {"routes": (result[0], result[1]), "transport_requirements": result[2]}

    async def quality(state: PipelineState):
        coverage = tools.measure_coverage(
            state["trip"],
            state["stops"],
            state["pois"],
            state["routes"],
            state.get("transport_requirements"),
        )
        quality_result = tools.validate_quality(
            state["trip"],
            state["stops"],
            state["pois"],
            state["clusters"],
            state["routes"],
            coverage,
            state.get("transport_requirements"),
        )
        context_gaps = []
        if state["stops"] and (
            not state.get("weather")
            or any(item.evidence_type == "unknown" for item in state.get("weather", []))
        ):
            context_gaps.append("Some destination weather context remains unavailable.")
        if state.get("finance", {}).get("status") == "unavailable":
            context_gaps.append("Currency conversion remains unverified.")
        if context_gaps:
            quality_result = quality_result.model_copy(
                update={
                    "optional_gaps": list(
                        dict.fromkeys(quality_result.optional_gaps + context_gaps)
                    ),
                }
            )
        gaps = tools.research_gaps(
            coverage, quality_result, state.get("transport_requirements"), state["pois"]
        )
        status = (
            "blocked"
            if quality_result.critical_gaps
            else ("partial" if quality_result.optional_gaps else "complete")
        )
        tools.write_stage(
            "09_quality_gate",
            {
                "status": status,
                **quality_result.model_dump(),
                "coverage": coverage.model_dump(),
                "research_gaps": [item.model_dump() for item in gaps],
                "repair_pass": state.get("repair_pass", 0),
            },
        )
        return {
            "quality": quality_result,
            "coverage": coverage,
            "research_gaps": gaps,
        }

    async def gap_repair(state: PipelineState):
        gaps = state.get("research_gaps", [])
        resolution_needed = any(gap.code == "destination_resolution" for gap in gaps)
        repaired_stops = (
            await tools.repair_destination_resolution(state["trip"])
            if resolution_needed
            else []
        )
        targets = sorted(
            {
                stop_id
                for gap in gaps
                if gap.code
                in {"destination_activity_coverage", "optional_closed_candidates"}
                for stop_id in gap.stop_ids
            }
        )
        if repaired_stops:
            targets = [stop.id for stop in repaired_stops]
        transport_needed = any(gap.code == "major_transport_evidence" for gap in gaps)
        transport_targets = sorted(
            requirement.id
            for requirement in state.get("transport_requirements", [])
            if requirement.status in {"missing", "unavailable"}
        )
        tools.write_stage(
            "10_repair",
            {
                "previous_stop_count": len(state["stops"]),
                "repaired_stop_count": len(repaired_stops),
                "target_stop_ids": targets,
                "target_transport": transport_needed,
                "target_transport_requirement_ids": transport_targets,
                "repair_pass": state.get("repair_pass", 0) + 1,
            },
        )
        return {
            "stops": repaired_stops or state["stops"],
            "repair_pass": state.get("repair_pass", 0) + 1,
            "repair_targets": targets,
            "repair_transport": transport_needed,
            "repair_transport_targets": transport_targets,
        }

    async def build_report(state: PipelineState):
        quality_result = state["quality"]
        status = (
            "blocked"
            if quality_result.critical_gaps
            else ("partial" if quality_result.optional_gaps else "complete")
        )
        tools.merge_metrics()
        report = ResearchReport(
            status=status,
            normalized_trip_context=state["trip"],
            destination_interpretation=f"Validated {state['trip'].destination.type} destination using provider evidence.",
            resolved_stops=state["stops"],
            pois_by_stop=state["pois"],
            geographic_clusters=state["clusters"],
            stay_bases=tools.build_stay_bases(state["stops"], state["clusters"]),
            inter_stop_routes=state["routes"][0],
            intra_stop_route_evidence=state["routes"][1],
            transport_requirements=state.get("transport_requirements", []),
            weather_context=state["weather"],
            financial_context=state["finance"],
            mandatory_constraints=state["trip"].must_see,
            mandatory_requirements=tools._requirements_for(state["trip"]),
            unresolved_gaps=quality_result.critical_gaps + quality_result.optional_gaps,
            coverage=state["coverage"],
            research_gaps=state.get("research_gaps", []),
            evidence=tools.evidence,
            quality=quality_result,
            metrics=tools.cache.metrics,
        )
        tools.write_stage("11_research_report", report.model_dump(mode="json"))
        return {"report": report}

    def next_after_quality(state: PipelineState) -> str:
        if any(
            gap.code
            in {
                "destination_resolution",
                "destination_activity_coverage",
                "major_transport_evidence",
            }
            for gap in state.get("research_gaps", [])
        ) and state.get("repair_pass", 0) < min(1, tools.limits.max_gap_repair_passes):
            return "gap_repair"
        return "build_research_report"

    def next_after_repair(state: PipelineState) -> str:
        """Repeat only the factual area that the quality gate identified."""
        return "poi_discovery" if state.get("repair_targets") else "route_research"

    nodes = {
        "prepare_research_input": prepare,
        "interpret_destination": interpret,
        "resolve_destinations": resolve,
        "parallel_context_research": context,
        "poi_discovery": discovery,
        "poi_shortlist": shortlist,
        "parallel_poi_enrichment": enrich,
        "geographic_clustering": cluster,
        "route_research": routes,
        "research_quality_gate": quality,
        "gap_repair": gap_repair,
        "build_research_report": build_report,
    }
    return nodes, next_after_quality, next_after_repair
