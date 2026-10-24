/**
 * Read a newline-delimited JSON stream (NDJSON) and yield one parsed value per non-empty line.
 * A trailing line without a final newline is parsed as well.
 */
export async function* readNdjson(stream: ReadableStream<Uint8Array>): AsyncGenerator<unknown> {
  const reader = stream.getReader();
  const decoder = new TextDecoder();
  let buffer = '';

  try {
    while (true) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value ?? new Uint8Array(), { stream: !done });

      let newlineIndex = buffer.indexOf('\n');
      while (newlineIndex !== -1) {
        const line = buffer.slice(0, newlineIndex).trim();
        buffer = buffer.slice(newlineIndex + 1);
        if (line) yield JSON.parse(line);
        newlineIndex = buffer.indexOf('\n');
      }

      if (done) break;
    }

    const trailing = buffer.trim();
    if (trailing) yield JSON.parse(trailing);
  } finally {
    reader.releaseLock();
  }
}
