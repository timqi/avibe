import React, { useEffect, useState } from 'react';
import { createPortal } from 'react-dom';
import { useTranslation } from 'react-i18next';
import { Check, Copy, GitFork, TextQuote } from 'lucide-react';

import { copyTextToClipboard } from '../../lib/utils';

type SelectionState = { text: string; top: number; bottom: number; left: number };

const TOOLBAR_H = 38;
const GAP = 8;

// A floating toolbar that appears over a text selection inside the chat
// transcript. "Quote" appends the (quoted) selection to the current composer;
// "Ask in a new session" forks + prefills the fork's draft; "Copy" (mobile
// only) replaces the native callout that the transcript suppresses there.
export const SelectionQuoteToolbar: React.FC<{
  containerRef: React.RefObject<HTMLDivElement | null>;
  onQuote: (text: string) => void;
  onAskInNew: (text: string) => void;
}> = ({ containerRef, onQuote, onAskInNew }) => {
  const { t } = useTranslation();
  const [sel, setSel] = useState<SelectionState | null>(null);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    let timer = 0;
    const recompute = () => {
      const selection = window.getSelection();
      if (!selection || selection.isCollapsed || selection.rangeCount === 0) {
        setSel(null);
        return;
      }
      const text = selection.toString().trim();
      const range = selection.getRangeAt(0);
      if (!text || !container.contains(range.commonAncestorContainer)) {
        setSel(null);
        return;
      }
      const rect = range.getBoundingClientRect();
      if (!rect.width && !rect.height) {
        setSel(null);
        return;
      }
      setSel({ text, top: rect.top, bottom: rect.bottom, left: rect.left + rect.width / 2 });
    };
    // Debounce so the toolbar appears when the selection settles, not on every
    // intermediate range while dragging the selection / handles.
    const onSelectionChange = () => {
      window.clearTimeout(timer);
      timer = window.setTimeout(recompute, 150);
    };
    // A scrolled transcript makes the cached rect stale — hide immediately.
    const onScroll = () => {
      window.clearTimeout(timer);
      setSel(null);
    };
    document.addEventListener('selectionchange', onSelectionChange);
    container.addEventListener('scroll', onScroll, { passive: true });
    return () => {
      window.clearTimeout(timer);
      document.removeEventListener('selectionchange', onSelectionChange);
      container.removeEventListener('scroll', onScroll);
    };
  }, [containerRef]);

  if (!sel) return null;

  const dismiss = () => {
    window.getSelection()?.removeAllRanges();
    setSel(null);
    setCopied(false);
  };
  const runQuote = () => {
    onQuote(sel.text);
    dismiss();
  };
  const runAskInNew = () => {
    onAskInNew(sel.text);
    dismiss();
  };
  const runCopy = () => {
    const text = sel.text;
    void copyTextToClipboard(text).then((ok) => {
      if (ok) {
        setCopied(true);
        window.setTimeout(dismiss, 800);
      }
    });
  };

  const above = sel.top > TOOLBAR_H + GAP + 8;
  const top = above ? sel.top - TOOLBAR_H - GAP : sel.bottom + GAP;
  const left = Math.min(Math.max(sel.left, 96), window.innerWidth - 96);

  const itemClass =
    'flex items-center gap-1.5 px-3 py-2 text-[13px] font-medium text-foreground transition-colors hover:bg-foreground/[0.06]';

  return createPortal(
    <div
      role="toolbar"
      // Keep the selection alive when the toolbar is pressed (desktop). On touch
      // the handlers use the captured `sel.text`, so a collapse is harmless too.
      onMouseDown={(e) => e.preventDefault()}
      style={{ position: 'fixed', top, left, transform: 'translateX(-50%)', zIndex: 60 }}
      className="flex items-center overflow-hidden rounded-lg border border-border-strong bg-surface-2 shadow-[0_12px_30px_-8px_rgba(0,0,0,0.7)]"
    >
      <button type="button" className={itemClass} onClick={runQuote}>
        <TextQuote className="size-3.5 text-muted" />
        {t('chat.selection.quote')}
      </button>
      <span className="h-5 w-px bg-border" />
      <button type="button" className={itemClass} onClick={runAskInNew}>
        <GitFork className="size-3.5 text-muted" />
        {t('chat.selection.askInNew')}
      </button>
      {/* Copy is mobile-only: the transcript suppresses the native iOS callout
          there, so we re-offer copy ourselves. Desktop keeps Cmd/Ctrl+C. */}
      <span className="h-5 w-px bg-border md:hidden" />
      <button type="button" className={`${itemClass} md:hidden`} onClick={runCopy}>
        {copied ? <Check className="size-3.5 text-mint" /> : <Copy className="size-3.5 text-muted" />}
        {t('chat.selection.copy')}
      </button>
    </div>,
    document.body,
  );
};
