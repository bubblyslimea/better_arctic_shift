// shared history rules for the browser reader, checked against history.py
export const HISTORY_VERSION = 2;
export const LIVE = new Set(['reddit', 'browser', 'browser_page']);

export function timestamp(value) {
  if (value == null || value === '') return null;
  let at;
  if (typeof value === 'string' && !/^\d+(\.\d+)?$/.test(value)) {
    if (!/(?:Z|[+-]\d\d:\d\d)$/i.test(value)) throw Error('include a timezone with the observation date');
    at = Date.parse(value) / 1000;
  } else at = Number(value);
  if (!Number.isFinite(at) || at < 0) throw Error("the observation date isn't valid");
  return at;
}

export const iso = value => value == null ? null : new Date(value * 1000).toISOString();

export function removal(record, text, category = null) {
  category = category || record.removed_by_category || null;
  const marker = text.trim().toLowerCase();
  let actor = null;
  if (category === 'deleted' || marker === '[deleted]') actor = 'user';
  else if (['reddit', 'removed by reddit', 'anti_evil_ops', 'community_ops', 'content_takedown', 'copyright_takedown'].includes(category) || /^\[\s*removed by reddit\b/.test(marker)) actor = 'reddit';
  else if (['moderator', 'automod_filtered'].includes(category) || record.banned_by === 'AutoModerator') actor = 'mod';
  else if (category || marker === '[removed]' || record.removed === true) actor = 'unknown';
  return actor ? {actor, category, reason: record.mod_reason_title ?? null} : null;
}

export function captureStates(record, kind, source, captured) {
  const text = record[kind === 't1' ? 'body' : 'selftext'] || '';
  if (typeof text !== 'string') throw Error("the record text isn't a string");
  const meta = record._meta || {}, first = timestamp(record.retrieved_on), second = timestamp(meta.retrieved_2nd_on);
  const dated = [first, second].filter(t => t !== null);
  const observed = LIVE.has(source) || source === 'user_report' ? captured : source === 'wayback' && record._source_time ? timestamp(record._source_time) : dated.length ? Math.max(...dated) : null;
  const base = {observed_at: observed, content_at: source === 'arctic_shift' && meta.is_edited && !meta.was_initially_deleted ? first : observed,
    body: text, removal: removal(record, text), archive_edit_flag: !!meta.is_edited};
  const rows = [];
  if (source === 'arctic_shift' && meta.was_deleted_later === true) {
    if (text.trim() && !/^\[\s*(?:deleted|removed|removed by reddit[^\]]*)\s*\]$/i.test(text.trim())) rows.push({...base, observed_at: first, content_at: first, removal: null, state: 'present'});
    const info = removal({}, '', meta.removal_type || 'unknown');
    rows.push({...base, observed_at: second, content_at: second, body: info.actor === 'user' ? '[deleted]' : '[removed]', removal: info, state: info.actor === 'user' ? 'deleted' : 'removed'});
  } else {
    if (source === 'arctic_shift' && meta.was_initially_deleted === true) {
      const info = removal({}, '', meta.removal_type || 'unknown');
      rows.push({...base, observed_at: first, content_at: first, body: info.actor === 'user' ? '[deleted]' : '[removed]', removal: info, state: info.actor === 'user' ? 'deleted' : 'removed'});
      Object.assign(base, {removal: null, observed_at: second, content_at: second});
    }
    const info = base.removal;
    rows.push({...base, state: info?.actor === 'user' ? 'deleted' : info ? 'removed' : text.trim() || record.title ? 'present' : 'unknown'});
  }
  return rows;
}

export function observationRank(observation, state, readable = false) {
  const source = observation.source;
  const value = readable && 'content_time' in observation ? observation.content_time : LIVE.has(source) || source === 'user_report' ? observation.observed_at : observation.source_time;
  return [timestamp(value) ?? -1, LIVE.has(source) ? 3 : source === 'user_report' ? 2 : 1, Number(state === 'deleted' || state === 'removed')];
}
