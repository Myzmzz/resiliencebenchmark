export function menuSelectionForPath(pathname: string): string {
  return pathname.startsWith("/evaluation/tasks")
    ? "/evaluation/tasks"
    : pathname.startsWith("/evaluation/stage2-matrices")
      ? "/evaluation/stage2-matrices"
    : pathname.startsWith("/evaluation/monitoring")
      ? "/evaluation/monitoring"
      : pathname.startsWith("/evaluation/results")
        ? "/evaluation/results"
        : pathname;
}
