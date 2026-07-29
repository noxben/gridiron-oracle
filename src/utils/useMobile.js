// useMobile.js — Gridiron Oracle
// Shared responsive breakpoint hook.
// Used by components to adapt layout at 375px (phone) and 640px (small tablet).
//
// Usage:
//   const { isMobile, isNarrow } = useMobile();
//   isMobile  — true at ≤640px  (collapse two-column layouts)
//   isNarrow  — true at ≤420px  (reduce padding, font sizes)

import { useState, useEffect } from 'react';

export function useMobile() {
  const [width, setWidth] = useState(
    typeof window !== 'undefined' ? window.innerWidth : 1024
  );

  useEffect(() => {
    const handler = () => setWidth(window.innerWidth);
    window.addEventListener('resize', handler, { passive: true });
    return () => window.removeEventListener('resize', handler);
  }, []);

  return {
    isMobile: width <= 640,
    isNarrow: width <= 420,
    width,
  };
}

// Responsive padding helper — returns content padding based on screen width
export function contentPadding(isMobile, isNarrow) {
  if (isNarrow) return '16px';
  if (isMobile) return '20px';
  return '40px';
}
