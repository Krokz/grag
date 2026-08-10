import { useCallback, useRef, useState } from 'react';

/**
 * Returns [size, onPointerDown] for a drag-resize handle.
 *
 * dir: 'x' = horizontal (resize width by dragging right edge)
 *      'y' = vertical   (resize height by dragging top/bottom edge)
 * invert: true when dragging toward decreasing clientX/Y should *increase* size
 *         (e.g. dragging the top edge of a bottom-anchored panel upward).
 */
export function useResizable(
  initial: number,
  min: number,
  max: number,
  dir: 'x' | 'y',
  invert = false,
): [number, (e: React.PointerEvent) => void] {
  const [size, setSize] = useState(initial);
  const startRef = useRef({ startPos: 0, startSize: initial });

  const onPointerDown = useCallback(
    (e: React.PointerEvent) => {
      e.preventDefault();
      const handle = e.currentTarget as HTMLElement;
      handle.setPointerCapture(e.pointerId);
      startRef.current = {
        startPos: dir === 'x' ? e.clientX : e.clientY,
        startSize: size,
      };

      const onMove = (ev: PointerEvent) => {
        const pos = dir === 'x' ? ev.clientX : ev.clientY;
        const delta = pos - startRef.current.startPos;
        const next = startRef.current.startSize + (invert ? -delta : delta);
        setSize(Math.min(max, Math.max(min, next)));
      };
      const onUp = () => {
        handle.removeEventListener('pointermove', onMove);
        handle.removeEventListener('pointerup', onUp);
      };
      handle.addEventListener('pointermove', onMove);
      handle.addEventListener('pointerup', onUp);
    },
    [dir, invert, max, min, size],
  );

  return [size, onPointerDown];
}
