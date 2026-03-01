/** Canonical final Planner display contract returned by GET trip plan. */
export interface DisplayWarning { severity: string; message: string; day_number?: number | null }
export interface DisplayActivity {
  poi_id: string; name: string; mandatory: boolean; start_time?: string | null; end_time?: string | null
  visit_duration_minutes?: number | null; latitude?: number | null; longitude?: number | null
  address?: string | null; categories: string[]; restrictions: string[]; accessibility_notes: string[]
}
export interface DisplayTimelineItem { id: string; type: string; title: string; start_time?: string | null; end_time?: string | null; status: string }
export interface DisplayTransportLeg { origin_id: string; destination_id: string; selected_mode: string; duration_minutes?: number | null; distance_km?: number | null; uncertainty: boolean }
export interface DisplayDay { day_number: number; date: string; title: string; theme: string; activities: DisplayActivity[]; timeline: DisplayTimelineItem[]; transport_legs: DisplayTransportLeg[]; warnings: string[]; unresolved_items: string[] }
export interface DisplayMandatoryItem { requirement_id: string; label: string; status: 'SATISFIED' | 'ALLOCATED_UNRESOLVED' | 'UNSATISFIED'; day_number?: number | null; reason?: string | null }
export interface DisplayTrip { status: 'PASS' | 'BEST_EFFORT' | 'FAIL'; trip_summary: { title: string; duration_days: number; currency: string }; days: DisplayDay[]; warnings: string[]; mandatory_summary: { items: DisplayMandatoryItem[] }; unresolved_items: DisplayWarning[] }
