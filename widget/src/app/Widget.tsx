import { useEffect, useId, type CSSProperties } from 'react';
import { ChatPanel } from '@/features/panel/ChatPanel';
import { Launcher } from '@/features/launcher/Launcher';
import { useConnectionMonitor } from '@/features/connection/useConnectionMonitor';
import { readStoredSession } from '@/features/session/sessionSlice';
import { panelOpened } from '@/features/ui/uiSlice';
import { startWidget } from './bootstrap';
import { useAppDispatch, useAppSelector, useAppStore } from './hooks';
import { useResolvedTheme } from './useResolvedTheme';

export function Widget() {
  const dispatch = useAppDispatch();
  const store = useAppStore();
  const panelId = useId();
  const config = useAppSelector((s) => s.config);
  const isOpen = useAppSelector((s) => s.ui.isOpen);
  const sessionStatus = useAppSelector((s) => s.session.status);
  const theme = useResolvedTheme(config.theme);

  useConnectionMonitor();

  // Performance: the widget does no network work on page load unless a previous
  // session exists (then it resumes, so order tracking survives reloads).
  useEffect(() => {
    if (readStoredSession(store.getState())) void dispatch(startWidget());
    if (config.openOnLoad) dispatch(panelOpened());
  }, [dispatch, store, config.openOnLoad]);

  useEffect(() => {
    if (isOpen && sessionStatus === 'idle') void dispatch(startWidget());
  }, [isOpen, sessionStatus, dispatch]);

  return (
    <div
      className="cc-root"
      data-theme={theme}
      style={{ '--cc-brand': config.brandColor, '--cc-brand-fg': config.brandForeground } as CSSProperties}
    >
      {isOpen && <ChatPanel id={panelId} />}
      <Launcher panelId={panelId} />
    </div>
  );
}
