export function getPlanningState(
  tripStatus: string | undefined,
  jobStatus: string | undefined,
  hasPlan: boolean,
) {
  const hasJob = jobStatus === 'queued' || jobStatus === 'running'
    || jobStatus === 'completed' || jobStatus === 'failed'
  const isDone = jobStatus === 'completed'
    || (!hasJob && (tripStatus === 'completed' || hasPlan))
  const isPlanning = !isDone && jobStatus !== 'failed'
    && (jobStatus === 'running' || jobStatus === 'queued' || tripStatus === 'planning')
  const hasPlanningFailure = !isDone && jobStatus === 'failed'
  const hasPlanningStarted = isPlanning || isDone || hasPlanningFailure

  return { isDone, isPlanning, hasPlanningFailure, hasPlanningStarted }
}
