export const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

export function createLatestRequestScope() {
  let generation = 0;
  let controller = null;

  function cancel() {
    generation += 1;
    if (controller) controller.abort();
    controller = null;
  }

  function begin() {
    cancel();
    controller = new AbortController();
    const requestGeneration = generation;
    return {
      signal: controller.signal,
      generation: requestGeneration,
      isCurrent: () => requestGeneration === generation && !controller.signal.aborted,
    };
  }

  return {
    begin,
    cancel,
    isCurrent: (requestGeneration) => requestGeneration === generation,
    get generation() { return generation; },
  };
}
