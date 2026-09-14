export function mergeMessagePages(older, current) {
  return [...new Map([...older, ...current].map(message => [message.id, message]))
    .values()].sort((a, b) => a.id - b.id);
}

export function isWritable(mode, source) {
  return mode === 'inbox' || (mode === 'library' && source?.writable === true);
}

export function uploadWithinLimit(file, maxBytes) {
  return Boolean(file && Number.isSafeInteger(file.size) && file.size > 0
    && file.size <= maxBytes);
}
