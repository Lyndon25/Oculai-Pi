/** Defers a scheduler rebuild until no active run can still reference it. */
export class SchedulerRefreshGate {
  private pending = false;

  request(activeRuns: number): boolean {
    if (activeRuns > 0) {
      this.pending = true;
      return false;
    }
    this.pending = false;
    return true;
  }

  consumeWhenIdle(activeRuns: number): boolean {
    if (!this.pending || activeRuns > 0) return false;
    this.pending = false;
    return true;
  }
}
