// Wrap a chat selection in quotes for the composer / a forked draft. Text that
// contains any CJK ideograph (pure Chinese OR mixed Chinese+Latin) uses the
// Chinese corner brackets 「」; pure Latin/ASCII text uses straight double
// quotes. Mixed text counts as Chinese — any CJK character flips to 「」.
// Ranges: CJK Ext A (3400–4DBF), CJK Unified (4E00–9FFF), CJK Compat (F900–FAFF).
const CJK = /[㐀-䶿一-鿿豈-﫿]/;

export function quoteText(text: string): string {
  const trimmed = text.trim();
  return CJK.test(trimmed) ? `「${trimmed}」` : `"${trimmed}"`;
}
