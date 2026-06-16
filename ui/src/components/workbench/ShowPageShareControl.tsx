import React, { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Check, Copy, Loader2, Share2 } from 'lucide-react';

import { Button } from '../ui/button';
import { Input } from '../ui/input';
import { Popover, PopoverContent, PopoverTrigger } from '../ui/popover';
import { Switch } from '../ui/switch';
import { useApi } from '../../context/ApiContext';

type ShowPagePayload = {
  visibility: string;
  active_url: string | null;
  public_url: string | null;
  private_url: string | null;
  url_available: boolean;
  url_guidance: string | null;
  share_id: string | null;
};

// Share affordance shown only while the Show Page is open (the in-chat view).
// A popover with the page link (copy + native share) and a public/private
// toggle that flips visibility in place via the existing show-pages API.
export const ShowPageShareControl: React.FC<{ sessionId: string }> = ({ sessionId }) => {
  const { t } = useTranslation();
  const api = useApi();
  const [open, setOpen] = useState(false);
  const [payload, setPayload] = useState<ShowPagePayload | null>(null);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [copied, setCopied] = useState(false);

  const isPublic = payload?.visibility === 'public';
  const link = payload?.active_url ?? payload?.private_url ?? '';
  const canNativeShare = typeof navigator !== 'undefined' && typeof navigator.share === 'function';

  const handleOpenChange = (next: boolean) => {
    setOpen(next);
    // Lazily resolve the page (idempotent ensure) the first time the popover
    // opens, so the link + current visibility are always fresh.
    if (next && !payload && !loading) {
      setLoading(true);
      api
        .ensureShowPage(sessionId)
        .then((res: ShowPagePayload) => setPayload(res))
        .catch(() => undefined)
        .finally(() => setLoading(false));
    }
  };

  const toggleVisibility = (nextPublic: boolean) => {
    setBusy(true);
    api
      .setShowPageVisibility(sessionId, nextPublic ? 'public' : 'private')
      .then((res: ShowPagePayload) => setPayload(res))
      .catch(() => undefined)
      .finally(() => setBusy(false));
  };

  const copyLink = async () => {
    if (!link) return;
    let ok = false;
    try {
      await navigator.clipboard.writeText(link);
      ok = true;
    } catch {
      const field = document.getElementById('show-share-link');
      if (field instanceof HTMLInputElement) {
        field.select();
        ok = document.execCommand('copy');
      }
    }
    if (!ok) return;
    setCopied(true);
    window.setTimeout(() => setCopied(false), 2000);
  };

  const nativeShare = async () => {
    if (!link) return;
    try {
      await navigator.share({ title: t('chat.showPage.title'), url: link });
    } catch {
      // user dismissed the share sheet, or it is unavailable — no-op
    }
  };

  return (
    <Popover open={open} onOpenChange={handleOpenChange}>
      <PopoverTrigger asChild>
        <Button
          type="button"
          variant="ghost"
          size="icon"
          className="size-7 shrink-0"
          aria-label={t('chat.showPage.share')}
          title={t('chat.showPage.share')}
        >
          <Share2 className="size-3.5" />
        </Button>
      </PopoverTrigger>
      <PopoverContent align="end" className="w-80 space-y-3">
        <div className="text-sm font-medium">{t('chat.showPage.shareTitle')}</div>

        {loading ? (
          <div className="flex items-center gap-2 py-2 text-sm text-muted">
            <Loader2 className="size-4 animate-spin" />
            {t('common.loading')}
          </div>
        ) : (
          <>
            <div className="flex items-center gap-1.5">
              <Input
                id="show-share-link"
                readOnly
                value={link}
                onFocus={(e) => e.currentTarget.select()}
                className="h-8 flex-1 text-xs"
              />
              <Button
                type="button"
                variant="outline"
                size="icon"
                className="size-8 shrink-0"
                onClick={copyLink}
                aria-label={t('chat.showPage.copyLink')}
                title={t('chat.showPage.copyLink')}
              >
                {copied ? <Check className="size-3.5" /> : <Copy className="size-3.5" />}
              </Button>
              {canNativeShare && (
                <Button
                  type="button"
                  variant="outline"
                  size="icon"
                  className="size-8 shrink-0"
                  onClick={nativeShare}
                  aria-label={t('chat.showPage.nativeShare')}
                  title={t('chat.showPage.nativeShare')}
                >
                  <Share2 className="size-3.5" />
                </Button>
              )}
            </div>

            <div className="flex items-center justify-between gap-3">
              <div className="min-w-0">
                <div className="text-sm">
                  {isPublic ? t('chat.showPage.visibilityPublic') : t('chat.showPage.visibilityPrivate')}
                </div>
                <div className="text-xs text-muted">
                  {isPublic ? t('chat.showPage.publicDesc') : t('chat.showPage.privateDesc')}
                </div>
              </div>
              <Switch
                checked={isPublic}
                disabled={busy}
                onCheckedChange={toggleVisibility}
                label={t('chat.showPage.visibilityPublic')}
              />
            </div>

            {isPublic && payload && !payload.url_available && payload.url_guidance && (
              <div className="rounded-md border border-border px-2.5 py-2 text-xs text-muted">
                {payload.url_guidance}
              </div>
            )}
          </>
        )}
      </PopoverContent>
    </Popover>
  );
};
