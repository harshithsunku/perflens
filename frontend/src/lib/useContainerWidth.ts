import { useEffect, useState, type RefObject } from 'react';
import { debounce } from './format';

/** The element's inner width, tracked through a ResizeObserver (which
 * also fires when a hidden tab becomes visible and gets its real size). */
export function useContainerWidth(ref: RefObject<HTMLElement | null>, padding = 0): number {
  const [width, setWidth] = useState(0);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const measure = () => setWidth(Math.max(0, el.clientWidth - padding));
    measure();
    const ro = new ResizeObserver(debounce(measure, 150));
    ro.observe(el);
    return () => ro.disconnect();
  }, [ref, padding]);
  return width;
}
