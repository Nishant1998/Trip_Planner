export function getPlanningState(
  tripStatus: string | undefined,
  jobStatus: string | undefined,
  hasPlan: boolean,
) {
  const failed = jobStatus === 'failed' || jobStatus === 'interrupted'
  const hasJob = jobStatus === 'queued' || jobStatus === 'running'
    || jobStatus === 'completed' || failed
  const isDone = jobStatus === 'completed'
    || (!hasJob && (tripStatus === 'completed' || hasPlan))
  const isPlanning = !isDone && !failed
    && (jobStatus === 'running' || jobStatus === 'queued' || tripStatus === 'planning')
  const hasPlanningFailure = !isDone && failed
  const hasPlanningStarted = isPlanning || isDone || hasPlanningFailure

  return { isDone, isPlanning, hasPlanningFailure, hasPlanningStarted }
}

export interface PlanningJob {
  id?: string
  status: string
  progress_percent: number
  current_step?: string
  current_stage?: string | null
  progress_kind?: string | null
  activity_detail?: string | null
  progress_sequence?: number | null
  updated_at?: string | null
  failure?: { code?: string; message?: string } | null
}

export interface PlanningProgress {
  run_id: string
  sequence: number
  stage: string
  kind: string
  detail?: string | null
  occurred_at?: string
}

export function mergePlanningProgress(job: PlanningJob | null, event: PlanningProgress): PlanningJob | null {
  if (job && (['completed', 'failed', 'interrupted'].includes(job.status)
    || (job.id && job.id !== event.run_id)
    || (job.progress_sequence ?? 0) >= event.sequence)) return job
  return {
    ...job, id: event.run_id, status: job?.status ?? 'running',
    progress_percent: job?.progress_percent ?? 0,
    current_stage: event.stage, progress_kind: event.kind,
    activity_detail: event.detail, progress_sequence: event.sequence,
    updated_at: event.occurred_at,
  }
}

export function shouldPollTrip(tripStatus: string | undefined, jobStatus: string | undefined, pending: boolean) {
  if (['completed', 'failed', 'interrupted'].includes(jobStatus ?? '')) return false
  return pending || tripStatus === 'confirmed' || tripStatus === 'planning'
    || jobStatus === 'running' || jobStatus === 'queued'
}
