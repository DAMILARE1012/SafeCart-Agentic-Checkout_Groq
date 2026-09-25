import { useCallback, useEffect, useRef, useState, type RefObject } from 'react';

const THRESHOLD_PX = 80;

/**
 * Keeps a scroll container pinned to the bottom while content streams in, but
 * only if the user is already near the bottom. If they scrolled up to read,
 * we don't yank them down; instead `showJump` offers a "Jump to latest" button.
 */
export function useStickToBottom(
  containerRef: RefObject<HTMLElement | null>,
  contentRef: RefObject<HTMLElement | null>,
) {
  const pinned = useRef(true);
  const [showJump, setShowJump] = useState(false);

  const scrollToBottom = useCallback(
    (behavior: ScrollBehavior = 'auto') => {
      const el = containerRef.current;
      if (!el) return;
      el.scrollTo({ top: el.scrollHeight, behavior });
      pinned.current = true;
      setShowJump(false);
    },
    [containerRef],
  );

  useEffect(() => {
    const container = containerRef.current;
    const content = contentRef.current;
    if (!container || !content) return;

    const onScroll = () => {
      const distance = container.scrollHeight - container.scrollTop - container.clientHeight;
      pinned.current = distance < THRESHOLD_PX;
      if (pinned.current) setShowJump(false);
    };

    // Streaming text and images loading change the height without a React state change we can hook.
    const observer = new ResizeObserver(() => {
      if (pinned.current) container.scrollTop = container.scrollHeight;
      else setShowJump(true);
    });

    container.addEventListener('scroll', onScroll, { passive: true });
    observer.observe(content);
    container.scrollTop = container.scrollHeight;
    return () => {
      container.removeEventListener('scroll', onScroll);
      observer.disconnect();
    };
  }, [containerRef, contentRef]);

  return { showJump, scrollToBottom };
}
