import { useCallback, useEffect, useState } from "react";
import type { Dispatch, SetStateAction } from "react";

export interface AsyncResource<T> {
  data?: T;
  loading: boolean;
  error?: Error;
  reload: () => Promise<void>;
  setData: Dispatch<SetStateAction<T | undefined>>;
}

export function useAsyncResource<T>(loader: (signal: AbortSignal) => Promise<T>): AsyncResource<T> {
  const [data, setData] = useState<T>();
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<Error>();
  const load = useCallback(async () => {
    const controller = new AbortController();
    setLoading(true);
    setError(undefined);
    try {
      setData(await loader(controller.signal));
    } catch (reason) {
      if (!controller.signal.aborted) {
        setError(reason instanceof Error ? reason : new Error(String(reason)));
      }
    } finally {
      if (!controller.signal.aborted) setLoading(false);
    }
    return () => controller.abort();
  }, [loader]);

  useEffect(() => {
    const controller = new AbortController();
    loader(controller.signal)
      .then(setData)
      .catch((reason) => {
        if (!controller.signal.aborted) {
          setError(reason instanceof Error ? reason : new Error(String(reason)));
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [loader]);

  return { data, loading, error, reload: async () => { await load(); }, setData };
}
