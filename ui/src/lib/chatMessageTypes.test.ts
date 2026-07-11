import { describe, expect, it } from 'vitest';

import { isNotifyMessageType } from './chatMessageTypes';

describe('isNotifyMessageType', () => {
  it('renders current and legacy failure rows as notifications', () => {
    expect(isNotifyMessageType('notify')).toBe(true);
    expect(isNotifyMessageType('error')).toBe(true);
  });

  it('keeps agent results out of the notification treatment', () => {
    expect(isNotifyMessageType('result')).toBe(false);
    expect(isNotifyMessageType('assistant')).toBe(false);
  });
});
