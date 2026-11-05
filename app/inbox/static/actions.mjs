export const BASIC_REACTIONS = ['👍', '👎', '❤️', '🔥', '🥰', '👏', '😁'];

export function selectedQuote(text, selection, paragraph) {
  if (!selection || selection.isCollapsed || !selection.rangeCount || !paragraph) return null;
  const range = selection.getRangeAt(0);
  if (!paragraph.contains(range.startContainer) || !paragraph.contains(range.endContainer)) return null;
  const prefix = range.cloneRange();
  prefix.selectNodeContents(paragraph); prefix.setEnd(range.startContainer, range.startOffset);
  // JavaScript string offsets and Telegram offsets both count UTF-16 code units.
  const offset = prefix.toString().length, quote = range.toString();
  if (!quote.trim() || quote.length > 1024 || text.slice(offset, offset + quote.length) !== quote) return null;
  return {quote_text: quote, quote_offset: offset};
}

export function birthdayLabel(birthday) {
  if (!birthday?.day || !birthday?.month) return '';
  const date = new Date(birthday.year || 2000, birthday.month - 1, birthday.day);
  return date.toLocaleDateString('ru-RU', {day: 'numeric', month: 'long',
    ...(birthday.year ? {year: 'numeric'} : {})});
}
